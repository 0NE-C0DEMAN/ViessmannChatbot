"""Top up the `embedding_gem` column for chunks that only have the OpenAI
embedding so far. Skips chunks where embedding_gem IS NOT NULL.

Run once after `migrations/004_dual_embeddings.sql` to backfill historical
data. Subsequent ingests with INGEST_DUAL=true keep both columns in sync.

Usage:
    py -3.11 tools/topup_gem_embeddings.py [--limit N] [--dry-run]

Cost: free (uses gemini-embedding-001).
"""
from __future__ import annotations

import argparse
import logging
import sys
import time

# Make `viessmann_rag` importable from the repo root.
import pathlib
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

from viessmann_rag.config import SUPABASE_SERVICE_KEY, SUPABASE_URL  # noqa: E402
from viessmann_rag.gemini_client import embed as gem_embed  # noqa: E402

import requests  # noqa: E402

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)-7s %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("topup")

HEADERS = {
    "apikey":        SUPABASE_SERVICE_KEY,
    "Authorization": "Bearer " + SUPABASE_SERVICE_KEY,
    "Content-Type":  "application/json",
}


def fetch_missing(limit: int, batch_size: int = 50) -> list[dict]:
    """Pull chunks with embedding_gem IS NULL, only the fields we need."""
    rows: list[dict] = []
    offset = 0
    while len(rows) < limit:
        params = {
            "select":         "id,chunk_text",
            "embedding_gem":  "is.null",
            "limit":          str(min(batch_size, limit - len(rows))),
            "offset":         str(offset),
            "order":          "id",
        }
        r = requests.get(
            f"{SUPABASE_URL}/rest/v1/document_chunks_v2",
            headers=HEADERS, params=params, timeout=30,
        )
        r.raise_for_status()
        page = r.json()
        if not page:
            break
        rows.extend(page)
        offset += len(page)
        if len(page) < batch_size:
            break
    return rows[:limit]


def update_embedding(chunk_id: str, vec: list[float]) -> None:
    """PATCH a single row's embedding_gem column."""
    r = requests.patch(
        f"{SUPABASE_URL}/rest/v1/document_chunks_v2?id=eq.{chunk_id}",
        headers=HEADERS, json={"embedding_gem": vec}, timeout=30,
    )
    r.raise_for_status()


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--limit", type=int, default=10000,
                    help="Max chunks to top up (default: 10000)")
    ap.add_argument("--dry-run", action="store_true",
                    help="Count only, no API calls")
    args = ap.parse_args()

    log.info("Fetching chunks with embedding_gem IS NULL ...")
    todo = fetch_missing(args.limit)
    log.info("Found %d chunks to top up", len(todo))

    if args.dry_run or not todo:
        return

    started = time.time()
    failed = 0
    for i, row in enumerate(todo, 1):
        try:
            vec = gem_embed(row["chunk_text"])
            update_embedding(row["id"], vec)
        except Exception as e:
            failed += 1
            log.warning("  [%d/%d] failed: %s", i, len(todo), e)
            continue

        if i % 10 == 0 or i == len(todo):
            elapsed = time.time() - started
            rate = i / elapsed if elapsed > 0 else 0
            eta = (len(todo) - i) / rate if rate > 0 else 0
            log.info("  [%d/%d] ok=%d fail=%d  rate=%.1f/s  eta=%.0fs",
                     i, len(todo), i - failed, failed, rate, eta)

    log.info("Done. %d succeeded, %d failed in %.0fs",
             len(todo) - failed, failed, time.time() - started)


if __name__ == "__main__":
    main()
