"""Finds crawlable links on a page and scores them so contact pages are visited first."""

from __future__ import annotations

import re
from dataclasses import dataclass
from urllib.parse import urlsplit

from crawler.urls import normalize_url, same_site, skip_reason, url_key
from extractors.page import ParsedPage

SEED_PRIORITY = 1000

_RULES: tuple[tuple[re.Pattern[str], int], ...] = (
    (re.compile(r"contact|reach-?us|get-?in-?touch|kontakt|contatti|contato", re.I), 100),
    # Many European countries legally require these pages to list the company's address and contacts.
    (re.compile(r"impressum|imprint|legal-?notice|mentions-?legales|aviso-?legal|note-?legali|colofon", re.I), 90),
    (re.compile(r"about|who-?we-?are|our-?story|company|profile|ueber-?uns|a-?propos|qui-?sommes|chi-?siamo|sobre|over-?ons", re.I), 80),
    (re.compile(r"team|staff|people|leadership|management", re.I), 60),
    (re.compile(r"location|branch|office|find-?us|store-?locator|directions", re.I), 50),
    (re.compile(r"service|what-?we-?do", re.I), 20),
    (re.compile(r"blog|news|article|press|tag|category|author|archive|page/\d+|\d{4}/\d{2}", re.I), -30),
)
_FOOTER_BONUS = 10


@dataclass(frozen=True)
class Link:
    url: str
    priority: int


def score_link(url: str, anchor_text: str = "", in_footer: bool = False) -> int:
    path = urlsplit(url).path
    best = 0
    for pattern, score in _RULES:
        if pattern.search(path) or pattern.search(anchor_text.replace(" ", "-")):
            best = score if best == 0 else max(best, score)
    if in_footer:
        best += _FOOTER_BONUS
    depth_penalty = path.strip("/").count("/") * 2
    return best - depth_penalty


def extract_links(page: ParsedPage, domain: str, limit: int) -> list[Link]:
    """Same-site, crawlable links, best first, at most `limit`."""
    best: dict[str, Link] = {}
    for anchor in page.anchors:
        url = normalize_url(anchor.href, base=page.base_url)
        if url is None or not same_site(url, domain) or skip_reason(url):
            continue
        link = Link(url, score_link(url, anchor.text, anchor.in_footer))
        key = url_key(url)
        if key not in best or link.priority > best[key].priority:
            best[key] = link
    return sorted(best.values(), key=lambda link: -link.priority)[:limit]
