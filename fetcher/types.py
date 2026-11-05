"""The result every fetcher returns."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from enum import Enum


class FetchKind(str, Enum):
    OK = "ok"
    REDIRECT = "redirect"
    NOT_HTML = "not_html"
    CLIENT_ERROR = "client_error"
    BLOCKED = "blocked"
    SERVER_ERROR = "server_error"
    NETWORK_ERROR = "network_error"
    TOO_LARGE = "too_large"
    TOO_MANY_REDIRECTS = "too_many_redirects"
    OFFSITE_REDIRECT = "offsite_redirect"
    ROBOTS_DISALLOWED = "robots_disallowed"
    SKIPPED_REDIRECT = "skipped_redirect"
    FORBIDDEN_HOST = "forbidden_host"


# Results suggesting the site is unwell or doesn't want us. Enough in a row and we stop.
COUNTS_AS_BLOCK = {FetchKind.BLOCKED, FetchKind.SERVER_ERROR, FetchKind.NETWORK_ERROR}

# Given a redirect target, returns a reason not to follow it, or None to follow.
RedirectCheck = Callable[[str], Awaitable["FetchKind | None"]]


@dataclass(frozen=True)
class FetchResult:
    url: str
    kind: FetchKind
    final_url: str = ""
    status: int | None = None
    text: str = ""
    body: bytes = b""  # The raw bytes, kept only when html_only is off, for files such as PDFs.
    error: str | None = None
    location: str | None = None
    retry_after: float | None = None
    retryable: bool = False
    rendered: bool = False
