"""Finds social profile links and reduces them to a clean profile URL.

Share buttons, single posts and login pages get rejected. A business's profile is what we want.
"""

from __future__ import annotations

import re
from urllib.parse import parse_qs, urlsplit

from extractors.page import ParsedPage
from models import Fact

_FACEBOOK_RESERVED = {
    "sharer", "sharer.php", "share", "share.php", "dialog", "plugins", "tr", "login", "login.php", "photo",
    "photo.php", "watch", "events", "hashtag", "policies", "help", "privacy", "legal", "story.php",
    "permalink.php", "home.php", "gaming", "ads", "business", "groups", "recover", "reg", "pg",
}
_INSTAGRAM_RESERVED = {"p", "reel", "reels", "tv", "explore", "accounts", "stories", "about", "developer", "direct", "legal"}
_X_RESERVED = {
    "intent", "share", "home", "search", "hashtag", "i", "login", "signup", "explore", "settings",
    "privacy", "tos", "about", "notifications", "messages", "compose",
}
_HANDLE_RE = re.compile(r"^[A-Za-z0-9._-]+$")
_X_HANDLE_RE = re.compile(r"^[A-Za-z0-9_]{1,15}$")
_INSTAGRAM_HANDLE_RE = re.compile(r"^[A-Za-z0-9._]{1,30}$")


def canonicalize(url: str) -> tuple[str, str] | None:
    """Return (network, clean profile URL), or None if this isn't a profile link."""
    try:
        parts = urlsplit(url if "://" in url else f"https://{url.lstrip('/')}")
        host = (parts.hostname or "").lower()
    except ValueError:
        return None
    segments = [segment for segment in parts.path.split("/") if segment]

    if _host_is(host, "facebook.com", "fb.com"):
        return _facebook(segments, parts.query)
    if _host_is(host, "instagram.com"):
        return _instagram(segments)
    if _host_is(host, "linkedin.com"):
        return _linkedin(segments)
    if _host_is(host, "twitter.com", "x.com"):
        return _x(segments)
    if _host_is(host, "tiktok.com"):
        return _tiktok(segments)
    return None


def _host_is(host: str, *domains: str) -> bool:
    return any(host == domain or host.endswith(f".{domain}") for domain in domains)


def _facebook(segments: list[str], query: str) -> tuple[str, str] | None:
    if not segments:
        return None
    first = segments[0].lower()
    if first == "profile.php":
        profile_id = parse_qs(query).get("id", [""])[0]
        return ("facebook", f"https://facebook.com/profile.php?id={profile_id}") if profile_id.isdigit() else None
    if first == "pages" and len(segments) >= 2:
        return "facebook", "https://facebook.com/" + "/".join(segments[:3])
    if first == "people" and len(segments) >= 3:
        return "facebook", "https://facebook.com/" + "/".join(segments[:3])
    if first in _FACEBOOK_RESERVED or not _HANDLE_RE.match(segments[0]):
        return None
    return "facebook", f"https://facebook.com/{segments[0]}"


def _instagram(segments: list[str]) -> tuple[str, str] | None:
    if segments and segments[0] == "_u":
        segments = segments[1:]
    if not segments or segments[0].lower() in _INSTAGRAM_RESERVED or not _INSTAGRAM_HANDLE_RE.match(segments[0]):
        return None
    return "instagram", f"https://instagram.com/{segments[0].lower()}"


def _linkedin(segments: list[str]) -> tuple[str, str] | None:
    if len(segments) < 2 or segments[0].lower() not in {"company", "school", "showcase", "in"}:
        return None
    return "linkedin", f"https://linkedin.com/{segments[0].lower()}/{segments[1]}"


def _x(segments: list[str]) -> tuple[str, str] | None:
    if not segments:
        return None
    handle = segments[0].lstrip("@")
    if handle.lower() in _X_RESERVED or not _X_HANDLE_RE.match(handle):
        return None
    return "x", f"https://x.com/{handle}"


def _tiktok(segments: list[str]) -> tuple[str, str] | None:
    if not segments or not segments[0].startswith("@") or len(segments[0]) < 2:
        return None
    return "tiktok", f"https://tiktok.com/{segments[0].lower()}"


def extract(page: ParsedPage) -> list[Fact]:
    found: dict[str, tuple[str, float]] = {}
    for anchor in page.anchors:
        result = canonicalize(anchor.href) if "://" in anchor.href or anchor.href.startswith("//") else None
        if result is None:
            continue
        network, url = result
        confidence = 0.8 if anchor.in_footer else 0.7
        # A company page represents the business better than one person's profile.
        if "linkedin.com/in/" in url:
            confidence -= 0.2
        if url not in found or found[url][1] < confidence:
            found[url] = (network, confidence)

    return [
        Fact(field=network, value=url, source_url=page.url, method="link", confidence=confidence)  # type: ignore[arg-type]
        for url, (network, confidence) in found.items()
    ]
