import random
from datetime import datetime, timedelta, timezone
from email.utils import format_datetime

import httpx
import pytest

from conftest import FakeWeb, make_settings, no_sleep
from crawler.robots import RobotsCache, RobotsVerdict
from fetcher.detect import is_bot_wall, needs_javascript
from fetcher.http_client import HttpFetcher, _parse_retry_after
from fetcher.rate_limit import DomainRateLimiter
from fetcher.safety import HostGuard
from fetcher.types import FetchKind

URL = "https://acme.com/"


def build(web: FakeWeb, *, sleeps=None, guard=None, settings=None) -> HttpFetcher:
    settings = settings or make_settings()
    limiter = DomainRateLimiter(1, 3, sleep=no_sleep)

    async def record(seconds: float) -> None:
        if sleeps is not None:
            sleeps.append(seconds)

    return HttpFetcher(
        settings, limiter, guard or HostGuard(allow_private=True), transport=web.transport(), sleep=record, rng=random.Random(1)
    )


def sequence(*replies):
    """A page that answers differently on each request."""
    remaining = list(replies)

    def handler(_request):
        status, headers = remaining.pop(0) if len(remaining) > 1 else remaining[0]
        return httpx.Response(status, text="<html><body>hi</body></html>", headers={"content-type": "text/html", **headers})

    return handler


class TestRateLimiter:
    async def test_waits_between_requests_for_the_same_site(self):
        now = [100.0]
        waits: list[float] = []

        async def fake_sleep(seconds):
            waits.append(seconds)
            now[0] += seconds

        limiter = DomainRateLimiter(1, 3, clock=lambda: now[0], sleep=fake_sleep, rng=random.Random(7))
        async with limiter.slot("a.com"):
            pass
        async with limiter.slot("a.com"):
            pass
        assert len(waits) == 1 and 1 <= waits[0] <= 3

    async def test_different_sites_do_not_wait_for_each_other(self):
        waits: list[float] = []

        async def fake_sleep(seconds):
            waits.append(seconds)

        limiter = DomainRateLimiter(1, 3, clock=lambda: 0.0, sleep=fake_sleep)
        async with limiter.slot("a.com"):
            pass
        async with limiter.slot("b.com"):
            pass
        assert waits == []

    async def test_crawl_delay_raises_the_floor_and_cannot_lower_it(self):
        limiter = DomainRateLimiter(1, 3, rng=random.Random(1))
        limiter.set_crawl_delay("a.com", 10)
        limiter.set_crawl_delay("a.com", 2)
        assert limiter._next_delay("a.com") == 10
        limiter.set_crawl_delay("b.com", 0.1)
        assert 1 <= limiter._next_delay("b.com") <= 3

    async def test_pause_is_timed_from_the_end_of_the_response(self):
        now = [0.0]
        waits: list[float] = []

        async def fake_sleep(seconds):
            waits.append(seconds)
            now[0] += seconds

        limiter = DomainRateLimiter(2, 2, clock=lambda: now[0], sleep=fake_sleep)
        async with limiter.slot("a.com"):
            now[0] += 50  # A very slow response.
        async with limiter.slot("a.com"):
            pass
        assert waits == [2]


class TestRetries:
    async def test_retries_5xx_with_growing_backoff_then_succeeds(self):
        web = FakeWeb({URL: sequence((500, {}), (502, {}), (200, {}))})
        sleeps: list[float] = []
        async with build(web, sleeps=sleeps) as http:
            result = await http.fetch(URL)
        assert result.kind is FetchKind.OK and len(web.requests) == 3
        assert 2 <= sleeps[0] <= 2.5 and 4 <= sleeps[1] <= 5

    async def test_gives_up_after_max_retries(self):
        web = FakeWeb({URL: (503, "down")})
        async with build(web, sleeps=[]) as http:
            result = await http.fetch(URL)
        assert result.kind is FetchKind.SERVER_ERROR and len(web.requests) == 4

    async def test_429_honours_retry_after(self):
        web = FakeWeb({URL: sequence((429, {"retry-after": "30"}), (200, {}))})
        sleeps: list[float] = []
        async with build(web, sleeps=sleeps) as http:
            result = await http.fetch(URL)
        assert result.kind is FetchKind.OK and sleeps == [30]

    async def test_excessive_retry_after_stops_immediately(self):
        web = FakeWeb({URL: (429, "slow down", {"retry-after": "86400"})})
        async with build(web, sleeps=[]) as http:
            result = await http.fetch(URL)
        assert result.kind is FetchKind.BLOCKED and len(web.requests) == 1

    async def test_403_and_404_are_not_retried(self):
        web = FakeWeb({URL: (403, "no"), "https://acme.com/gone": (404, "gone")})
        async with build(web) as http:
            assert (await http.fetch(URL)).kind is FetchKind.BLOCKED
            assert (await http.fetch("https://acme.com/gone")).kind is FetchKind.CLIENT_ERROR
        assert len(web.requests) == 2

    async def test_timeouts_retry_but_dns_failures_do_not(self):
        calls = {"timeout": 0, "dns": 0}

        def handler(request):
            kind = "timeout" if "timeout" in request.url.host else "dns"
            calls[kind] += 1
            raise (httpx.ReadTimeout("slow") if kind == "timeout" else httpx.ConnectError("no such host"))

        limiter = DomainRateLimiter(1, 3, sleep=no_sleep)
        http = HttpFetcher(make_settings(), limiter, HostGuard(True), transport=httpx.MockTransport(handler), sleep=no_sleep)
        async with http:
            assert (await http.fetch("https://timeout.com/")).kind is FetchKind.NETWORK_ERROR
            assert (await http.fetch("https://dns.com/")).kind is FetchKind.NETWORK_ERROR
        assert calls == {"timeout": 4, "dns": 1}

    def test_retry_after_formats(self):
        assert _parse_retry_after("120") == 120
        assert _parse_retry_after("nonsense") is None
        assert _parse_retry_after(None) is None
        future = format_datetime(datetime.now(timezone.utc) + timedelta(seconds=60), usegmt=True)
        assert 50 < _parse_retry_after(future) <= 60


class TestResponses:
    async def test_sends_an_honest_user_agent_and_contact(self):
        seen = {}

        def handler(request):
            seen.update(request.headers)
            return httpx.Response(200, text="ok", headers={"content-type": "text/html"})

        async with build(FakeWeb({URL: handler})) as http:
            await http.fetch(URL)
        assert seen["user-agent"] == "LeadCrawler/1.0 (+mailto:ops@acme-marketing.ng)"
        assert seen["from"] == "ops@acme-marketing.ng"

    async def test_non_html_is_not_downloaded(self):
        web = FakeWeb({URL: (200, "%PDF", {"content-type": "application/pdf"})})
        async with build(web) as http:
            assert (await http.fetch(URL)).kind is FetchKind.NOT_HTML

    async def test_oversize_body_is_abandoned(self):
        settings = make_settings(fetch={"max_bytes": 100})
        async with build(FakeWeb({URL: "x" * 500}), settings=settings) as http:
            assert (await http.fetch(URL)).kind is FetchKind.TOO_LARGE

    async def test_decodes_charset_from_header_and_meta(self):
        def latin_header(_request):
            return httpx.Response(200, content="Café".encode("latin-1"), headers={"content-type": "text/html; charset=latin-1"})

        def latin_meta(_request):
            return httpx.Response(200, content='<meta charset="latin-1">Café'.encode("latin-1"), headers={"content-type": "text/html"})

        web = FakeWeb({URL: latin_header, "https://acme.com/m": latin_meta})
        async with build(web) as http:
            assert "Café" in (await http.fetch(URL)).text
            assert "Café" in (await http.fetch("https://acme.com/m")).text

    async def test_redirects_are_followed_and_checked_per_hop(self):
        web = FakeWeb(
            {
                URL: (301, "", {"location": "/a"}),
                "https://acme.com/a": (302, "", {"location": "https://acme.com/b"}),
                "https://acme.com/b": "<p>final</p>",
                "https://acme.com/out": (302, "", {"location": "https://evil.com/"}),
            }
        )
        checked: list[str] = []

        async def check(target):
            checked.append(target)
            return FetchKind.OFFSITE_REDIRECT if "evil" in target else None

        async with build(web) as http:
            ok = await http.fetch(URL, check_redirect=check)
            refused = await http.fetch("https://acme.com/out", check_redirect=check)
        assert ok.kind is FetchKind.OK and ok.final_url == "https://acme.com/b"
        assert refused.kind is FetchKind.OFFSITE_REDIRECT
        assert "https://evil.com/" not in web.requests
        assert checked == ["https://acme.com/a", "https://acme.com/b", "https://evil.com/"]

    async def test_redirect_loop_stops(self):
        web = FakeWeb({URL: (302, "", {"location": "/b"}), "https://acme.com/b": (302, "", {"location": "/"})})
        async with build(web) as http:
            assert (await http.fetch(URL)).kind is FetchKind.TOO_MANY_REDIRECTS
        assert len(web.requests) == 6


class TestDetection:
    def test_bot_walls(self):
        assert is_bot_wall({"cf-mitigated": "challenge"}, "")
        assert is_bot_wall({}, "<title>Just a moment...</title><body>checking</body>")
        assert not is_bot_wall({}, "<title>Acme Realty</title><body><script src='/cdn-cgi/challenge-platform/x'></script>hello</body>")
        long_article = "<title>What is a CAPTCHA?</title><body>" + "word " * 1000 + "</body>"
        assert not is_bot_wall({}, long_article)

    async def test_bot_wall_with_status_200_counts_as_blocked_and_is_not_retried(self):
        web = FakeWeb({URL: "<title>Attention Required! | Cloudflare</title><body>blocked</body>"})
        async with build(web) as http:
            assert (await http.fetch(URL)).kind is FetchKind.BLOCKED
        assert len(web.requests) == 1

    def test_needs_javascript(self):
        assert needs_javascript('<body><div id="root"></div><script src="a.js"></script></body>', 200)
        assert needs_javascript("<body>Please enable JavaScript to view this.</body>", 200)
        assert not needs_javascript("<body>" + "real content " * 50 + "<script></script></body>", 200)
        assert not needs_javascript("<body>Tiny static page</body>", 200)


class TestHostGuard:
    @pytest.mark.parametrize(
        "url",
        [
            "http://localhost/", "http://127.0.0.1/", "http://10.0.0.5/", "http://192.168.1.1/admin",
            "http://169.254.169.254/latest/meta-data/", "http://[::1]/", "http://printer.local/",
        ],
    )
    async def test_refuses_private_addresses(self, url):
        assert not await HostGuard().allowed(url)

    async def test_public_ip_and_override(self):
        assert await HostGuard().allowed("http://8.8.8.8/")
        assert await HostGuard(allow_private=True).allowed("http://127.0.0.1/")

    async def test_redirect_to_private_address_is_refused(self):
        web = FakeWeb({"http://8.8.8.8/": (302, "", {"location": "http://127.0.0.1/secret"})})
        async with build(web, guard=HostGuard()) as http:
            result = await http.fetch("http://8.8.8.8/")
        assert result.kind is FetchKind.FORBIDDEN_HOST
        assert "http://127.0.0.1/secret" not in web.requests


class TestRobots:
    async def check(self, web: FakeWeb, url: str):
        async with build(web, sleeps=[]) as http:
            return await RobotsCache(http, "LeadCrawler").check(url)

    async def test_rules_for_our_agent_and_wildcards(self):
        robots = "User-agent: *\nDisallow: /private/\nDisallow: /*.php$\n\nUser-agent: LeadCrawler\nDisallow: /nope\nCrawl-delay: 7\n"
        web = FakeWeb({"https://acme.com/robots.txt": robots})
        async with build(web) as http:
            cache = RobotsCache(http, "LeadCrawler")
            assert not (await cache.check("https://acme.com/nope")).allowed
            allowed = await cache.check("https://acme.com/private/x")
            assert allowed.allowed and allowed.crawl_delay == 7
        assert web.requests.count("https://acme.com/robots.txt") == 1

    async def test_wildcard_group_applies_when_not_named(self):
        web = FakeWeb({"https://acme.com/robots.txt": "User-agent: *\nDisallow: /\n"})
        assert (await self.check(web, "https://acme.com/a")).verdict is RobotsVerdict.DISALLOWED

    async def test_missing_robots_allows(self):
        assert (await self.check(FakeWeb({}), "https://acme.com/a")).allowed

    @pytest.mark.parametrize("status", [429, 500, 503])
    async def test_server_trouble_means_do_not_crawl(self, status):
        web = FakeWeb({"https://acme.com/robots.txt": (status, "")})
        assert (await self.check(web, "https://acme.com/a")).verdict is RobotsVerdict.UNREACHABLE

    async def test_each_subdomain_has_its_own_rules(self):
        web = FakeWeb({"https://shop.acme.com/robots.txt": "User-agent: *\nDisallow: /\n"})
        async with build(web) as http:
            cache = RobotsCache(http, "LeadCrawler")
            assert (await cache.check("https://acme.com/a")).allowed
            assert not (await cache.check("https://shop.acme.com/a")).allowed
