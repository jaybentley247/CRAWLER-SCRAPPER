"""Per-site pacing: one request at a time, with a random pause between them."""

from __future__ import annotations

import asyncio
import random
import time
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager


class DomainRateLimiter:
    def __init__(
        self,
        min_delay: float,
        max_delay: float,
        *,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
        rng: random.Random | None = None,
    ):
        self.min_delay = min_delay
        self.max_delay = max_delay
        self._clock = clock
        self._sleep = sleep
        self._rng = rng or random.Random()
        self._locks: dict[str, asyncio.Lock] = {}
        self._next_allowed: dict[str, float] = {}
        self._crawl_delays: dict[str, float] = {}

    def set_crawl_delay(self, domain: str, seconds: float) -> None:
        """Apply a robots.txt Crawl-delay. It can only slow us down, never speed us up."""
        self._crawl_delays[domain] = max(seconds, self._crawl_delays.get(domain, 0.0))

    @asynccontextmanager
    async def slot(self, domain: str) -> AsyncIterator[None]:
        """Hold the site's only request slot. The pause is timed from when the response ends."""
        lock = self._locks.setdefault(domain, asyncio.Lock())
        async with lock:
            wait = self._next_allowed.get(domain, 0.0) - self._clock()
            if wait > 0:
                await self._sleep(wait)
            try:
                yield
            finally:
                self._next_allowed[domain] = self._clock() + self._next_delay(domain)

    def _next_delay(self, domain: str) -> float:
        floor = self._crawl_delays.get(domain, 0.0)
        return self._rng.uniform(max(self.min_delay, floor), max(self.max_delay, floor))
