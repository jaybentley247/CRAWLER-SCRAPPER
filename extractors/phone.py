"""Finds phone numbers and normalises them to E.164 with the phonenumbers library."""

from __future__ import annotations

from collections.abc import Iterable
from urllib.parse import parse_qs, unquote, urlsplit

import phonenumbers
from phonenumbers import PhoneNumberFormat, PhoneNumberMatcher

from extractors.page import ParsedPage
from models import Fact

_MAX_TEXT_CHARS = 200_000
_FALLBACK_SCALE = 0.7
_WHATSAPP_HOSTS = {"wa.me", "api.whatsapp.com", "web.whatsapp.com", "chat.whatsapp.com"}


def normalize_phone(raw: str, region: str) -> str | None:
    try:
        number = phonenumbers.parse(unquote(raw).strip(), region)
    except phonenumbers.NumberParseException:
        return None
    if not phonenumbers.is_valid_number(number):
        return None
    return phonenumbers.format_number(number, PhoneNumberFormat.E164)


def extract(page: ParsedPage, region: str, fallback_regions: Iterable[str] = ()) -> list[Fact]:
    """Read numbers using `region`. If that finds none, try each fallback with less confidence."""
    found: dict[str, tuple[str, float]] = {}

    def scan(current: str, scale: float) -> None:
        def add(number: str | None, method: str, confidence: float) -> None:
            confidence *= scale
            if number and (number not in found or found[number][1] < confidence):
                found[number] = (method, confidence)

        for anchor in page.anchors:
            href = anchor.href.strip()
            if href.lower().startswith("tel:"):
                add(normalize_phone(href[4:], current), "tel_link", 0.9)
            elif (whatsapp := _whatsapp_number(href)) is not None:
                add(normalize_phone(whatsapp, current), "whatsapp_link", 0.75)

        for match in PhoneNumberMatcher(page.flat_text[:_MAX_TEXT_CHARS], current):
            add(phonenumbers.format_number(match.number, PhoneNumberFormat.E164), "text", 0.7)

    scan(region, 1.0)
    for fallback in fallback_regions:
        if found:
            break
        scan(fallback, _FALLBACK_SCALE)

    return [
        Fact(field="phone", value=number, source_url=page.url, method=method, confidence=confidence)
        for number, (method, confidence) in found.items()
    ]


def _whatsapp_number(href: str) -> str | None:
    """WhatsApp links always use international format, without the leading +."""
    try:
        parts = urlsplit(href)
    except ValueError:
        return None
    if (parts.hostname or "").lower().removeprefix("www.") not in _WHATSAPP_HOSTS:
        return None
    digits = parts.path.strip("/") if parts.hostname and parts.hostname.lower().endswith("wa.me") else ""
    digits = digits or parse_qs(parts.query).get("phone", [""])[0]
    digits = digits.strip().lstrip("+").replace(" ", "")
    return f"+{digits}" if digits.isdigit() else None
