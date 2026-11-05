"""The crawl loop: visits each site one page at a time, in priority order, within all the limits."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable
from functools import partial

from crawler.frontier import Frontier, Page
from crawler.links import Link, extract_links
from crawler.robots import RobotsCache, RobotsVerdict
from crawler.urls import is_document_url, normalize_url, origin, same_site, skip_reason
from extractors import extract_all
from extractors.document import UnreadableDocument, extract_document
from extractors.page import ParsedPage
from extractors.region import detect_region
from fetcher.browser import BrowserFetcher
from fetcher.detect import needs_javascript
from fetcher.http_client import HttpFetcher
from fetcher.rate_limit import DomainRateLimiter
from fetcher.types import COUNTS_AS_BLOCK, FetchKind, FetchResult
from models import Fact
from settings import Settings

log = logging.getLogger(__name__)


def analyse_page(
    html: str, url: str, domain: str, settings: Settings, seed_region: str | None = None
) -> tuple[list[Fact], list[Link]]:
    page = ParsedPage(html, url)
    target = settings.target
    region, confident = detect_region(page, seed_region, target.default_region)
    # Only guess at other countries when nothing on the page said where this business is.
    fallbacks = () if confident else [code for code in target.fallback_regions if code != region]
    facts = extract_all(page, region, target.location_hints, fallbacks)
    return facts, extract_links(page, domain, settings.crawl.max_links_per_page)


class CrawlEngine:
    def __init__(
        self,
        settings: Settings,
        frontier: Frontier,
        http: HttpFetcher,
        robots: RobotsCache,
        limiter: DomainRateLimiter,
        browser: BrowserFetcher | None = None,
        on_page: Callable[[Page, str], None] | None = None,
        on_domain_done: Callable[[str], None] | None = None,
    ):
        self.settings = settings
        self.frontier = frontier
        self.http = http
        self.robots = robots
        self.limiter = limiter
        self.browser = browser
        self._on_page = on_page or (lambda _page, _status: None)
        self._on_domain_done = on_domain_done or (lambda _domain: None)

    async def run(self) -> None:
        self.frontier.reset_interrupted()
        queue: asyncio.Queue[str] = asyncio.Queue()
        for domain in self.frontier.active_domains():
            queue.put_nowait(domain)

        async def worker() -> None:
            # Each site belongs to one worker until it's finished, so a site never
            # receives parallel requests however high concurrency is set.
            while True:
                try:
                    domain = queue.get_nowait()
                except asyncio.QueueEmpty:
                    return
                try:
                    await self._crawl_domain(domain)
                except Exception as exc:  # One broken site must not end a run of thousands.
                    log.exception("Unexpected error crawling %s", domain)
                    self.frontier.finish_domain(domain, "error", f"{type(exc).__name__}: {exc}"[:200])
                self._on_domain_done(domain)

        await asyncio.gather(*(worker() for _ in range(self.settings.politeness.concurrency)))

    async def _crawl_domain(self, domain: str) -> None:
        limit = self.settings.crawl.max_pages_per_domain
        while True:
            if self.frontier.pages_attempted(domain) >= limit:
                self.frontier.finish_domain(domain, "done", "page_limit")
                return
            page = self.frontier.claim_next(domain)
            if page is None:
                self.frontier.finish_domain(domain, "done")
                return
            if not await self._process_page(page):
                return

    async def _process_page(self, page: Page) -> bool:
        """Handle one page. Returns False when the whole site should stop."""
        decision = await self.robots.check(page.url)

        if decision.verdict is RobotsVerdict.UNREACHABLE:
            if page.depth == 0 and page.url.startswith("https://"):
                # Plenty of small business sites have no working HTTPS. Try the plain address.
                self.frontier.rewrite_url(page, "http://" + page.url.removeprefix("https://"))
                return True
            self.frontier.finish_page(page, "failed", "robots_unavailable")
            self.frontier.finish_domain(page.domain, "unreachable", "robots.txt could not be read")
            self._on_page(page, "failed")
            return False

        if decision.verdict is RobotsVerdict.DISALLOWED:
            self.frontier.finish_page(page, "robots_disallowed")
            self._on_page(page, "robots_disallowed")
            return True

        if decision.crawl_delay:
            if decision.crawl_delay > self.settings.politeness.max_crawl_delay:
                # We won't ignore the requested delay, and it's too slow to honour, so we leave.
                self.frontier.finish_domain(page.domain, "skipped", f"crawl_delay {decision.crawl_delay:.0f}s")
                return False
            self.limiter.set_crawl_delay(page.domain, decision.crawl_delay)

        document = is_document_url(page.url)
        check = partial(self._check_redirect, page.domain, document=document)
        result = await self.http.fetch(page.url, check_redirect=check, html_only=not document)
        return await self._handle_result(page, result)

    async def _check_redirect(self, domain: str, target: str, document: bool = False) -> FetchKind | None:
        # A site moving means a new business, but a file moving is still the same file.
        # Its new host gets the same robots.txt, pacing and private-address checks.
        if not same_site(target, domain) and not document:
            return FetchKind.OFFSITE_REDIRECT
        if skip_reason(target, allow_documents=document):
            return FetchKind.SKIPPED_REDIRECT
        if (await self.robots.check(target)).verdict is not RobotsVerdict.ALLOWED:
            return FetchKind.ROBOTS_DISALLOWED
        return None

    async def _handle_result(self, page: Page, result: FetchResult) -> bool:
        if result.kind is FetchKind.OK and is_document_url(page.url):
            status = await self._save_document(page, result)
            self._on_page(page, status)
            return True

        if result.kind is FetchKind.OK:
            await self._save_page(page, result)
            self._on_page(page, "done")
            return True

        if result.kind in COUNTS_AS_BLOCK:
            failures = self.frontier.record_failure(page, result.error or result.kind.value, result.status)
            self._on_page(page, "failed")
            if failures >= self.settings.politeness.max_consecutive_blocks:
                log.warning("Stopping %s after %d blocks or failures in a row", page.domain, failures)
                self.frontier.finish_domain(page.domain, "blocked", result.error or result.kind.value)
                return False
            return True

        if result.kind is FetchKind.OFFSITE_REDIRECT and page.depth == 0:
            # The whole site moved. Stop, and leave the new address in the log and database.
            self.frontier.finish_page(page, "skipped", result.error, result.status)
            self.frontier.finish_domain(page.domain, "moved", result.error)
            log.warning("%s has moved (%s). Add the new address as a seed to crawl it.", page.domain, result.final_url)
            self._on_page(page, "skipped")
            return False

        # Both statuses count against the page budget, because a request was spent either way.
        status = "failed" if result.kind in {FetchKind.NOT_HTML, FetchKind.CLIENT_ERROR, FetchKind.TOO_LARGE} else "redirected"
        self.frontier.finish_page(page, status, result.error or result.kind.value, result.status)
        self._on_page(page, status)
        return True

    async def _save_page(self, page: Page, result: FetchResult) -> None:
        html, rendered = await self._maybe_render(page, result)
        final_url = normalize_url(result.final_url) or page.url
        # Parsing a big page takes real CPU time, so it runs off the event loop.
        seed_region = self.frontier.domain_region(page.domain)
        facts, links = await asyncio.to_thread(analyse_page, html, final_url, page.domain, self.settings, seed_region)
        if page.depth == 0:
            facts.append(Fact(field="website", value=origin(final_url), source_url=final_url, method="crawled", confidence=0.9))
        if page.depth >= self.settings.crawl.max_depth:
            links = []
        self.frontier.record_success(
            page, final_url=final_url, http_status=result.status, rendered=rendered, facts=facts, links=links
        )

    async def _save_document(self, page: Page, result: FetchResult) -> str:
        final_url = normalize_url(result.final_url) or page.url
        try:
            leads = await asyncio.to_thread(extract_document, result.body, final_url)
        except UnreadableDocument as exc:
            log.warning("Could not read %s (%s)", page.url, exc)
            self.frontier.finish_page(page, "failed", str(exc), result.status)
            return "failed"
        if not leads:
            log.warning("No emails found in %s. A scanned PDF or a picture in a spreadsheet has no text to read.", page.url)
        self.frontier.record_success(
            page, final_url=final_url, http_status=result.status, rendered=False, facts=[], links=[], other_leads=leads
        )
        return "done"

    async def _maybe_render(self, page: Page, result: FetchResult) -> tuple[str, bool]:
        browser_settings = self.settings.browser
        if (
            self.browser is None
            or not self.browser.available
            or not needs_javascript(result.text, browser_settings.min_text_chars)
            or self.frontier.renders_used(page.domain) >= browser_settings.max_renders_per_domain
        ):
            return result.text, False

        self.frontier.add_render(page.domain)
        check = partial(self._check_redirect, page.domain)
        rendered = await self.browser.fetch(normalize_url(result.final_url) or page.url, check)
        if rendered.kind is FetchKind.OK:
            return rendered.text, True
        log.info("Rendering %s failed (%s). Using the static HTML.", page.url, rendered.error)
        return result.text, False
