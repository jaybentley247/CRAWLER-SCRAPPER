"""Connects the parts for one run. Used by the CLI and the end-to-end tests."""

from __future__ import annotations

from collections.abc import Callable

from crawler.engine import CrawlEngine
from crawler.frontier import Frontier, Page
from crawler.robots import RobotsCache
from fetcher.browser import BrowserFetcher
from fetcher.http_client import HttpFetcher
from fetcher.rate_limit import DomainRateLimiter
from fetcher.safety import HostGuard
from settings import Settings


async def run_crawl(
    settings: Settings,
    frontier: Frontier,
    on_page: Callable[[Page, str], None] | None = None,
    on_domain_done: Callable[[str], None] | None = None,
) -> None:
    polite = settings.politeness
    limiter = DomainRateLimiter(polite.min_delay, polite.max_delay)
    guard = HostGuard(settings.fetch.allow_private_hosts)
    browser = BrowserFetcher(settings, limiter, guard) if settings.browser.enabled else None
    try:
        async with HttpFetcher(settings, limiter, guard) as http:
            robots = RobotsCache(http, settings.identity.bot_name)
            engine = CrawlEngine(settings, frontier, http, robots, limiter, browser, on_page, on_domain_done)
            await engine.run()
    finally:
        if browser:
            await browser.close()
