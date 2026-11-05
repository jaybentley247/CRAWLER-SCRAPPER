"""Drops values that are the right shape but useless as lead data."""

from __future__ import annotations

import re

from extractors.email import normalize_email
from extractors.metadata import GENERIC_TITLE_PARTS
from models import SOCIAL_FIELDS, Fact

_E164_RE = re.compile(r"^\+[1-9]\d{6,14}$")
_JUNK_NAMES = GENERIC_TITLE_PARTS | {
    "just a moment...", "just a moment", "access denied", "attention required", "wordpress", "site title",
    "my site", "my blog", "my website", "coming soon", "under construction", "default web site page",
    "account suspended", "error", "forbidden", "not found",
}
_MIN_DESCRIPTION = 20


def is_valid_fact(fact: Fact) -> bool:
    value = fact.value
    if fact.field == "email":
        return normalize_email(value) == value
    if fact.field == "phone":
        return bool(_E164_RE.match(value))
    if fact.field == "name":
        return 2 <= len(value) <= 120 and value.lower() not in _JUNK_NAMES
    if fact.field == "address":
        return 10 <= len(value) <= 300
    if fact.field == "description":
        return len(value) >= _MIN_DESCRIPTION
    if fact.field == "website" or fact.field in SOCIAL_FIELDS:
        return value.startswith(("http://", "https://"))
    return bool(value)
