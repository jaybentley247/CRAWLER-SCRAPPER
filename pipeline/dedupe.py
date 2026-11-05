"""Consolidation and deduplication.

Within a site: many sightings of a value become one best value per field.
Across sites: the same business under two domains merges only when both a contact detail
and the name agree. A shared phone alone could just be the same web designer, so that
gets flagged for a person to check rather than merged.
"""

from __future__ import annotations

import re
from collections import defaultdict
from difflib import SequenceMatcher

from extractors.email import classify_email
from models import SOCIAL_FIELDS, Business, Fact, FieldValue
from pipeline.enrichment import quality_score

_SINGLE_FIELDS = ("name", "website", "address", "category", "description", *SOCIAL_FIELDS)
_EMAIL_RANK = {"role": 0, "named": 1, "freemail": 2, "external": 3}
_REPEAT_BONUS = 0.03
_MAX_REPEAT_BONUS_STEPS = 4
_NAME_NOISE_RE = re.compile(
    r"\b(?:ltd|limited|plc|inc|incorporated|llc|llp|corp|corporation|company|co|the|enterprises?|"
    r"gmbh|ag|kg|sarl|sas|srl|spa|bv|nv|oy|ab|ulc)\b\.?",
    re.I,
)
_SIMILAR_NAME_RATIO = 0.85


def _rank_values(facts: list[Fact]) -> list[FieldValue]:
    """One entry per distinct value, best first. Seeing a value on more pages raises its score."""
    groups: dict[str, list[Fact]] = defaultdict(list)
    for fact in facts:
        groups[fact.value.casefold()].append(fact)

    ranked: list[FieldValue] = []
    for sightings in groups.values():
        best = max(sightings, key=lambda fact: fact.confidence)
        repeats = min(len({fact.source_url for fact in sightings}) - 1, _MAX_REPEAT_BONUS_STEPS)
        ranked.append(
            FieldValue(value=best.value, source_url=best.source_url, confidence=best.confidence + repeats * _REPEAT_BONUS)
        )
    ranked.sort(key=lambda field: -field.confidence)
    return ranked


def consolidate(domain: str, facts: list[Fact]) -> Business:
    by_field: dict[str, list[Fact]] = defaultdict(list)
    for fact in facts:
        by_field[fact.field].append(fact)

    business = Business(domain=domain)
    for field in _SINGLE_FIELDS:
        ranked = _rank_values(by_field[field])
        if ranked:
            setattr(business, field, ranked[0])

    # The address most useful for outreach comes first, however many pages mention the others.
    emails = _rank_values(by_field["email"])
    emails.sort(key=lambda email: (_EMAIL_RANK[classify_email(email.value, domain)], -email.confidence))
    business.emails = emails
    business.email_type = classify_email(emails[0].value, domain) if emails else ""
    business.phones = _rank_values(by_field["phone"])
    return business


def _comparable_name(business: Business) -> str:
    if not business.name:
        return ""
    return re.sub(r"[^a-z0-9]", "", _NAME_NOISE_RE.sub("", business.name.value.lower()))


def _similar_names(a: Business, b: Business) -> bool:
    name_a, name_b = _comparable_name(a), _comparable_name(b)
    if not name_a or not name_b:
        return False
    return name_a == name_b or SequenceMatcher(None, name_a, name_b).ratio() >= _SIMILAR_NAME_RATIO


def dedupe_businesses(businesses: list[Business]) -> list[Business]:
    businesses = sorted(businesses, key=lambda business: (-business.quality_score, business.domain))
    parent = list(range(len(businesses)))

    def find(index: int) -> int:
        while parent[index] != index:
            parent[index] = parent[parent[index]]
            index = parent[index]
        return index

    by_contact: dict[str, list[int]] = defaultdict(list)
    for index, business in enumerate(businesses):
        for contact in {field.value for field in [*business.emails, *business.phones]}:
            by_contact[contact].append(index)

    possible: dict[int, set[int]] = defaultdict(set)
    for indexes in by_contact.values():
        for position, first in enumerate(indexes):
            for second in indexes[position + 1:]:
                if _similar_names(businesses[first], businesses[second]):
                    # Sorted best-first, so the smaller index is the better record to keep.
                    root_a, root_b = find(first), find(second)
                    parent[max(root_a, root_b)] = min(root_a, root_b)
                else:
                    possible[first].add(second)
                    possible[second].add(first)

    survivors: list[Business] = []
    for index, business in enumerate(businesses):
        root = find(index)
        if root == index:
            survivors.append(business)
        else:
            _merge_into(businesses[root], business)

    for index, business in enumerate(businesses):
        if find(index) != index:
            continue
        related = {businesses[find(other)].domain for other in possible[index]} - {business.domain}
        business.possible_duplicate_of = sorted(related)
    for business in survivors:
        business.quality_score = quality_score(business)
    return survivors


def _merge_into(keeper: Business, other: Business) -> None:
    keeper.merged_domains = sorted({*keeper.merged_domains, other.domain})
    keeper.email_type = keeper.email_type or other.email_type
    for attribute in ("emails", "phones"):
        current: list[FieldValue] = getattr(keeper, attribute)
        known = {field.value for field in current}
        current.extend(field for field in getattr(other, attribute) if field.value not in known)
    for field in (*SOCIAL_FIELDS, "address", "category", "description"):
        if getattr(keeper, field) is None:
            setattr(keeper, field, getattr(other, field))
