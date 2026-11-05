"""Works out which country a site is in, so local phone numbers can be read correctly.

"(713) 555-0142" only means something once you know it's American. A number written with
its country code doesn't need any of this, but plenty of business sites leave the code off.
"""

from __future__ import annotations

import re
from collections import Counter
from typing import NamedTuple

import phonenumbers
from phonenumbers import geocoder

from extractors import jsonld
from extractors.page import ParsedPage


def _all_country_names() -> dict[str, str]:
    """Every country the phone library knows, by English name, plus common local names and short forms."""
    names: dict[str, str] = {}
    for region in phonenumbers.SUPPORTED_REGIONS:
        example = phonenumbers.example_number(region)
        name = geocoder.country_name_for_number(example, "en") if example else ""
        if name:
            names[name.split(" (")[0].lower()] = region
    names.update({
        "united states of america": "US", "usa": "US", "u.s.a.": "US", "uk": "GB", "great britain": "GB",
        "england": "GB", "scotland": "GB", "deutschland": "DE", "españa": "ES", "italia": "IT", "nederland": "NL",
        "the netherlands": "NL", "belgië": "BE", "belgique": "BE", "schweiz": "CH", "suisse": "CH",
        "österreich": "AT", "sverige": "SE", "norge": "NO", "danmark": "DK", "suomi": "FI", "polska": "PL",
        "czech republic": "CZ", "brasil": "BR", "méxico": "MX", "uae": "AE", "emirates": "AE", "hong kong": "HK",
    })
    return names


COUNTRY_NAMES = _all_country_names()
# Valid as a country code or address, but in running text these are usually a US state,
# a person's name or an ordinary word, so they aren't counted as evidence of a country.
_AMBIGUOUS_IN_TEXT = {"georgia", "jordan", "chad", "turkey", "guinea", "lebanon", "mali", "niger", "dominica", "jersey"}
_LANGUAGE_REGIONS = {
    "de": "DE", "fr": "FR", "es": "ES", "it": "IT", "nl": "NL", "pl": "PL", "pt": "PT", "sv": "SE", "da": "DK",
    "fi": "FI", "nb": "NO", "nn": "NO", "no": "NO", "el": "GR", "cs": "CZ", "sk": "SK", "hu": "HU", "ro": "RO",
    "bg": "BG", "hr": "HR", "sl": "SI", "ja": "JP", "ko": "KR", "tr": "TR", "ru": "RU", "uk": "UA", "he": "IL",
    "th": "TH", "vi": "VN", "id": "ID", "ms": "MY",
}
# Country-code domains that businesses anywhere buy because the letters look good.
_VANITY_TLDS = {"io", "ai", "co", "me", "tv", "cc", "ws", "fm", "ly", "to", "gg", "sh", "so", "la", "am", "vc", "st", "tk"}
_US_STATES = (
    "AL|AK|AZ|AR|CA|CO|CT|DE|DC|FL|GA|HI|ID|IL|IN|IA|KS|KY|LA|ME|MD|MA|MI|MN|MS|MO|MT|NE|NV|NH|NJ|NM|NY|NC|ND|"
    "OH|OK|OR|PA|RI|SC|SD|TN|TX|UT|VT|VA|WA|WV|WI|WY"
)
_US_ZIP_RE = re.compile(rf"\b(?:{_US_STATES})\s+\d{{5}}(?:-\d{{4}})?\b")
_CA_POSTAL_RE = re.compile(r"\b[ABCEGHJ-NPRSTVXY]\d[ABCEGHJ-NPRSTV-Z][ -]?\d[ABCEGHJ-NPRSTV-Z]\d\b")
COUNTRY_NAME_RE = re.compile(
    r"(?<!\w)(?<!new )("
    + "|".join(re.escape(name) for name in sorted(COUNTRY_NAMES.keys() - _AMBIGUOUS_IN_TEXT, key=len, reverse=True))
    + r")(?!\w)",
    re.IGNORECASE,
)


class RegionGuess(NamedTuple):
    region: str
    confident: bool


def country_code(value: str | None) -> str | None:
    """Turn 'DE', 'de' or 'Germany' into a phone region code. None if it isn't recognised."""
    text = (value or "").strip()
    if text.upper() == "UK":
        return "GB"
    if text.upper() in phonenumbers.SUPPORTED_REGIONS:
        return text.upper()
    return COUNTRY_NAMES.get(text.lower())


def region_from_domain(domain: str) -> str | None:
    tld = domain.rsplit(".", 1)[-1].lower()
    if tld in _VANITY_TLDS or len(tld) != 2:
        return None
    return country_code(tld)


def detect_region(page: ParsedPage, seed_region: str | None, default: str) -> RegionGuess:
    """Best guess at the site's country, trying the most reliable signals first."""
    found = seed_region or region_from_domain(page.domain) or _from_jsonld(page) or _from_lang_tag(page, full_only=True)
    found = found or _from_postal_code(page) or _from_country_names(page) or _from_lang_tag(page, full_only=False)
    return RegionGuess(found, True) if found else RegionGuess(default, False)


def _from_jsonld(page: ParsedPage) -> str | None:
    for node in jsonld.parse_blocks(page.jsonld_blocks):
        for address in jsonld.as_list(node.get("address")):
            if isinstance(address, dict) and (code := country_code(jsonld.text_of(address.get("addressCountry")))):
                return code
    return None


def _from_lang_tag(page: ParsedPage, full_only: bool) -> str | None:
    """<html lang="en-GB"> names a country. <html lang="de"> strongly suggests one. Plain "en" doesn't."""
    node = page.tree.css_first("html")
    parts = ((node.attributes.get("lang") or "") if node else "").replace("_", "-").split("-")
    if len(parts) >= 2 and len(parts[-1]) == 2:
        return country_code(parts[-1])
    return None if full_only else _LANGUAGE_REGIONS.get(parts[0].lower())


def _from_postal_code(page: ParsedPage) -> str | None:
    if _US_ZIP_RE.search(page.flat_text):
        return "US"
    if _CA_POSTAL_RE.search(page.flat_text):
        return "CA"
    return None


def _from_country_names(page: ParsedPage) -> str | None:
    """The country named most often. A tie tells us nothing ("we ship to Canada and Germany")."""
    counts = Counter(COUNTRY_NAMES[match.lower()] for match in COUNTRY_NAME_RE.findall(page.flat_text))
    ranked = counts.most_common(2)
    if not ranked or (len(ranked) == 2 and ranked[0][1] == ranked[1][1]):
        return None
    return ranked[0][0]
