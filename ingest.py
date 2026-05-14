"""Entry point: ingestion CLI.

Run from the repository root:

    python ingest.py --dir "C:\\path\\to\\pdfs"
    python ingest.py --drive
    python ingest.py --drive --loop

Implementation lives in `viessmann_rag/ingest/cli.py`.
"""
from viessmann_rag.ingest.cli import main

if __name__ == "__main__":
    main()
