"""Tidies raw values so the same fact always looks the same."""

from __future__ import annotations

import html
import re
import unicodedata

from models import Fact

_CONTROL_RE = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f-\x9f​-‏ - ﻿]")
_MAX_DESCRIPTION = 300
_MAX_TEXT = 500


def clean_text(value: str) -> str:
    text = html.unescape(unicodedata.normalize("NFKC", value))
    text = _CONTROL_RE.sub(" ", text)
    return re.sub(r"\s+", " ", text).strip()


def shorten(text: str, limit: int = _MAX_DESCRIPTION) -> str:
    """Cut at a word boundary, so descriptions don't end mid-word."""
    if len(text) <= limit:
        return text
    return text[:limit].rsplit(" ", 1)[0].rstrip(" ,;:.-") + "…"


def clean_fact(fact: Fact) -> Fact | None:
    value = clean_text(fact.value)
    if fact.field == "description":
        value = shorten(value)
    elif fact.field == "name":
        value = value.strip(" -|,.:;")
    elif fact.field == "address":
        value = value.strip(" ,;")
    value = value[:_MAX_TEXT]
    if not value:
        return None
    return fact if value == fact.value else fact.model_copy(update={"value": value})
