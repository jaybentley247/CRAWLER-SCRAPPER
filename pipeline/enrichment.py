"""Adds what can be worked out from the data: a fallback category, the country and a quality score."""

from __future__ import annotations

import re

import phonenumbers

from extractors.region import region_from_domain
from models import SOCIAL_FIELDS, Business, FieldValue
from settings import TargetSettings

_EMAIL_POINTS = {"role": 35, "named": 35, "freemail": 30, "external": 15}


def keyword_category(text: str, keywords: dict[str, list[str]]) -> str | None:
    """The category whose keywords appear most often in the text, if any do."""
    lowered = text.lower()
    best, best_hits = None, 0
    for category, words in keywords.items():
        # Plurals count too, so "contractor" also matches "contractors".
        hits = sum(len(re.findall(rf"\b{re.escape(word.lower())}(?:s|es)?\b", lowered)) for word in words)
        if hits > best_hits:
            best, best_hits = category, hits
    return best


def enrich(business: Business, target: TargetSettings) -> Business:
    if business.category is None:
        business.category = _infer_category(business, target)
    business.country = _infer_country(business)
    business.quality_score = quality_score(business)
    return business


def _infer_country(business: Business) -> str:
    """The main phone number is the best evidence, since its country code is unambiguous."""
    if business.phones:
        try:
            region = phonenumbers.region_code_for_number(phonenumbers.parse(business.phones[0].value))
        except phonenumbers.NumberParseException:
            region = None
        if region:
            return region
    return region_from_domain(business.domain) or ""


def _infer_category(business: Business, target: TargetSettings) -> FieldValue | None:
    text = " ".join(field.value for field in (business.name, business.description) if field)
    category = keyword_category(text, target.category_keywords)
    if category:
        source = business.description or business.name
        return FieldValue(value=category, source_url=source.source_url if source else "", confidence=0.4)
    if target.default_category:
        return FieldValue(value=target.default_category, source_url="config:target.default_category", confidence=0.2)
    return None


def quality_score(business: Business) -> int:
    """0-100: how ready this lead is for outreach. Contact details count the most."""
    score = 0
    if business.name:
        score += 10
    if business.emails:
        score += _EMAIL_POINTS.get(business.email_type, 15)
    if business.phones:
        score += 25
    if business.address:
        score += 10
    if any(getattr(business, social) for social in SOCIAL_FIELDS):
        score += 10
    if business.category:
        score += 5
    if business.description:
        score += 5
    return score
