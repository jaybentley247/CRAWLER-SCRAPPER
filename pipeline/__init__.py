"""Turns raw facts into finished leads: clean, validate, consolidate, enrich, deduplicate."""

from __future__ import annotations

from collections.abc import Mapping

from models import Business, Fact
from pipeline.cleaning import clean_fact
from pipeline.dedupe import consolidate, dedupe_businesses
from pipeline.enrichment import enrich
from pipeline.validation import is_valid_fact
from settings import TargetSettings


def build_leads(
    facts_by_domain: Mapping[str, list[Fact]],
    target: TargetSettings,
    statuses: Mapping[str, str] | None = None,
) -> list[Business]:
    statuses = statuses or {}
    businesses = []
    for domain, facts in facts_by_domain.items():
        cleaned = [fact for fact in map(clean_fact, facts) if fact and is_valid_fact(fact)]
        business = consolidate(domain, cleaned)
        business.crawl_status = statuses.get(domain, "")
        businesses.append(enrich(business, target))
    return dedupe_businesses(businesses)
