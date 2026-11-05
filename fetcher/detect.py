"""Reads a response to decide: is this a bot wall, or a page that needs JavaScript?

The two lead to opposite actions. A JavaScript app is rendered in a browser. A bot wall
means the site said no, so we count a block and never try to get past it with the browser.
"""

from __future__ import annotations

import re
from collections.abc import Mapping

from selectolax.lexbor import LexborHTMLParser as HTMLParser

_TITLE_RE = re.compile(r"<title[^>]*>(.*?)</title>", re.I | re.S)
_WALL_TITLE_RE = re.compile(
    r"just a moment|attention required|access denied|are you (a )?human|are you a robot|"
    r"security check|captcha|verify you are|ddos protection|request blocked|pardon our interruption",
    re.I,
)
_SPA_ROOT_RE = re.compile(r"""<div[^>]+id=["'](?:root|app|__next|__nuxt|___gatsby)["']""", re.I)
_ENABLE_JS_RE = re.compile(r"(?:enable|requires?|need)\s+javascript|javascript\s+(?:is\s+)?(?:required|disabled)", re.I)
_MAX_WALL_TEXT = 2000


def is_bot_wall(headers: Mapping[str, str], text: str) -> bool:
    if "cf-mitigated" in headers:
        return True
    title = _TITLE_RE.search(text[:20000])
    if not title or not _WALL_TITLE_RE.search(title.group(1)):
        return False
    # Real articles can mention these words, but a wall page has almost no content.
    return _visible_length(text) < _MAX_WALL_TEXT


def needs_javascript(html: str, min_text_chars: int) -> bool:
    """True when the page has little visible text and shows signs of being built by scripts."""
    if _visible_length(html) >= min_text_chars:
        return False
    return bool(_SPA_ROOT_RE.search(html) or _ENABLE_JS_RE.search(html) or "<script" in html.lower())


def _visible_length(html: str) -> int:
    tree = HTMLParser(html)
    tree.strip_tags(["script", "style", "noscript", "template", "head"])
    body = tree.body
    return len(body.text(separator=" ", strip=True)) if body else 0
