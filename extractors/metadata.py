"""Business name and short description from Open Graph tags, the title, logo and meta tags."""

from __future__ import annotations

import re
from difflib import SequenceMatcher

from extractors.page import ParsedPage
from models import Fact

_TITLE_SPLIT_RE = re.compile(r"\s+[|\-–—:•·»]+\s+|\s*\|\s*")
_WELCOME_RE = re.compile(r"^\s*(?:welcome\s+to|home\s*(?:page)?\s+of)\s+", re.I)
_LOGO_WORD_RE = re.compile(r"\b(?:logo|icon|image|header|footer)\b", re.I)
GENERIC_TITLE_PARTS = {
    "home", "homepage", "home page", "welcome", "contact", "contact us", "about", "about us", "our team",
    "team", "services", "our services", "blog", "news", "gallery", "faq", "faqs", "privacy policy",
    "terms", "index", "official site", "official website", "untitled", "page not found", "404",
    "kontakt", "startseite", "impressum", "über uns", "ueber uns", "accueil", "contactez-nous", "à propos",
    "inicio", "contacto", "chi siamo", "contatti",
}
_LOGO_SELECTOR = "img[class*='logo' i], img[id*='logo' i], [class*='logo' i] img, [id*='logo' i] img, header a img"
_MIN_PARAGRAPH = 60


def name_from_title(title: str, domain: str) -> str | None:
    """Pick the business name out of a title like 'Contact Us | Acme Realty'."""
    parts = [_WELCOME_RE.sub("", part).strip() for part in _TITLE_SPLIT_RE.split(title)]
    candidates = [part for part in parts if part and part.lower() not in GENERIC_TITLE_PARTS]
    if not candidates:
        return None
    if len(candidates) == 1:
        return candidates[0]

    # With several parts, the one that looks most like the domain is usually the name.
    label = re.sub(r"[^a-z0-9]", "", domain.split(".")[0].lower())

    def resemblance(part: str) -> float:
        squashed = re.sub(r"[^a-z0-9]", "", part.lower())
        return SequenceMatcher(None, squashed, label).ratio()

    return max(candidates, key=resemblance)


def extract(page: ParsedPage) -> list[Fact]:
    facts: list[Fact] = []

    def add(field: str, value: str | None, method: str, confidence: float) -> None:
        if value and value.strip():
            facts.append(
                Fact(field=field, value=value.strip(), source_url=page.url, method=method, confidence=confidence)  # type: ignore[arg-type]
            )

    add("name", page.metas.get("og:site_name"), "og_site_name", 0.85)
    add("name", _logo_alt(page), "logo_alt", 0.55)
    if page.title:
        add("name", name_from_title(page.title, page.domain), "title", 0.6 if page.is_homepage else 0.4)

    # A homepage describes the business. Other pages describe themselves.
    scale = 1.0 if page.is_homepage else 0.5
    add("description", page.metas.get("og:description"), "og_description", 0.75 * scale)
    add("description", page.metas.get("description"), "meta_description", 0.7 * scale)
    if page.is_homepage:
        add("description", _first_paragraph(page), "first_paragraph", 0.3)
    return facts


def _logo_alt(page: ParsedPage) -> str | None:
    node = page.tree.css_first(_LOGO_SELECTOR)
    alt = (node.attributes.get("alt") or "") if node else ""
    cleaned = re.sub(r"\s+", " ", _LOGO_WORD_RE.sub(" ", alt)).strip(" -|,")
    return cleaned or None


def _first_paragraph(page: ParsedPage) -> str | None:
    for node in page.tree.css("p"):
        text = node.text(separator=" ", strip=True)
        if len(text) >= _MIN_PARAGRAPH:
            return text
    return None
