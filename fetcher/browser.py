"""Playwright fallback, used only when a page's content is built by JavaScript.

It's far heavier than httpx, so the browser only starts if a page actually needs it.
It sends the same honest User-Agent and isn't used to get around bot walls.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

from crawler.urls import normalize_url, registrable_domain, same_site
from fetcher.detect import is_bot_wall
from fetcher.rate_limit import DomainRateLimiter
from fetcher.safety import HostGuard
from fetcher.types import FetchKind, FetchResult, RedirectCheck
from settings import Settings

log = logging.getLogger(__name__)

_UNNEEDED_RESOURCES = {"image", "media", "font"}
_SETTLE_MS = 5000


class BrowserFetcher:
    def __init__(self, settings: Settings, limiter: DomainRateLimiter, guard: HostGuard):
        self.settings = settings
        self.limiter = limiter
        self.guard = guard
        self._semaphore = asyncio.Semaphore(settings.browser.concurrency)
        self._start_lock = asyncio.Lock()
        self._playwright: Any = None
        self._browser: Any = None
        self.available = True

    async def _ensure_started(self) -> bool:
        async with self._start_lock:
            if self._browser or not self.available:
                return self.available
            try:
                from playwright.async_api import async_playwright

                self._playwright = await async_playwright().start()
                self._browser = await self._playwright.chromium.launch(headless=True)
            except Exception as exc:  # A missing package or browser must not stop the crawl.
                self.available = False
                log.warning(
                    "Browser fallback is off, so JavaScript-only pages will be thin (%s). "
                    "To enable it: python -m playwright install chromium",
                    str(exc).splitlines()[0] if str(exc) else type(exc).__name__,
                )
                await self.close()
            return self.available

    async def close(self) -> None:
        browser, playwright = self._browser, self._playwright
        self._browser = self._playwright = None
        if browser:
            await browser.close()
        if playwright:
            await playwright.stop()

    async def fetch(self, url: str, check_redirect: RedirectCheck | None = None) -> FetchResult:
        if not await self._ensure_started():
            return FetchResult(url=url, final_url=url, kind=FetchKind.NETWORK_ERROR, error="browser_unavailable")

        domain = registrable_domain(url)
        async with self._semaphore, self.limiter.slot(domain):
            context = await self._browser.new_context(
                user_agent=self.settings.identity.user_agent,
                extra_http_headers={"From": self.settings.identity.contact_email},
                accept_downloads=False,
                service_workers="block",
            )
            try:
                return await self._render(context, url, domain, check_redirect)
            except Exception as exc:
                return FetchResult(
                    url=url, final_url=url, kind=FetchKind.NETWORK_ERROR, error=f"{type(exc).__name__}: {exc}"[:200]
                )
            finally:
                await context.close()

    async def _render(self, context: Any, url: str, domain: str, check_redirect: RedirectCheck | None) -> FetchResult:
        page = await context.new_page()

        async def route_request(route: Any) -> None:
            request = route.request
            if request.resource_type in _UNNEEDED_RESOURCES or not await self.guard.allowed(request.url):
                await route.abort()
            elif request.is_navigation_request() and request.frame == page.main_frame and not same_site(request.url, domain):
                await route.abort()
            else:
                await route.continue_()

        await page.route("**/*", route_request)
        timeout_ms = self.settings.browser.timeout * 1000
        response = await page.goto(url, wait_until="domcontentloaded", timeout=timeout_ms)
        try:
            await page.wait_for_load_state("networkidle", timeout=_SETTLE_MS)
        except Exception:  # Pages with constant background traffic never go idle. That's fine.
            pass

        # Playwright's route handler doesn't see redirects, so check where we ended up.
        final_url = normalize_url(page.url) or url
        if check_redirect and final_url != url and (refusal := await check_redirect(final_url)):
            return FetchResult(url=url, final_url=final_url, kind=refusal, error=f"redirect to {final_url}")

        status = response.status if response else None
        html = await page.content()
        headers = dict(response.headers) if response else {}
        if is_bot_wall(headers, html) or status in {403, 429}:
            return FetchResult(url=url, final_url=final_url, kind=FetchKind.BLOCKED, status=status, error="bot_wall")
        if status is not None and status >= 400:
            kind = FetchKind.SERVER_ERROR if status >= 500 else FetchKind.CLIENT_ERROR
            return FetchResult(url=url, final_url=final_url, kind=kind, status=status, error=f"http_{status}")
        return FetchResult(url=url, final_url=final_url, kind=FetchKind.OK, status=status, text=html, rendered=True)
