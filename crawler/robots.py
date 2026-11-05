"""robots.txt handling, following RFC 9309. Always on, and there is no setting to disable it."""

from __future__ import annotations

import logging
from dataclasses import dataclass
from enum import Enum
from typing import Protocol

from protego import Protego

from crawler.urls import origin
from fetcher.types import FetchResult

log = logging.getLogger(__name__)

_MAX_ROBOTS_BYTES = 512_000


class RobotsVerdict(str, Enum):
    ALLOWED = "allowed"
    DISALLOWED = "disallowed"
    UNREACHABLE = "unreachable"


@dataclass(frozen=True)
class RobotsDecision:
    verdict: RobotsVerdict
    crawl_delay: float | None = None

    @property
    def allowed(self) -> bool:
        return self.verdict is RobotsVerdict.ALLOWED


class _Fetcher(Protocol):
    async def fetch(self, url: str, *, html_only: bool = ..., max_bytes: int | None = ...) -> FetchResult: ...


_ALLOW_ALL = Protego.parse("")


class RobotsCache:
    def __init__(self, fetcher: _Fetcher, agent: str):
        self._fetcher = fetcher
        self._agent = agent
        # Rules per origin. None means robots.txt couldn't be read.
        self._rules: dict[str, Protego | None] = {}

    async def check(self, url: str) -> RobotsDecision:
        site = origin(url)
        if site not in self._rules:
            self._rules[site] = await self._load(site)
        rules = self._rules[site]
        if rules is None:
            return RobotsDecision(RobotsVerdict.UNREACHABLE)
        if not rules.can_fetch(url, self._agent):
            return RobotsDecision(RobotsVerdict.DISALLOWED)
        return RobotsDecision(RobotsVerdict.ALLOWED, rules.crawl_delay(self._agent))

    async def _load(self, site: str) -> Protego | None:
        result = await self._fetcher.fetch(f"{site}/robots.txt", html_only=False, max_bytes=_MAX_ROBOTS_BYTES)
        status = result.status
        if status is None or status == 429 or status >= 500:
            # RFC 9309: if the server can't say what's allowed, assume nothing is.
            log.info("robots.txt unreachable for %s (%s)", site, status or result.error)
            return None
        if status != 200:
            return _ALLOW_ALL  # A missing robots.txt places no restrictions.
        try:
            return Protego.parse(result.text)
        except Exception as exc:
            log.warning("Could not parse robots.txt for %s: %s", site, exc)
            return None
