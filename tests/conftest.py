"""Shared test helpers: valid settings, and a fake web that records every request."""

from __future__ import annotations

from collections import Counter
from collections.abc import Callable

import httpx
import pytest

from crawler.engine import CrawlEngine
from crawler.frontier import Frontier
from crawler.robots import RobotsCache
from crawler.seeds import Seed
from crawler.urls import registrable_domain
from fetcher.http_client import HttpFetcher
from fetcher.rate_limit import DomainRateLimiter
from fetcher.safety import HostGuard
from settings import Settings, validate_settings
from storage.db import Database

Reply = str | tuple[int, str] | tuple[int, str, dict[str, str]] | Callable[[httpx.Request], httpx.Response]


def make_settings(**sections) -> Settings:
    raw = {
        "identity": {"contact_email": "ops@acme-marketing.ng"},
        "target": {"default_region": "NG", "location_hints": ["Lagos", "Ikeja"]},
    }
    raw.update(sections)
    return validate_settings(raw)


class FakeWeb:
    """A pretend internet. URLs not listed return 404, and every request is recorded."""

    def __init__(self, pages: dict[str, Reply]):
        self.pages = pages
        self.requests: list[str] = []
        self.user_agents: set[str] = set()

    def transport(self) -> httpx.MockTransport:
        return httpx.MockTransport(self._handle)

    def _handle(self, request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        self.requests.append(url)
        self.user_agents.add(request.headers.get("user-agent", ""))
        reply = self.pages.get(url)
        if reply is None:
            return httpx.Response(404, text="not found", headers={"content-type": "text/html"})
        if callable(reply):
            return reply(request)
        if isinstance(reply, str):
            content_type = "text/plain" if url.endswith("robots.txt") else "text/html"
            return httpx.Response(200, text=reply, headers={"content-type": content_type})
        status, body, *rest = reply
        headers = {"content-type": "text/html", **(rest[0] if rest else {})}
        return httpx.Response(status, text=body, headers=headers)

    def page_requests(self) -> list[str]:
        return [url for url in self.requests if not url.endswith("/robots.txt")]

    def repeated(self) -> list[str]:
        return [url for url, count in Counter(self.page_requests()).items() if count > 1]


def make_pdf(lines: list[str], links: tuple[str, ...] = ()) -> bytes:
    """A small, valid one-page PDF with these lines of text and these clickable links."""
    text = "\n".join(f"({line}) Tj 0 -16 Td" for line in lines)
    stream = f"BT /F1 11 Tf 40 800 Td\n{text}\nET".encode("latin-1")
    annots = " ".join(f"{6 + index} 0 R" for index in range(len(links)))
    objects = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        f"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 595 842] /Contents 4 0 R "
        f"/Resources << /Font << /F1 5 0 R >> >> /Annots [{annots}] >>".encode(),
        b"<< /Length %d >>\nstream\n" % len(stream) + stream + b"\nendstream",
        b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
        *(f"<< /Type /Annot /Subtype /Link /Rect [0 0 10 10] /A << /S /URI /URI ({link}) >> >>".encode() for link in links),
    ]
    out = bytearray(b"%PDF-1.4\n")
    offsets = []
    for number, body in enumerate(objects, start=1):
        offsets.append(len(out))
        out += b"%d 0 obj\n" % number + body + b"\nendobj\n"
    xref = len(out)
    out += b"xref\n0 %d\n0000000000 65535 f \n" % (len(objects) + 1)
    out += b"".join(b"%010d 00000 n \n" % offset for offset in offsets)
    out += b"trailer\n<< /Size %d /Root 1 0 R >>\nstartxref\n%d\n%%%%EOF\n" % (len(objects) + 1, xref)
    return bytes(out)


def pdf_reply(body: bytes, content_type: str = "application/pdf") -> Callable[[httpx.Request], httpx.Response]:
    return lambda _request: httpx.Response(200, content=body, headers={"content-type": content_type})


async def no_sleep(_seconds: float) -> None:
    return None


class Harness:
    """A real engine, frontier and database, connected to a FakeWeb with the delays removed."""

    def __init__(self, web: FakeWeb, settings: Settings, db_path, browser=None, on_page=None):
        self.web = web
        self.settings = settings
        self.db = Database(db_path)
        self.browser = browser
        self.on_page = on_page
        self.run_id = self.db.create_run("test", settings.model_dump_json())
        self.frontier = Frontier(self.db, self.run_id)

    def seed(self, *urls: str) -> "Harness":
        self.frontier.add_seeds(Seed(url=url, domain=registrable_domain(url)) for url in urls)
        return self

    async def run(self) -> None:
        polite = self.settings.politeness
        limiter = DomainRateLimiter(polite.min_delay, polite.max_delay, sleep=no_sleep)
        http = HttpFetcher(self.settings, limiter, HostGuard(allow_private=True), transport=self.web.transport(), sleep=no_sleep)
        try:
            robots = RobotsCache(http, self.settings.identity.bot_name)
            engine = CrawlEngine(self.settings, self.frontier, http, robots, limiter, self.browser, self.on_page)
            await engine.run()
        finally:
            await http.close()

    def page_statuses(self) -> dict[str, str]:
        rows = self.db.conn.execute("SELECT url, status FROM pages WHERE run_id = ?", (self.run_id,))
        return {row["url"]: row["status"] for row in rows}

    def domain_status(self, domain: str) -> str:
        return self.db.domain_statuses(self.run_id)[domain]

    def facts(self, field: str) -> set[str]:
        return {
            fact.value
            for facts in self.db.facts_by_domain(self.run_id).values()
            for fact in facts
            if fact.field == field
        }


@pytest.fixture
def harness(tmp_path):
    created: list[Harness] = []

    def build(web: FakeWeb, settings: Settings | None = None, **kwargs) -> Harness:
        item = Harness(web, settings or make_settings(), tmp_path / f"h{len(created)}.db", **kwargs)
        created.append(item)
        return item

    yield build
    for item in created:
        item.db.close()
