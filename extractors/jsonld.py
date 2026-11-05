"""Reads schema.org JSON-LD. Sites write this data on purpose, so it's the most reliable source."""

from __future__ import annotations

import json
import logging
import re
from collections.abc import Iterator
from typing import Any

from extractors import socials
from extractors.email import normalize_email
from extractors.page import ParsedPage
from extractors.phone import normalize_phone
from models import Fact

log = logging.getLogger(__name__)

_NON_BUSINESS_TYPES = {
    "Person", "WebSite", "WebPage", "AboutPage", "ContactPage", "CollectionPage", "ItemPage", "SearchResultsPage",
    "BreadcrumbList", "ListItem", "ItemList", "Article", "NewsArticle", "BlogPosting", "Product", "Offer",
    "AggregateOffer", "Event", "ImageObject", "VideoObject", "Review", "AggregateRating", "Rating", "FAQPage",
    "Question", "Answer", "HowTo", "Recipe", "SiteNavigationElement", "PostalAddress", "ContactPoint",
    "SearchAction", "Language", "Place", "Course", "JobPosting",
}
_BUSINESS_SUFFIX_RE = re.compile(r"(?:Business|Organization|Corporation|Store|Agent|Agency|Restaurant|Company|Service)$")
_GENERIC_CATEGORIES = {"Organization", "LocalBusiness", "Corporation", "Thing", "Store", "OnlineBusiness"}
_CAMEL_RE = re.compile(r"(?<=[a-z])(?=[A-Z])")
_CDATA_RE = re.compile(r"//\s*<!\[CDATA\[|//\s*\]\]>|<!\[CDATA\[|\]\]>")


def parse_blocks(blocks: list[str]) -> Iterator[dict[str, Any]]:
    """Every object in the JSON-LD blocks. Broken blocks are skipped: they're common in the wild."""
    for block in blocks:
        try:
            data = json.loads(_CDATA_RE.sub("", block).strip(), strict=False)
        except ValueError:
            log.debug("Skipping invalid JSON-LD block")
            continue
        yield from _flatten(data)


def _flatten(data: Any) -> Iterator[dict[str, Any]]:
    if isinstance(data, list):
        for item in data:
            yield from _flatten(item)
    elif isinstance(data, dict):
        yield data
        yield from _flatten(data.get("@graph"))
        yield from _flatten(data.get("mainEntity"))


def _types(node: dict[str, Any]) -> list[str]:
    raw = node.get("@type") or []
    values = raw if isinstance(raw, list) else [raw]
    return [str(value).rsplit("/", 1)[-1] for value in values]


def _is_business(node: dict[str, Any]) -> bool:
    types = _types(node)
    if not types or any(kind in _NON_BUSINESS_TYPES for kind in types):
        return False
    if any(_BUSINESS_SUFFIX_RE.search(kind) for kind in types):
        return True
    # schema.org has hundreds of business subtypes, like Dentist or Bakery. Contact details give them away.
    return "address" in node or "telephone" in node


def as_list(value: Any) -> list[Any]:
    if value is None:
        return []
    return value if isinstance(value, list) else [value]


def text_of(value: Any) -> str | None:
    if isinstance(value, dict):
        value = value.get("name") or value.get("@value")
    if isinstance(value, (str, int, float)) and not isinstance(value, bool):
        return str(value).strip() or None
    return None


def format_address(value: Any) -> str | None:
    if isinstance(value, str):
        return value.strip() or None
    if not isinstance(value, dict):
        return None
    keys = ("streetAddress", "addressLocality", "addressRegion", "postalCode", "addressCountry")
    parts = [text for key in keys if (text := text_of(value.get(key)))]
    return ", ".join(parts) or None


def extract(page: ParsedPage, region: str) -> list[Fact]:
    facts: dict[tuple[str, str], Fact] = {}

    def add(field: str, value: str | None, confidence: float = 0.95) -> None:
        if value:
            facts[(field, value)] = Fact(
                field=field, value=value, source_url=page.url, method="json_ld", confidence=confidence  # type: ignore[arg-type]
            )

    for node in parse_blocks(page.jsonld_blocks):
        if not _is_business(node):
            continue
        add("name", text_of(node.get("name")) or text_of(node.get("legalName")))
        add("description", text_of(node.get("description")), 0.9)
        for kind in _types(node):
            if kind not in _GENERIC_CATEGORIES:
                add("category", _CAMEL_RE.sub(" ", kind), 0.9)

        contacts = [node, *(c for c in as_list(node.get("contactPoint")) if isinstance(c, dict))]
        for contact in contacts:
            for raw in as_list(contact.get("email")):
                if (text := text_of(raw)):
                    add("email", normalize_email(text.removeprefix("mailto:")))
            for raw in as_list(contact.get("telephone")):
                if (text := text_of(raw)):
                    add("phone", normalize_phone(text.removeprefix("tel:"), region))

        for address in as_list(node.get("address")):
            add("address", format_address(address))
        for link in as_list(node.get("sameAs")):
            if isinstance(link, str) and (result := socials.canonicalize(link)):
                add(result[0], result[1])

    return list(facts.values())
