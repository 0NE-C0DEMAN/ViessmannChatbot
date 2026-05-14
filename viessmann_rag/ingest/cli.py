"""CLI entry point for ingestion.

  python ingest.py --dir <path>                Local directory (one-shot)
  python ingest.py --dir <path> --file foo.pdf Local, single file
  python ingest.py --drive                     Google Drive (one-shot)
  python ingest.py --drive --loop              Google Drive (continuous)
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

from ..logging_setup import configure
from .drive import ingest_drive
from .local import ingest_local


def main() -> None:
    log = configure("ingest")

    ap = argparse.ArgumentParser(description="Viessmann RAG ingest pipeline")
    src = ap.add_mutually_exclusive_group(required=True)
    src.add_argument("--dir",   help="Local directory of PDFs (recursed)")
    src.add_argument("--drive", action="store_true", help="Pull from Google Drive")

    ap.add_argument("--file", help="(--dir mode) only process this filename")
    ap.add_argument("--loop", action="store_true",
                    help="(--drive mode) poll every POLL_INTERVAL_SECONDS")
    args = ap.parse_args()

    if args.dir:
        directory = Path(args.dir)
        if not directory.exists():
            log.error("Directory not found: %s", directory)
            sys.exit(1)
        ingest_local(directory, single_file=args.file)
    else:
        ingest_drive(loop=args.loop)


if __name__ == "__main__":
    main()
