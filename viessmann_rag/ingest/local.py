"""Local-directory ingest mode."""
from __future__ import annotations

import logging
import time
from pathlib import Path
from typing import Optional

from .metadata import md5_file
from .pipeline import process_pdf_bytes

log = logging.getLogger("ingest")


def ingest_local(directory: Path, single_file: Optional[str] = None) -> None:
    """Walk `directory` recursively for `*.pdf` and ingest each one.

    `file_id` for locally-ingested PDFs is the filename stem. (Drive mode
    uses Drive's actual file_id, so a PDF re-ingested through Drive after a
    local test will create a separate row.)
    """
    pdfs = sorted(directory.rglob("*.pdf"))
    if single_file:
        pdfs = [p for p in pdfs if p.name == single_file]
    log.info("Ingesting %d PDFs from %s", len(pdfs), directory)

    t0 = time.time()
    ok = 0
    for p in pdfs:
        try:
            process_pdf_bytes(
                file_id=p.stem,
                file_name=p.name,
                pdf_bytes=p.read_bytes(),
                md5=md5_file(p),
            )
            ok += 1
        except Exception as e:
            log.error("✗ %s — %s", p.name, e, exc_info=True)
    log.info("Local ingest done: %d / %d PDFs in %.1fs",
             ok, len(pdfs), time.time() - t0)
