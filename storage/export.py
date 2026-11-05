"""CSV and Excel export, including a sheet showing where each value came from.

Scraped text is untrusted. A site could publish a "name" like =HYPERLINK(...), which a
spreadsheet would run as a formula, so every cell goes through `safe_cell` first.
"""

from __future__ import annotations

import csv
import re
from collections.abc import Sequence
from pathlib import Path

from openpyxl import Workbook
from openpyxl.styles import Font
from openpyxl.utils import get_column_letter

from models import LEAD_COLUMNS

EVIDENCE_COLUMNS = ("domain", "field", "value", "source_url", "method", "confidence")
_FORMULA_PREFIXES = ("=", "+", "-", "@", "\t", "\r")
_E164_RE = re.compile(r"^\+\d{7,15}$")
_MAX_COLUMN_WIDTH = 50
_EXCEL_CELL_LIMIT = 32767
_EXCEL_ILLEGAL_RE = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f]")

Rows = Sequence[dict[str, str | int | float]]


def safe_cell(value: str | int | float | None) -> str | int | float:
    """Stop a text value being read as a formula. Clean phone numbers are left as they are."""
    if value is None:
        return ""
    if not isinstance(value, str):
        return value
    if value.startswith(_FORMULA_PREFIXES) and not _E164_RE.match(value):
        return "'" + value
    return value


def export_csv(
    leads: Rows, evidence: Rows, out_dir: Path, stem: str, columns: Sequence[str] = LEAD_COLUMNS
) -> list[Path]:
    out_dir.mkdir(parents=True, exist_ok=True)
    leads_path = out_dir / f"{stem}.csv"
    evidence_path = out_dir / f"{stem}_evidence.csv"
    _write_csv(leads_path, columns, leads)
    _write_csv(evidence_path, EVIDENCE_COLUMNS, evidence)
    return [leads_path, evidence_path]


def _write_csv(path: Path, columns: Sequence[str], rows: Rows) -> None:
    # utf-8-sig so Excel shows accents and symbols like ₦ correctly.
    with path.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=columns, extrasaction="ignore")
        writer.writeheader()
        for row in rows:
            writer.writerow({column: safe_cell(row.get(column)) for column in columns})


def export_excel(leads: Rows, evidence: Rows, out_dir: Path, stem: str, columns: Sequence[str] = LEAD_COLUMNS) -> Path:
    out_dir.mkdir(parents=True, exist_ok=True)
    workbook = Workbook()
    sheet = workbook.active
    sheet.title = "Leads"
    _fill_sheet(sheet, columns, leads)
    _fill_sheet(workbook.create_sheet("Evidence"), EVIDENCE_COLUMNS, evidence)
    path = out_dir / f"{stem}.xlsx"
    workbook.save(path)
    return path


def _fill_sheet(sheet, columns: Sequence[str], rows: Rows) -> None:
    sheet.append(list(columns))
    for cell in sheet[1]:
        cell.font = Font(bold=True)
    widths = [len(column) for column in columns]
    for row_index, row in enumerate(rows, start=2):
        for column_index, column in enumerate(columns, start=1):
            value = safe_cell(row.get(column))
            cell = sheet.cell(row=row_index, column=column_index)
            if isinstance(value, str):
                # Setting the type explicitly stops openpyxl treating "=..." as a formula.
                cell.value = _EXCEL_ILLEGAL_RE.sub(" ", value)[:_EXCEL_CELL_LIMIT]
                cell.data_type = "s"
            else:
                cell.value = value
            widths[column_index - 1] = max(widths[column_index - 1], len(str(value)))
    for index, width in enumerate(widths, start=1):
        sheet.column_dimensions[get_column_letter(index)].width = min(width + 2, _MAX_COLUMN_WIDTH)
    sheet.freeze_panes = "A2"
    if rows:
        sheet.auto_filter.ref = sheet.dimensions
