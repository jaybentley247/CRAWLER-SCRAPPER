"""No mocks: real sockets, the real delays, and real Chromium, against a server on this machine."""

import json
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from conftest import make_settings
from crawler.frontier import Frontier
from crawler.runner import run_crawl
from crawler.seeds import Seed
from crawler.urls import registrable_domain
from fetcher.browser import BrowserFetcher
from fetcher.rate_limit import DomainRateLimiter
from fetcher.safety import HostGuard
from fetcher.types import FetchKind
from pipeline import build_leads
from storage.db import Database

JSON_LD = {"@context": "https://schema.org", "@type": "RealEstateAgent", "name": "Acme Realty", "telephone": "0803 123 4567"}
PAGES = {
    "/robots.txt": "User-agent: *\nDisallow: /private\n",
    "/": f"""<html><head><title>Home | Acme Realty</title>
        <script type="application/ld+json">{json.dumps(JSON_LD)}</script></head>
        <body><a href="/contact">Contact</a><a href="/private/x">Private</a><a href="/app">App</a>
        <p>{'Trusted property agents serving Lagos and beyond. ' * 6}</p></body></html>""",
    "/contact": f"""<html><body>{'Talk to us any time. ' * 15}<a href="mailto:info@acme.com">Email</a>
        <address>12 Allen Avenue, Ikeja, Lagos</address>
        <a href="https://www.instagram.com/acme/?utm_source=x">Instagram</a></body></html>""",
    "/private/x": "<html><body>forbidden@acme.com</body></html>",
    "/app": """<html><body><div id="root"></div><script>
        document.getElementById('root').innerHTML = '<a href="mailto:rendered@acme.com">Email</a>';
        </script></body></html>""",
}


class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        self.server.hits.append((time.monotonic(), self.path, self.headers.get("User-Agent", "")))
        body = PAGES.get(self.path)
        kind = "text/plain" if self.path.endswith(".txt") else "text/html; charset=utf-8"
        self.send_response(200 if body is not None else 404)
        self.send_header("Content-Type", kind)
        self.end_headers()
        self.wfile.write((body or "not found").encode())

    def log_message(self, *_args):
        pass


@pytest.fixture
def server():
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    httpd.hits = []
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    yield httpd
    httpd.shutdown()
    httpd.server_close()


def settings_for(tmp_path, **browser):
    return make_settings(
        fetch={"allow_private_hosts": True},
        browser=browser or {"enabled": False},
        storage={"db_path": str(tmp_path / "t.db"), "export_dir": str(tmp_path)},
    )


async def crawl(server, settings):
    base = f"http://127.0.0.1:{server.server_port}/"
    db = Database(settings.storage.resolved_db_path)
    run_id = db.create_run("test", settings.model_dump_json())
    frontier = Frontier(db, run_id)
    frontier.add_seeds([Seed(url=base, domain=registrable_domain(base))])
    await run_crawl(settings, frontier)
    return db, run_id


async def test_real_crawl_is_paced_obeys_robots_and_produces_a_lead(server, tmp_path):
    db, run_id = await crawl(server, settings_for(tmp_path))
    try:
        paths = [path for _time, path, _agent in server.hits]
        assert paths[0] == "/robots.txt"
        assert "/private/x" not in paths
        assert paths[1:3] == ["/", "/contact"]

        times = [when for when, _path, _agent in server.hits]
        gaps = [later - earlier for earlier, later in zip(times, times[1:])]
        assert min(gaps) >= 0.95, f"requests came too quickly: {gaps}"
        assert all("LeadCrawler" in agent and "ops@acme-marketing.ng" in agent for _time, _path, agent in server.hits)

        settings = settings_for(tmp_path)
        [lead] = build_leads(db.facts_by_domain(run_id), settings.target, db.domain_statuses(run_id))
        assert lead.name.value == "Acme Realty"
        assert lead.phones[0].value == "+2348031234567"
        assert lead.emails[0].value == "info@acme.com"
        assert lead.emails[0].source_url.endswith("/contact")
        assert lead.address.value == "12 Allen Avenue, Ikeja, Lagos"
        assert lead.instagram.value == "https://instagram.com/acme"
        assert lead.category.value == "Real Estate Agent"
    finally:
        db.close()


@pytest.mark.browser
async def test_real_chromium_renders_a_javascript_page(server, tmp_path):
    settings = settings_for(tmp_path, enabled=True)
    browser = BrowserFetcher(settings, DomainRateLimiter(0, 0), HostGuard(allow_private=True))
    url = f"http://127.0.0.1:{server.server_port}/app"
    try:
        result = await browser.fetch(url)
        if not browser.available:
            pytest.skip("Chromium is not installed: python -m playwright install chromium")
    finally:
        await browser.close()
    assert result.kind is FetchKind.OK and result.rendered
    assert "rendered@acme.com" in result.text
    agents = {agent for _time, path, agent in server.hits if path == "/app"}
    assert agents == {settings.identity.user_agent}


@pytest.mark.browser
async def test_full_crawl_uses_chromium_only_when_needed(server, tmp_path):
    settings = settings_for(tmp_path, enabled=True)
    db, run_id = await crawl(server, settings)
    try:
        emails = {fact.value for fact in db.facts_by_domain(run_id)[f"127.0.0.1"] if fact.field == "email"}
        rendered = [row["url"] for row in db.conn.execute("SELECT url FROM pages WHERE rendered = 1")]
        if not rendered:
            pytest.skip("Chromium is not installed: python -m playwright install chromium")
        assert "rendered@acme.com" in emails
        assert [url.rsplit("/", 1)[-1] for url in rendered] == ["app"]
    finally:
        db.close()
