"""Stops a website from pointing the crawler at machines on your own network."""

from __future__ import annotations

import asyncio
import ipaddress
import socket
from urllib.parse import urlsplit


class HostGuard:
    def __init__(self, allow_private: bool = False):
        self.allow_private = allow_private
        self._cache: dict[str, bool] = {}

    async def allowed(self, url: str) -> bool:
        if self.allow_private:
            return True
        host = urlsplit(url).hostname or ""
        if host not in self._cache:
            self._cache[host] = await self._resolves_publicly(host)
        return self._cache[host]

    async def _resolves_publicly(self, host: str) -> bool:
        if host == "localhost" or host.endswith((".localhost", ".local", ".internal")):
            return False
        try:
            return ipaddress.ip_address(host).is_global
        except ValueError:
            pass
        try:
            infos = await asyncio.get_running_loop().getaddrinfo(host, None, type=socket.SOCK_STREAM)
        except OSError:
            return True  # DNS failed, so the request itself will fail and be recorded normally.
        return all(ipaddress.ip_address(info[4][0].split("%")[0]).is_global for info in infos)
