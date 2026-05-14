"""
Layout-preserving PDF extraction for Viessmann technical docs.

Approach (ported from ParkerJones/backend.py):
  - pdfplumber `extract_text(layout=True)` to keep column whitespace
  - pdfplumber `extract_tables(lines_strict)` rendered as markdown
  - NO regex stripping. Numbers, model codes, units, tables all preserved.

One Page = one chunk. The chunk_text includes the layout text and, if any
real tables (with ruling lines) exist, an appended `[TABLE n]` markdown
block. Both views are valid; the LLM uses whichever serves the question.
"""
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

import pdfplumber

# Cap chunk text so we never exceed text-embedding-3-small's 8191-token limit.
# 24000 chars ≈ 6000 tokens — comfortable margin.
MAX_CHUNK_CHARS = 24000


@dataclass
class Page:
    page_number: int
    section_heading: Optional[str]
    text: str
    has_table: bool
    char_count: int


def _table_to_markdown(table: list[list[Optional[str]]], idx: int) -> str:
    lines = [f"[TABLE {idx}]"]
    for row in table:
        cells = [
            "" if c is None else str(c).replace("\n", " ").strip()
            for c in row
        ]
        lines.append("| " + " | ".join(cells) + " |")
    return "\n".join(lines)


def _is_real_table(table: list[list[Optional[str]]]) -> bool:
    if not table:
        return False
    cols = max(len(r) for r in table)
    if len(table) < 2 or cols < 2:
        return False
    non_empty = sum(
        1 for row in table for cell in row
        if cell is not None and str(cell).strip()
    )
    total = len(table) * cols
    return total > 0 and non_empty / total >= 0.25


def _detect_heading(layout_text: str) -> Optional[str]:
    """First short, non-terminated line — typically the page's section header."""
    for line in layout_text.splitlines():
        s = line.strip()
        if not s:
            continue
        if len(s) > 80:
            return None
        if s.endswith((".", ",", ";", ":")):
            return None
        # Skip page-number-only lines
        if s.isdigit():
            return None
        return s
    return None


def extract_page(page) -> Page:
    """Extract one page as a structured chunk."""
    layout_text = page.extract_text(layout=True) or ""

    tables_md: list[str] = []
    try:
        tables = page.extract_tables({
            "vertical_strategy":   "lines_strict",
            "horizontal_strategy": "lines_strict",
        }) or []
        n = 0
        for table in tables:
            if not _is_real_table(table):
                continue
            n += 1
            tables_md.append(_table_to_markdown(table, n))
    except Exception:
        pass  # table extraction is additive — never let it fail the page

    body = layout_text.strip()
    if tables_md:
        body = body + "\n\n=== STRUCTURED TABLES ===\n\n" + "\n\n".join(tables_md)

    if len(body) > MAX_CHUNK_CHARS:
        body = body[:MAX_CHUNK_CHARS] + "\n[…truncated]"

    return Page(
        page_number=page.page_number,
        section_heading=_detect_heading(layout_text),
        text=body,
        has_table=bool(tables_md),
        char_count=len(body),
    )


def extract_pdf(pdf_path: Path) -> list[Page]:
    pages: list[Page] = []
    with pdfplumber.open(pdf_path) as pdf:
        for page in pdf.pages:
            pages.append(extract_page(page))
    return pages
