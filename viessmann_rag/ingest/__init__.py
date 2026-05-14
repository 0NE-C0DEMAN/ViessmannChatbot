"""Ingestion pipeline: PDFs → page chunks → Supabase."""

from .pipeline import process_pdf_bytes
from .local    import ingest_local
from .drive    import ingest_drive

__all__ = ["process_pdf_bytes", "ingest_local", "ingest_drive"]
