"""Finds postal addresses in microdata, <address> tags, labelled lines and street-like text."""

from __future__ import annotations

import re

from extractors.page import ParsedPage
from extractors.region import COUNTRY_NAME_RE
from models import Fact

_STREET_RE = re.compile(
    r"\b(?:street|st\.?|road|rd\.?|avenue|ave\.?|close|crescent|cres\.?|drive|dr\.?|court|ct\.?|lane|ln\.?|"
    r"expressway|estate|plaza|square|boulevard|blvd\.?|junction|bus[- ]stop|highway|hwy\.?|parkway|pkwy\.?|"
    r"layout|way|suite|ste\.?|rue|chemin|calle|carrer|avenida|paseo|viale|piazza|corso|rua|ulica|ul\.)(?=\W|$)"
    r"|^via\s"
    # Germanic languages join the street word onto the name: Hauptstraße, Kerkstraat, Storgatan.
    r"|\b\w+(?:straße|strasse|str\.|gasse|allee|platz|weg|straat|laan|gracht|plein|gatan|vägen|vej|gade)(?=\W|$)",
    re.IGNORECASE,
)
# Postal codes distinctive enough to mark a line as an address by themselves.
_STRONG_POSTAL_RE = re.compile(
    r"\b[A-Z]{2}\s+\d{5}(?:-\d{4})?\b"  # US: TX 77002
    r"|\b[A-Z]\d[A-Z][ -]?\d[A-Z]\d\b"  # Canada: M5V 2T6
    r"|\b[A-Z]{1,2}\d[A-Z\d]?\s+\d[A-Z]{2}\b"  # UK: SW1A 1AA
)
_EUROPEAN_POSTAL_LINE_RE = re.compile(r"^(?:[A-Z]{1,2}-)?\d{4,5}\s+[^\W\d]")  # 10115 Berlin
_LABEL_RE = re.compile(
    r"^\s*(?:(?:our\s+|office\s+|head\s+office\s+|business\s+|physical\s+|mailing\s+)?address|head\s+office|"
    r"headquarters|corporate\s+office|office|location|visit\s+us|find\s+us|adresse|anschrift|indirizzo|"
    r"direcci[oó]n|si[eè]ge\s+social|sede)\s*[:\-–]\s*(.*)$",
    re.IGNORECASE,
)
_CONTACT_PART_RE = re.compile(r"@|https?://|\b(?:tel|telephone|phone|mobile|fax|email|e-mail|whatsapp)\b", re.I)
_PROSE_RE = re.compile(r"\b(?:we|our|you|your|i|they|is|are|was|have|has|will)\b", re.I)
_MAX_WORDS = 30


def clean_address(raw: str) -> str:
    """Drop contact details mixed into an address and tidy its punctuation."""
    parts = [part.strip(" \t\r\n,;") for part in re.split(r"[,\n]", raw)]
    kept = [part for part in parts if part and not _CONTACT_PART_RE.search(part)]
    return re.sub(r"\s+", " ", ", ".join(kept))


def looks_like_address(text: str, hints: list[str]) -> bool:
    if not 10 <= len(text) <= 200 or len(text.split()) > _MAX_WORDS:
        return False
    if _CONTACT_PART_RE.search(text):
        return False
    # A place name alone ("United States") is a location, not an address.
    has_place = "," in text and _mentions_hint(text, hints)
    has_digit = any(char.isdigit() for char in text)
    return has_digit or has_place or bool(_STREET_RE.search(text))


def _mentions_hint(text: str, hints: list[str]) -> bool:
    # Whole words only: the hint "usa" must not match inside "thousand".
    if COUNTRY_NAME_RE.search(text):
        return True
    return any(re.search(rf"(?<!\w){re.escape(hint)}(?!\w)", text, re.IGNORECASE) for hint in hints)


def extract(page: ParsedPage, hints: list[str]) -> list[Fact]:
    found: dict[str, tuple[str, float]] = {}

    def add(raw: str, method: str, confidence: float) -> None:
        address = clean_address(raw)
        if looks_like_address(address, hints) and (address not in found or found[address][1] < confidence):
            found[address] = (method, confidence)

    for node in page.tree.css("[itemprop='address']"):
        add(node.text(separator=", ", strip=True), "microdata", 0.85)
    for node in page.tree.css("address"):
        add(node.text(separator=", ", strip=True), "address_tag", 0.8)

    for index, line in enumerate(page.lines):
        labelled = _LABEL_RE.match(line)
        if labelled:
            # The address may be on the same line as its label, or on the next one.
            value = labelled.group(1) or (page.lines[index + 1] if index + 1 < len(page.lines) else "")
            add(value, "labelled_text", 0.7)
        elif _splits_onto_postal_line(page.lines, index):
            add(f"{line}, {page.lines[index + 1]}", "street_pattern", 0.7)
        elif _is_street_line(line, hints):
            add(line, "street_pattern", 0.65 if _mentions_hint(line, hints) else 0.45)

    return [
        Fact(field="address", value=address, source_url=page.url, method=method, confidence=confidence)
        for address, (method, confidence) in found.items()
    ]


def _splits_onto_postal_line(lines: list[str], index: int) -> bool:
    """Addresses are often laid out as '123 Main St' then 'Houston, TX 77002' on the next line."""
    if index + 1 >= len(lines):
        return False
    line, following = lines[index], lines[index + 1].strip()
    if not any(char.isdigit() for char in line) or not _STREET_RE.search(line) or _PROSE_RE.search(line):
        return False
    return bool(_STRONG_POSTAL_RE.search(following) or _EUROPEAN_POSTAL_LINE_RE.match(following))


def _is_street_line(line: str, hints: list[str]) -> bool:
    if not any(char.isdigit() for char in line) or _PROSE_RE.search(line):
        return False
    if _STRONG_POSTAL_RE.search(line):
        return True
    return bool(_STREET_RE.search(line)) and ("," in line or _mentions_hint(line, hints))
