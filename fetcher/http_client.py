"""The main fetcher: httpx with pacing, retries, backoff and checked redirects."""

from __future__ import annotations

import asyncio
import logging
import random
import re
from collections.abc import Awaitable, Callable
from dataclasses import replace
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime

import httpx

from crawler.urls import normalize_url, registrable_domain
from fetcher.detect import is_bot_wall
from fetcher.rate_limit import DomainRateLimiter
from fetcher.safety import HostGuard
from fetcher.types import FetchKind, FetchResult, RedirectCheck
from settings import Settings

log = logging.getLogger(__name__)

_HTML_TYPES = ("text/html", "application/xhtml+xml")
_META_CHARSET_RE = re.compile(rb"""<meta[^>]+charset=["']?\s*([\w-]+)""", re.I)


class HttpFetcher:
    def __init__(
        self,
        settings: Settings,
        limiter: DomainRateLimiter,
        guard: HostGuard,
        *,
        transport: httpx.AsyncBaseTransport | None = None,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
        rng: random.Random | None = None,
    ):
        self.settings = settings
        self.limiter = limiter
        self.guard = guard
        self._sleep = sleep
        self._rng = rng or random.Random()
        self._client = httpx.AsyncClient(
            headers={
                "User-Agent": settings.identity.user_agent,
                "From": settings.identity.contact_email,
                "Accept": "text/html,application/xhtml+xml;q=0.9,*/*;q=0.5",
            },
            timeout=settings.fetch.timeout,
            follow_redirects=False,  # We follow them ourselves so each hop gets checked.
            transport=transport,
        )

    async def __aenter__(self) -> "HttpFetcher":
        return self

    async def __aexit__(self, *_exc: object) -> None:
        await self.close()

    async def close(self) -> None:
        await self._client.aclose()

    async def fetch(
        self,
        url: str,
        *,
        check_redirect: RedirectCheck | None = None,
        html_only: bool = True,
        max_bytes: int | None = None,
    ) -> FetchResult:
        """Fetch a URL, following redirects that pass `check_redirect`."""
        limit = max_bytes or self.settings.fetch.max_bytes
        current = url
        for _hop in range(self.settings.fetch.max_redirects + 1):
            result = await self._fetch_with_retries(current, html_only, limit)
            if result.kind is not FetchKind.REDIRECT:
                return replace(result, url=url, final_url=current)

            target = normalize_url(result.location or "", base=current)
            if target is None:
                return replace(result, url=url, final_url=current, kind=FetchKind.CLIENT_ERROR, error="bad_redirect")
            if check_redirect and (refusal := await check_redirect(target)):
                return replace(result, url=url, final_url=target, kind=refusal, error=f"redirect to {target}")
            current = target
        return FetchResult(url=url, final_url=current, kind=FetchKind.TOO_MANY_REDIRECTS, error="too_many_redirects")

    async def _fetch_with_retries(self, url: str, html_only: bool, limit: int) -> FetchResult:
        if not await self.guard.allowed(url):
            return FetchResult(url=url, kind=FetchKind.FORBIDDEN_HOST, error="host is not public")

        polite = self.settings.politeness
        domain = registrable_domain(url)
        attempt = 0
        while True:
            async with self.limiter.slot(domain):
                result = await self._get_once(url, html_only, limit)
            if not result.retryable or attempt >= polite.max_retries:
                return result

            wait = self._backoff(attempt)
            if result.retry_after is not None:
                if result.retry_after > polite.max_retry_after:
                    log.info("%s asked us to wait %.0fs. Giving up on this page.", url, result.retry_after)
                    return result
                wait = max(wait, result.retry_after)
            log.debug("Retrying %s in %.1fs (%s)", url, wait, result.status or result.error)
            await self._sleep(wait)
            attempt += 1

    def _backoff(self, attempt: int) -> float:
        polite = self.settings.politeness
        return min(polite.backoff_max, polite.backoff_base * 2**attempt) * self._rng.uniform(1.0, 1.25)

    async def _get_once(self, url: str, html_only: bool, limit: int) -> FetchResult:
        try:
            async with self._client.stream("GET", url) as response:
                return await self._read(url, response, html_only, limit)
        except httpx.ConnectError as exc:
            # DNS failures and bad certificates won't fix themselves within seconds.
            return FetchResult(url=url, kind=FetchKind.NETWORK_ERROR, error=_describe(exc))
        except httpx.TransportError as exc:
            return FetchResult(url=url, kind=FetchKind.NETWORK_ERROR, error=_describe(exc), retryable=True)
        except (httpx.HTTPError, httpx.InvalidURL) as exc:
            return FetchResult(url=url, kind=FetchKind.NETWORK_ERROR, error=_describe(exc))

    async def _read(self, url: str, response: httpx.Response, html_only: bool, limit: int) -> FetchResult:
        status = response.status_code
        if response.has_redirect_location:
            return FetchResult(url=url, kind=FetchKind.REDIRECT, status=status, location=response.headers["location"])

        content_type = response.headers.get("content-type", "").split(";")[0].strip().lower()
        if html_only and response.is_success and content_type and content_type not in _HTML_TYPES:
            return FetchResult(url=url, kind=FetchKind.NOT_HTML, status=status, error=content_type)

        declared = response.headers.get("content-length", "")
        if declared.isdigit() and int(declared) > limit:
            return FetchResult(url=url, kind=FetchKind.TOO_LARGE, status=status, error="too_large")
        body = bytearray()
        async for chunk in response.aiter_bytes():
            body.extend(chunk)
            if len(body) > limit:
                return FetchResult(url=url, kind=FetchKind.TOO_LARGE, status=status, error="too_large")

        text = _decode(bytes(body), response.charset_encoding)
        result = self._classify(url, response, status, text, html_only)
        if not html_only and result.kind is FetchKind.OK:
            return replace(result, body=bytes(body))
        return result

    def _classify(self, url: str, response: httpx.Response, status: int, text: str, html_only: bool) -> FetchResult:
        if html_only and is_bot_wall(response.headers, text):
            return FetchResult(url=url, kind=FetchKind.BLOCKED, status=status, error="bot_wall")
        if 200 <= status < 300:
            return FetchResult(url=url, kind=FetchKind.OK, status=status, text=text)

        retry_after = _parse_retry_after(response.headers.get("retry-after"))
        if status == 429:
            return FetchResult(
                url=url, kind=FetchKind.BLOCKED, status=status, error="http_429", retryable=True, retry_after=retry_after
            )
        if status == 403:
            return FetchResult(url=url, kind=FetchKind.BLOCKED, status=status, error="http_403")
        if status >= 500:
            return FetchResult(
                url=url, kind=FetchKind.SERVER_ERROR, status=status, error=f"http_{status}",
                retryable=True, retry_after=retry_after,
            )
        return FetchResult(url=url, kind=FetchKind.CLIENT_ERROR, status=status, error=f"http_{status}")


def _describe(exc: Exception) -> str:
    return f"{type(exc).__name__}: {exc}"[:200]


def _decode(body: bytes, declared: str | None) -> str:
    candidates = [declared]
    match = _META_CHARSET_RE.search(body[:2048])
    if match:
        candidates.append(match.group(1).decode("ascii", "ignore"))
    for encoding in candidates:
        if encoding:
            try:
                return body.decode(encoding)
            except (LookupError, UnicodeDecodeError):
                continue
    return body.decode("utf-8", "replace")


def _parse_retry_after(value: str | None) -> float | None:
    if not value:
        return None
    value = value.strip()
    if value.isdigit():
        return float(value)
    try:
        when = parsedate_to_datetime(value)
    except (TypeError, ValueError):
        return None
    if when.tzinfo is None:
        when = when.replace(tzinfo=timezone.utc)
    return max(0.0, (when - datetime.now(timezone.utc)).total_seconds())
