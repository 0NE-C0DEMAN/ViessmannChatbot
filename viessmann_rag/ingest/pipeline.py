"""The hot path: bytes → extract → embed → upsert.

Same regardless of source (local file or Drive download). Splitting this out
lets local.py and drive.py share the exact same logic.
"""
from __future__ import annotations

import logging
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from ..config import INGEST_BATCH_SIZE, INGEST_DUAL, LLM_PROVIDER
from ..llm import embed as active_embed
from ..pdf_parser import extract_pdf
from ..supabase_client import delete_chunks, insert_chunks, upsert_registry
from .metadata import md5_bytes, parse_metadata

log = logging.getLogger("ingest")

# Active provider column vs. the "other" column (for INGEST_DUAL mode).
# Resolved once at import — cheaper than checking per chunk.
ACTIVE_COL   = "embedding_gem" if LLM_PROVIDER == "gemini" else "embedding"
INACTIVE_COL = "embedding" if LLM_PROVIDER == "gemini" else "embedding_gem"


def _inactive_embed_fn():
    """Lazy import of the *other* provider's embed(). Only used when
    INGEST_DUAL=true — otherwise we never load the unused SDK."""
    if LLM_PROVIDER == "gemini":
        from ..openai_client import embed as oai_embed
        return oai_embed
    else:
        from ..gemini_client import embed as gem_embed
        return gem_embed


def _truncate_id(file_id: str) -> str:
    return file_id[:24] + "…" if len(file_id) > 24 else file_id


def process_pdf_bytes(
    file_id: str,
    file_name: str,
    pdf_bytes: bytes,
    md5: Optional[str] = None,
) -> None:
    """Extract, embed, and upsert a single PDF.

    The pdfplumber parser accepts a `Path`, so we write to a temp file. This
    is cheap (one disk write per PDF) and avoids special-casing for in-memory
    streams across the parser API.
    """
    product_line, doc_type = parse_metadata(file_name)
    md5 = md5 or md5_bytes(pdf_bytes)

    log.info("=== %s ===", file_name)
    log.info("  file_id=%s  product_line=%s  doc_type=%s",
             _truncate_id(file_id), product_line, doc_type)

    # 1. Extract pages from a temp file
    with tempfile.NamedTemporaryFile(suffix=".pdf", delete=False) as tmp:
        tmp.write(pdf_bytes)
        tmp_path = Path(tmp.name)
    try:
        pages = extract_pdf(tmp_path)
    finally:
        try:
            tmp_path.unlink()
        except Exception:
            pass
    log.info("  Extracted %d pages", len(pages))

    # 2. Clear old chunks (safe to re-run)
    delete_chunks(file_id)

    # 3. Upsert registry row
    upsert_registry({
        "file_id":           file_id,
        "file_name":         file_name,
        "product_line":      product_line,
        "document_type":     doc_type,
        "md5_checksum":      md5,
        "page_count":        len(pages),
        "status":            "active",
        "last_processed_at": datetime.now(timezone.utc).isoformat(),
    })

    # 4. Embed + insert in batches. In dual mode we also embed with the
    #    inactive provider so both columns are populated in one pass.
    inactive_embed = _inactive_embed_fn() if INGEST_DUAL else None

    batch: list[dict] = []
    stored = 0
    for p in pages:
        if not p.text.strip() or len(p.text.strip()) < 30:
            log.info("  p.%d skipped (sparse, %d chars)", p.page_number, p.char_count)
            continue

        chunk_text = f"[Document: {file_name} · Page {p.page_number}]\n{p.text}"

        try:
            emb_active = active_embed(chunk_text)
        except Exception as e:
            log.error("  p.%d active embed failed: %s", p.page_number, e)
            continue

        row = {
            "file_id":         file_id,
            "file_name":       file_name,
            "product_line":    product_line,
            "document_type":   doc_type,
            "page_number":     p.page_number,
            "section_heading": p.section_heading,
            "chunk_text":      chunk_text,
            "has_table":       p.has_table,
            "token_estimate":  p.char_count // 4,
            ACTIVE_COL:        emb_active,
        }

        if inactive_embed is not None:
            try:
                row[INACTIVE_COL] = inactive_embed(chunk_text)
            except Exception as e:
                # Don't fail the whole page on a dual-mode hiccup — the row
                # still gets the active embedding and can be topped up later.
                log.warning("  p.%d inactive embed failed (%s): %s",
                            p.page_number, INACTIVE_COL, e)

        batch.append(row)
        stored += 1

        if len(batch) >= INGEST_BATCH_SIZE:
            insert_chunks(batch)
            log.info("  flushed batch (%d / %d pages)", stored, len(pages))
            batch = []

    if batch:
        insert_chunks(batch)

    log.info("  ✓ stored %d / %d pages (dual=%s)",
             stored, len(pages), INGEST_DUAL)
