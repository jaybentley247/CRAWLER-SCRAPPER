"""Runs every extractor over a page. Each fact keeps the URL it was found on."""

from __future__ import annotations

from collections.abc import Iterable

from extractors import address, email, jsonld, metadata, phone, socials
from extractors.page import ParsedPage
from models import Fact


def extract_all(
    page: ParsedPage, region: str, location_hints: list[str], fallback_regions: Iterable[str] = ()
) -> list[Fact]:
    return [
        *jsonld.extract(page, region),
        *metadata.extract(page),
        *email.extract(page),
        *phone.extract(page, region, fallback_regions),
        *socials.extract(page),
        *address.extract(page, location_hints),
    ]
