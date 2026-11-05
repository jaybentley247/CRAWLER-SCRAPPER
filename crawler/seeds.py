"""Reads starting URLs from a single URL, a .txt file or a .csv file."""

from __future__ import annotations

import csv
from dataclasses import dataclass
from pathlib import Path

from crawler.urls import has_public_suffix, normalize_url, registrable_domain, skip_reason
from extractors.region import country_code

_URL_COLUMNS = ("url", "website", "site", "domain", "link")


@dataclass(frozen=True)
class Seed:
    url: str
    domain: str
    name: str | None = None
    category: str | None = None
    country: str | None = None


@dataclass(frozen=True)
class RejectedSeed:
    raw: str
    reason: str


def parse_seed(
    raw: str,
    skip_domains: list[str],
    allow_private_hosts: bool = False,
    name: str | None = None,
    category: str | None = None,
    country: str | None = None,
) -> Seed | RejectedSeed:
    text = raw.strip()
    url = normalize_url(text if "://" in text else f"https://{text}")
    if url is None:
        return RejectedSeed(raw, "not a valid web address")
    if not has_public_suffix(url) and not allow_private_hosts:
        return RejectedSeed(raw, "not a public web address")
    domain = registrable_domain(url)
    if domain in {d.lower() for d in skip_domains}:
        return RejectedSeed(raw, f"{domain} is on the skip_domains list")
    if skip_reason(url, allow_documents=True):
        return RejectedSeed(raw, "points at a file or a login/cart page")
    region = country_code(country)
    if country and not region:
        return RejectedSeed(raw, f"unknown country {country!r}. Use a code like US, CA, GB or DE")
    return Seed(url=url, domain=domain, name=name or None, category=category or None, country=region)


def load_seeds(
    path: str | Path, skip_domains: list[str], allow_private_hosts: bool = False
) -> tuple[list[Seed], list[RejectedSeed]]:
    """Read seeds from a file. A .csv may also carry optional name, category and country columns."""
    path = Path(path)
    entries = _read_csv(path) if path.suffix.lower() == ".csv" else _read_text(path)

    seeds: list[Seed] = []
    rejected: list[RejectedSeed] = []
    seen: set[str] = set()
    for raw, name, category, country in entries:
        parsed = parse_seed(raw, skip_domains, allow_private_hosts, name, category, country)
        if isinstance(parsed, RejectedSeed):
            rejected.append(parsed)
        elif parsed.url not in seen:
            seen.add(parsed.url)
            seeds.append(parsed)
    return seeds, rejected


def _read_text(path: Path) -> list[tuple[str, None, None, None]]:
    lines = path.read_text(encoding="utf-8-sig").splitlines()
    return [(line.strip(), None, None, None) for line in lines if line.strip() and not line.lstrip().startswith("#")]


def _read_csv(path: Path) -> list[tuple[str, str | None, str | None, str | None]]:
    with path.open(encoding="utf-8-sig", newline="") as handle:
        rows = [row for row in csv.reader(handle) if any(cell.strip() for cell in row)]
    if not rows:
        return []

    header = [cell.strip().lower() for cell in rows[0]]
    url_index = next((header.index(col) for col in _URL_COLUMNS if col in header), None)
    if url_index is None:
        return [(row[0], None, None, None) for row in rows if row[0].strip()]

    def cell(row: list[str], column: str) -> str | None:
        if column not in header or header.index(column) >= len(row):
            return None
        return row[header.index(column)].strip() or None

    return [
        (row[url_index], cell(row, "name"), cell(row, "category"), cell(row, "country"))
        for row in rows[1:]
        if url_index < len(row) and row[url_index].strip()
    ]
