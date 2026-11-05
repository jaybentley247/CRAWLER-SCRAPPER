"""End-to-end crawl behaviour against a fake web, with every request counted."""

import asyncio

from conftest import FakeWeb, make_pdf, make_settings, pdf_reply
from fetcher.types import FetchKind, FetchResult
from pipeline import build_leads

HOME = "https://acme.com/"


def links(*paths: str) -> str:
    return "".join(f'<a href="{path}">{path}</a>' for path in paths)


async def test_visits_contact_pages_first_and_extracts_from_them(harness):
    web = FakeWeb(
        {
            HOME: links("/blog", "/portfolio", "/about", "/contact", "https://other.com/x", "/logo.png", "/login"),
            "https://acme.com/contact": 'Email info@acme.com or call 0803 123 4567 <a href="https://instagram.com/acme">ig</a>',
            "https://acme.com/about": "About the company",
            "https://acme.com/portfolio": "Work",
            "https://acme.com/blog": "Posts",
        }
    )
    run = harness(web).seed(HOME)
    await run.run()

    assert web.page_requests() == [
        HOME, "https://acme.com/contact", "https://acme.com/about", "https://acme.com/portfolio", "https://acme.com/blog",
    ]
    assert run.facts("email") == {"info@acme.com"}
    assert run.facts("phone") == {"+2348031234567"}
    assert run.domain_status("acme.com") == "done"
    assert all("mailto" in agent for agent in web.user_agents)


async def test_contact_page_found_later_jumps_ahead_of_waiting_pages(harness):
    web = FakeWeb(
        {
            HOME: links("/portfolio", "/about"),
            "https://acme.com/about": links("/contact"),
            "https://acme.com/contact": "info@acme.com",
            "https://acme.com/portfolio": "Work",
        }
    )
    run = harness(web).seed(HOME)
    await run.run()
    assert web.page_requests() == [
        HOME, "https://acme.com/about", "https://acme.com/contact", "https://acme.com/portfolio",
    ]


INTERNATIONAL = make_settings(target={"default_region": "US", "fallback_regions": ["GB", "NG"]})


async def test_local_numbers_are_read_by_each_sites_own_country(harness):
    web = FakeWeb(
        {
            "https://houston-drilling.com/": "Call (713) 967-1111. 910 Louisiana St, Houston, TX 77002",
            "https://toronto-builders.ca/": "Phone 416-967-1111",
            "https://berlin-auto.de/": "Telefon 030 12345678",
            "https://manchester-steel.co.uk/": "Ring 0161 496 0000",
            "https://lagos-realty.com.ng/": "Call 0803 123 4567",
            "https://sydney-mining.com.au/": "Call (02) 9374 4000",
            "https://tokyo-parts.co.jp/": "TEL 03-3224-9000",
            "https://mumbai-steel.in/": "Call 022 2778 2000",
            "https://saopaulo-auto.com.br/": "Ligue (11) 3333-4444",
            "https://cape-oil.co.za/": "Call 021 410 8000",
        }
    )
    run = harness(web, INTERNATIONAL).seed(*web.pages)
    await run.run()
    assert run.facts("phone") == {
        "+17139671111", "+14169671111", "+493012345678", "+441614960000", "+2348031234567",
        "+61293744000", "+81332249000", "+912227782000", "+551133334444", "+27214108000",
    }


async def test_country_from_the_seed_file_beats_guessing(harness):
    from crawler.seeds import Seed

    web = FakeWeb({"https://global-parts.com/": "Telefon 030 12345678"})
    run = harness(web, INTERNATIONAL)
    run.frontier.add_seeds([Seed(url="https://global-parts.com/", domain="global-parts.com", country="DE")])
    await run.run()
    assert run.facts("phone") == {"+493012345678"}


async def test_site_with_no_clues_falls_back_to_other_regions(harness):
    web = FakeWeb({"https://acme-realty.com/": "Call 0803 123 4567"})
    run = harness(web, INTERNATIONAL).seed("https://acme-realty.com/")
    await run.run()
    assert run.facts("phone") == {"+2348031234567"}


async def test_impressum_is_visited_before_ordinary_pages(harness):
    web = FakeWeb({HOME: links("/produkte", "/impressum")})
    run = harness(web).seed(HOME)
    await run.run()
    assert web.page_requests()[1] == "https://acme.com/impressum"


async def test_every_fact_records_the_page_it_came_from(harness):
    web = FakeWeb({HOME: links("/contact"), "https://acme.com/contact": "info@acme.com"})
    run = harness(web).seed(HOME)
    await run.run()
    facts = run.db.facts_by_domain(run.run_id)["acme.com"]
    assert next(fact for fact in facts if fact.field == "email").source_url == "https://acme.com/contact"
    assert all(fact.source_url for fact in facts)


async def test_never_visits_a_url_twice(harness):
    web = FakeWeb(
        {
            HOME: links("/a", "/a/", "/a?utm_source=x", "/a#top", "https://www.acme.com/a", "http://acme.com/a", "/"),
            "https://acme.com/a": links("/", "/a", HOME),
        }
    )
    run = harness(web).seed(HOME)
    await run.run()
    assert web.repeated() == []
    assert web.page_requests() == [HOME, "https://acme.com/a"]


async def test_index_file_is_not_fetched_as_a_second_homepage(harness):
    web = FakeWeb({HOME: links("/index.html", "/contact"), "https://acme.com/index.html": "same page"})
    run = harness(web).seed(HOME)
    await run.run()
    assert "https://acme.com/index.html" not in web.requests


async def test_redirect_target_is_not_fetched_again(harness):
    web = FakeWeb(
        {
            HOME: links("/old", "/new"),
            "https://acme.com/old": (301, "", {"location": "/new"}),
            "https://acme.com/new": "landed",
        }
    )
    run = harness(web).seed(HOME)
    await run.run()
    assert web.page_requests().count("https://acme.com/new") == 1


async def test_depth_limit(harness):
    web = FakeWeb(
        {
            HOME: links("/one"),
            "https://acme.com/one": links("/two"),
            "https://acme.com/two": links("/three"),
            "https://acme.com/three": "too deep",
        }
    )
    run = harness(web, make_settings(crawl={"max_depth": 2})).seed(HOME)
    await run.run()
    assert "https://acme.com/two" in web.requests and "https://acme.com/three" not in web.requests


async def test_page_limit_keeps_the_most_useful_pages(harness):
    many = [f"/p{i}" for i in range(20)]
    web = FakeWeb({HOME: links(*many, "/contact"), "https://acme.com/contact": "info@acme.com"})
    run = harness(web, make_settings(crawl={"max_pages_per_domain": 3})).seed(HOME)
    await run.run()
    assert len(web.page_requests()) == 3
    assert "https://acme.com/contact" in web.page_requests()


async def test_pages_that_redirect_away_still_use_up_the_page_budget(harness):
    paths = [f"/r{i}" for i in range(20)]
    web = FakeWeb(
        {
            HOME: links(*paths),
            **{f"https://acme.com{path}": (302, "", {"location": "/login"}) for path in paths},
        }
    )
    run = harness(web, make_settings(crawl={"max_pages_per_domain": 5})).seed(HOME)
    await run.run()
    assert len(web.page_requests()) == 5
    assert "https://acme.com/login" not in web.requests


async def test_robots_rules_are_obeyed_including_on_redirects(harness):
    web = FakeWeb(
        {
            "https://acme.com/robots.txt": "User-agent: *\nDisallow: /private\n",
            HOME: links("/private/data", "/hop", "/ok"),
            "https://acme.com/private/data": "secret@acme.com",
            "https://acme.com/hop": (302, "", {"location": "/private/other"}),
            "https://acme.com/private/other": "secret2@acme.com",
            "https://acme.com/ok": "info@acme.com",
        }
    )
    run = harness(web).seed(HOME)
    await run.run()
    assert not [url for url in web.requests if "/private" in url]
    assert run.facts("email") == {"info@acme.com"}
    assert run.page_statuses()["https://acme.com/private/data"] == "robots_disallowed"


async def test_site_is_skipped_when_robots_is_fully_closed(harness):
    web = FakeWeb({"https://acme.com/robots.txt": "User-agent: *\nDisallow: /\n", HOME: "secret@acme.com"})
    run = harness(web).seed(HOME)
    await run.run()
    assert web.page_requests() == []


async def test_stops_hitting_a_site_after_repeated_blocks(harness):
    pages = [f"/p{i}" for i in range(10)]
    web = FakeWeb({HOME: links(*pages), **{f"https://acme.com{p}": (403, "no") for p in pages}})
    settings = make_settings(politeness={"max_consecutive_blocks": 3})
    run = harness(web, settings).seed(HOME)
    await run.run()
    assert len(web.page_requests()) == 4  # The homepage, then three blocked pages.
    assert run.domain_status("acme.com") == "blocked"
    statuses = run.page_statuses()
    assert sum(1 for status in statuses.values() if status == "skipped") == 7


async def test_a_good_page_resets_the_block_count(harness):
    web = FakeWeb(
        {
            HOME: links("/a", "/b", "/c", "/d"),
            "https://acme.com/a": (403, "no"),
            "https://acme.com/b": "fine",
            "https://acme.com/c": (403, "no"),
            "https://acme.com/d": "fine",
        }
    )
    run = harness(web, make_settings(politeness={"max_consecutive_blocks": 2})).seed(HOME)
    await run.run()
    assert run.domain_status("acme.com") == "done" and len(web.page_requests()) == 5


async def test_unreadable_robots_stops_the_site_without_fetching_pages(harness):
    web = FakeWeb({"https://acme.com/robots.txt": (503, "down"), "http://acme.com/robots.txt": (503, "down"), HOME: "hello"})
    run = harness(web).seed(HOME)
    await run.run()
    assert web.page_requests() == []
    assert run.domain_status("acme.com") == "unreachable"


async def test_falls_back_to_http_when_https_is_broken(harness):
    def broken(_request):
        import httpx

        raise httpx.ConnectError("bad certificate")

    web = FakeWeb({"https://acme.com/robots.txt": broken, "http://acme.com/": "info@acme.com"})
    run = harness(web).seed(HOME)
    await run.run()
    assert run.facts("email") == {"info@acme.com"}
    assert run.facts("website") >= {"http://acme.com"}


async def test_excessive_crawl_delay_means_we_leave_rather_than_ignore_it(harness):
    web = FakeWeb({"https://acme.com/robots.txt": "User-agent: *\nCrawl-delay: 600\n", HOME: "hello"})
    run = harness(web).seed(HOME)
    await run.run()
    assert web.page_requests() == []
    assert run.domain_status("acme.com") == "skipped"


async def test_moved_site_is_recorded_and_not_followed(harness):
    web = FakeWeb({HOME: (301, "", {"location": "https://acme-new.ng/"}), "https://acme-new.ng/": "other"})
    run = harness(web).seed(HOME)
    await run.run()
    assert "https://acme-new.ng/" not in web.requests
    assert run.domain_status("acme.com") == "moved"


async def test_never_two_requests_at_once_to_one_site(harness):
    active: dict[str, int] = {}
    peak: dict[str, int] = {}
    overall = {"active": 0, "peak": 0}

    class Tracking(FakeWeb):
        def transport(self):
            import httpx

            async def handle(request):
                host = request.url.host
                active[host] = active.get(host, 0) + 1
                overall["active"] += 1
                peak[host] = max(peak.get(host, 0), active[host])
                overall["peak"] = max(overall["peak"], overall["active"])
                await asyncio.sleep(0.001)
                active[host] -= 1
                overall["active"] -= 1
                return self._handle(request)

            return httpx.MockTransport(handle)

    sites = [f"https://site{i}.com/" for i in range(4)]
    pages = {}
    for site in sites:
        pages[site] = links("/a", "/b", "/c")
    web = Tracking(pages)
    run = harness(web, make_settings(politeness={"concurrency": 3})).seed(*sites)
    await run.run()
    assert max(peak.values()) == 1
    assert overall["peak"] > 1  # Sites did run in parallel.
    assert overall["peak"] <= 3


async def test_one_broken_site_does_not_end_the_run(harness, monkeypatch):
    import crawler.engine as engine_module

    real = engine_module.analyse_page

    def explode(html, url, domain, settings, seed_region=None):
        if domain == "bad.com":
            raise RuntimeError("parser crashed")
        return real(html, url, domain, settings, seed_region)

    monkeypatch.setattr(engine_module, "analyse_page", explode)
    web = FakeWeb({"https://bad.com/": "x", "https://good.com/": "info@good.com"})
    run = harness(web).seed("https://bad.com/", "https://good.com/")
    await run.run()
    assert run.domain_status("bad.com") == "error"
    assert run.facts("email") == {"info@good.com"}


class StubBrowser:
    def __init__(self, html="", kind=FetchKind.OK):
        self.available = True
        self.html = html
        self.kind = kind
        self.calls: list[str] = []

    async def fetch(self, url, check_redirect=None):
        self.calls.append(url)
        return FetchResult(url=url, final_url=url, kind=self.kind, text=self.html, rendered=True)


SPA_SHELL = '<body><div id="root"></div><script src="/app.js"></script></body>'


async def test_browser_is_used_only_for_javascript_pages(harness):
    browser = StubBrowser('<body>Rendered: info@spa.com <a href="/contact">c</a></body>')
    web = FakeWeb({"https://spa.com/": SPA_SHELL, "https://spa.com/contact": "<body>" + "plain text " * 50 + "</body>"})
    run = harness(web, browser=browser).seed("https://spa.com/")
    await run.run()
    assert browser.calls == ["https://spa.com/"]
    assert run.facts("email") == {"info@spa.com"}
    assert "https://spa.com/contact" in web.requests


async def test_browser_is_not_used_to_get_past_a_block(harness):
    browser = StubBrowser("<body>walled content</body>")
    web = FakeWeb({HOME: (403, "<title>Just a moment...</title>")})
    run = harness(web, browser=browser).seed(HOME)
    await run.run()
    assert browser.calls == []


async def test_failed_render_keeps_the_static_html(harness):
    browser = StubBrowser(kind=FetchKind.NETWORK_ERROR)
    web = FakeWeb({"https://spa.com/": '<body><div id="root">info@spa.com</div><script></script></body>'})
    run = harness(web, browser=browser).seed("https://spa.com/")
    await run.run()
    assert run.facts("email") == {"info@spa.com"}


async def test_render_budget_per_site(harness):
    browser = StubBrowser(SPA_SHELL + links("/a", "/b", "/c"))
    web = FakeWeb({"https://spa.com/": SPA_SHELL, "https://spa.com/a": SPA_SHELL, "https://spa.com/b": SPA_SHELL, "https://spa.com/c": SPA_SHELL})
    settings = make_settings(browser={"max_renders_per_domain": 2})
    run = harness(web, settings, browser=browser).seed("https://spa.com/")
    await run.run()
    assert len(browser.calls) == 2


class TestResume:
    def site(self) -> dict:
        return {
            HOME: links("/a", "/b", "/c", "/d"),
            "https://acme.com/a": "a@acme.com",
            "https://acme.com/b": "b@acme.com",
            "https://acme.com/c": "c@acme.com",
            "https://acme.com/d": "d@acme.com",
        }

    async def test_interrupted_run_continues_without_repeating_finished_pages(self, harness):
        web = FakeWeb(self.site())
        seen = []

        def interrupt(page, _status):
            seen.append(page.url)
            if len(seen) == 3:
                raise asyncio.CancelledError

        run = harness(web, on_page=interrupt).seed(HOME)
        try:
            await run.run()
        except asyncio.CancelledError:
            pass
        assert len(web.page_requests()) == 3
        assert run.domain_status("acme.com") == "active"

        run.on_page = None
        await run.run()

        assert web.repeated() == []
        assert len(web.page_requests()) == 5
        assert run.facts("email") == {"a@acme.com", "b@acme.com", "c@acme.com", "d@acme.com"}
        assert run.domain_status("acme.com") == "done"

    async def test_page_cut_off_mid_fetch_is_retried_on_resume(self, harness):
        web = FakeWeb(self.site())
        run = harness(web).seed(HOME)
        page = run.frontier.claim_next("acme.com")
        assert run.page_statuses()[page.url] == "in_progress"
        await run.run()
        assert run.page_statuses()[page.url] == "done"
        assert web.repeated() == []

    async def test_finished_and_blocked_sites_are_not_revisited(self, harness):
        web = FakeWeb({HOME: "done", "https://blocked.com/": (403, "no")})
        settings = make_settings(politeness={"max_consecutive_blocks": 1})
        run = harness(web, settings).seed(HOME, "https://blocked.com/")
        await run.run()
        before = len(web.requests)
        await run.run()
        assert len(web.requests) == before

    async def test_page_budget_survives_resume(self, harness):
        many = [f"/p{i}" for i in range(20)]
        web = FakeWeb({HOME: links(*many)})
        calls = []

        def interrupt(page, _status):
            calls.append(page)
            if len(calls) == 2:
                raise asyncio.CancelledError

        run = harness(web, make_settings(crawl={"max_pages_per_domain": 4}), on_page=interrupt).seed(HOME)
        try:
            await run.run()
        except asyncio.CancelledError:
            pass
        run.on_page = None
        await run.run()
        assert len(web.page_requests()) == 4


class TestPdfSeeds:
    PDF = "https://college.edu/files/contractors.pdf"
    LIST = make_pdf([
        "Cooley Construction  760-555-0100  ben@cooley-builders.com",
        "Desert Construction  desert.builder.test@aol.com",
        "See https://college.edu/more for more",
    ])

    async def test_each_listed_business_becomes_a_lead(self, harness):
        web = FakeWeb({self.PDF: pdf_reply(self.LIST)})
        run = harness(web).seed(self.PDF)
        await run.run()

        assert web.page_requests() == [self.PDF]  # Nothing in a PDF is followed.
        assert "https://college.edu/robots.txt" in web.requests
        facts = run.db.facts_by_domain(run.run_id)
        assert {domain: {fact.value for fact in found} for domain, found in facts.items()} == {
            "cooley-builders.com": {"ben@cooley-builders.com"},
            "desert.builder.test@aol.com": {"desert.builder.test@aol.com"},
        }
        leads = build_leads(facts, run.settings.target, run.db.domain_statuses(run.run_id))
        assert sorted(lead.emails[0].value for lead in leads) == ["ben@cooley-builders.com", "desert.builder.test@aol.com"]
        assert run.domain_status("college.edu") == "done"

    async def test_robots_rules_apply_to_pdfs(self, harness):
        web = FakeWeb({"https://college.edu/robots.txt": "User-agent: *\nDisallow: /files/\n", self.PDF: pdf_reply(self.LIST)})
        run = harness(web).seed(self.PDF)
        await run.run()
        assert web.page_requests() == []
        assert run.page_statuses()[self.PDF] == "robots_disallowed"

    async def test_a_page_served_instead_of_the_pdf_is_not_read(self, harness):
        web = FakeWeb({self.PDF: pdf_reply(b"<html>Please log in. info@college.edu</html>", "text/html")})
        run = harness(web).seed(self.PDF)
        await run.run()
        assert run.page_statuses()[self.PDF] == "failed"
        assert run.facts("email") == set()

    async def test_pdf_and_website_seeds_run_together(self, harness):
        web = FakeWeb({self.PDF: pdf_reply(self.LIST), HOME: "Email info@acme.com"})
        run = harness(web).seed(self.PDF, HOME)
        await run.run()
        assert run.facts("email") == {"ben@cooley-builders.com", "desert.builder.test@aol.com", "info@acme.com"}

    async def test_spreadsheet_seed_becomes_leads_even_when_its_link_says_xls(self, harness):
        from test_extractors import TestSpreadsheets

        sheet = "https://college.edu/forms/contractors.xls"
        web = FakeWeb({sheet: pdf_reply(TestSpreadsheets.xlsx(), "application/vnd.ms-excel")})
        run = harness(web).seed(sheet)
        await run.run()
        assert run.facts("email") == {"ben@cooley-builders.com", "desert.builder.test@aol.com", "estimating@kerns-paving.com"}
        assert run.page_statuses()[sheet] == "done"

    async def test_pdf_may_redirect_to_another_pdf_but_not_into_a_private_area(self, harness):
        moved = "https://college.edu/files/2024/contractors.pdf"
        web = FakeWeb({
            self.PDF: (301, "", {"location": moved}),
            moved: pdf_reply(self.LIST),
            "https://college.edu/old.pdf": (302, "", {"location": "/login/list.pdf"}),
            "https://college.edu/login/list.pdf": pdf_reply(self.LIST),
        })
        run = harness(web).seed(self.PDF, "https://college.edu/old.pdf")
        await run.run()
        assert "https://college.edu/login/list.pdf" not in web.requests
        assert run.facts("email") == {"ben@cooley-builders.com", "desert.builder.test@aol.com"}

    async def test_file_moved_to_another_domain_is_followed_with_its_robots_rules(self, harness):
        moved = "https://new-host.org/files/contractors.pdf"
        blocked = "https://closed-host.org/files/contractors.pdf"
        web = FakeWeb({
            self.PDF: (301, "", {"location": moved}),
            moved: pdf_reply(self.LIST),
            "https://college.edu/other.pdf": (302, "", {"location": blocked}),
            "https://closed-host.org/robots.txt": "User-agent: *\nDisallow: /files/\n",
            blocked: pdf_reply(self.LIST),
        })
        run = harness(web).seed(self.PDF, "https://college.edu/other.pdf")
        await run.run()
        assert "https://new-host.org/robots.txt" in web.requests
        assert blocked not in web.requests
        assert run.facts("email") == {"ben@cooley-builders.com", "desert.builder.test@aol.com"}
        assert {fact.source_url for facts in run.db.facts_by_domain(run.run_id).values() for fact in facts} == {moved}

    async def test_website_moved_to_another_domain_still_stops(self, harness):
        web = FakeWeb({HOME: (301, "", {"location": "https://elsewhere.com/"}), "https://elsewhere.com/": "info@elsewhere.com"})
        run = harness(web).seed(HOME)
        await run.run()
        assert run.domain_status("acme.com") == "moved"
        assert "https://elsewhere.com/" not in web.requests
