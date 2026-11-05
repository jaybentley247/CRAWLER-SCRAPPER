"""Reads emails from a PDF or spreadsheet given as a seed, such as a published list of contractors.

A file like that lists many businesses, so each email becomes its own lead: one per company
domain, or one per address for freemail, where the domain says nothing about the company.
"""

from __future__ import annotations

import html
import re
import zipfile
from collections import defaultdict
from collections.abc import Iterable
from io import BytesIO

import openpyxl
import pyxlsb
import xlrd
from pypdf import PdfReader

from crawler.urls import registrable_domain
from extractors import email
from extractors.email import FREEMAIL_DOMAINS
from extractors.page import ParsedPage
from models import Fact

_OLE_MAGIC = b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1"  # Old .xls files.
_ZIP_MAGIC = b"PK\x03\x04"  # .xlsx, .xlsm and .xlsb files.
# A small .xlsx can unpack to gigabytes. Real contact lists come nowhere near this.
_MAX_UNPACKED_BYTES = 200_000_000
_MAILTO_RE = re.compile(r"mailto:[^\s<>\"']+", re.IGNORECASE)


class UnreadableDocument(Exception):
    """The file isn't a PDF or spreadsheet, or is one we can't open, such as a password-protected one."""


def extract_document(body: bytes, url: str) -> dict[str, list[Fact]]:
    """Emails in a PDF or spreadsheet, grouped by the lead each one belongs to."""
    kind, text, links = _read(body)
    # Spreadsheets often hold "mailto:..." as plain cell text rather than as a link.
    links += _MAILTO_RE.findall(text)
    # Wrapped as a page so the email extractor treats the text and links exactly as it does on a site.
    anchors = "".join(f'<a href="{html.escape(link)}"></a>' for link in links)
    page = ParsedPage(f"<html><body>{anchors}<pre>{html.escape(text)}</pre></body></html>", url)

    grouped: dict[str, list[Fact]] = defaultdict(list)
    for fact in email.extract(page):
        grouped[lead_key(fact.value)].append(fact.model_copy(update={"method": f"{kind}_{fact.method}"}))
    return dict(grouped)


def lead_key(address: str) -> str:
    domain = address.rsplit("@", 1)[1]
    if domain in FREEMAIL_DOMAINS:
        return address
    return registrable_domain(f"https://{domain}") or domain


def _read(body: bytes) -> tuple[str, str, list[str]]:
    """The file's type, text and links. The type comes from the content: a .xls link often serves .xlsx."""
    if b"%PDF" in body[:1024]:
        kind, reader = "pdf", _read_pdf
    elif body.startswith(_OLE_MAGIC):
        kind, reader = "xls", _read_xls
    elif body.startswith(_ZIP_MAGIC):
        kind = _zip_kind(body)
        reader = _read_xlsb if kind == "xlsb" else _read_xlsx
    else:
        raise UnreadableDocument("not_a_pdf_or_spreadsheet")
    try:
        text, links = reader(body)
    except UnreadableDocument:
        raise
    except Exception as exc:  # The readers raise many kinds of error on damaged files.
        raise UnreadableDocument(f"unreadable_{kind}: {type(exc).__name__}") from exc
    return kind, text, links


def _zip_kind(body: bytes) -> str:
    try:
        with zipfile.ZipFile(BytesIO(body)) as archive:
            entries = archive.infolist()
    except zipfile.BadZipFile as exc:
        raise UnreadableDocument("unreadable_spreadsheet: BadZipFile") from exc
    if sum(entry.file_size for entry in entries) > _MAX_UNPACKED_BYTES:
        raise UnreadableDocument("spreadsheet_too_large_unpacked")
    names = {entry.filename for entry in entries}
    if "xl/workbook.bin" in names:
        return "xlsb"
    if "xl/workbook.xml" in names:
        return "xlsx"
    raise UnreadableDocument("not_a_pdf_or_spreadsheet")


def _read_pdf(body: bytes) -> tuple[str, list[str]]:
    reader = PdfReader(BytesIO(body))
    if reader.is_encrypted and not reader.decrypt(""):
        raise UnreadableDocument("password_protected")
    texts: list[str] = []
    links: list[str] = []
    for pdf_page in reader.pages:
        texts.append(pdf_page.extract_text() or "")
        links.extend(_pdf_page_links(pdf_page))
    return "\n".join(texts), links


def _pdf_page_links(pdf_page) -> list[str]:
    """Clickable links, which hold mailto: addresses that may not appear in the text."""
    annotations = pdf_page.get("/Annots")
    if annotations is None:
        return []
    links: list[str] = []
    for annotation in annotations.get_object():
        action = annotation.get_object().get("/A")
        uri = action.get_object().get("/URI") if action is not None else None
        if isinstance(uri, bytes):
            uri = uri.decode("latin-1")
        if isinstance(uri, str):
            links.append(uri)
    return links


def _read_xlsx(body: bytes) -> tuple[str, list[str]]:
    """.xlsx and .xlsm. Macros in an .xlsm are never run: only cell values are read."""
    # data_only gives the last calculated value of a formula, not the formula itself.
    workbook = openpyxl.load_workbook(BytesIO(body), data_only=True)
    try:
        rows: list[Iterable[object]] = []
        links: list[str] = []
        for sheet in workbook.worksheets:
            for row in sheet.iter_rows():
                rows.append(cell.value for cell in row)
                links.extend(cell.hyperlink.target for cell in row if cell.hyperlink and cell.hyperlink.target)
        return _rows_to_text(rows), links
    finally:
        workbook.close()


def _read_xls(body: bytes) -> tuple[str, list[str]]:
    workbook = xlrd.open_workbook(file_contents=body, on_demand=True)
    try:
        rows: list[Iterable[object]] = []
        links: list[str] = []
        for sheet in workbook.sheets():
            rows.extend(sheet.row_values(index) for index in range(sheet.nrows))
            links.extend(link.url_or_path for link in sheet.hyperlink_list if link.url_or_path)
        return _rows_to_text(rows), links
    finally:
        workbook.release_resources()


def _read_xlsb(body: bytes) -> tuple[str, list[str]]:
    rows: list[Iterable[object]] = []
    with pyxlsb.open_workbook(BytesIO(body)) as workbook:
        for name in workbook.sheets:
            with workbook.get_sheet(name) as sheet:
                rows.extend([cell.v for cell in row] for row in sheet.rows())
    return _rows_to_text(rows), []


def _rows_to_text(rows: Iterable[Iterable[object]]) -> str:
    # Gaps between cells keep an email in one cell from running into the text of the next.
    lines = ("   ".join(str(value) for value in row if value not in (None, "")) for row in rows)
    return "\n".join(line for line in lines if line)
