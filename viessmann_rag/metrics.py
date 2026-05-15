"""Per-query metrics logging (NDJSON).

Each chat request writes one JSON line to `logs/metrics.ndjson`. Lets you
analyze the system over time without changing query patterns mid-flight:

    cat logs/metrics.ndjson | jq 'select(.cache_hit) | .latency_ms' | average
    cat logs/metrics.ndjson | jq -s 'group_by(.intent) | map({intent: .[0].intent, count: length})'

Fields:
  ts             ISO8601 timestamp
  latency_ms     total wall-clock time for the request
  cache_hit      true if served from cache
  intent         classified intent category (or null)
  preferred_docs preferred doc_types from intent classifier
  retrieved      number of chunks after rerank
  top_rerank     highest rerank score (for confidence visibility)
  question_len   length of the question in chars
  answer_len     length of the answer in chars
  status         HTTP status code returned
  error          error string if any
  question_hash  sha256 of the question (privacy-friendly identifier)
"""
from __future__ import annotations

import hashlib
import json
import logging
import threading
from datetime import datetime, timezone
from typing import Any, Optional

from .config import LOG_DIR

log = logging.getLogger("metrics")

_LOCK = threading.Lock()
_PATH = LOG_DIR / "metrics.ndjson"


def _hash_question(question: str) -> str:
    return hashlib.sha256(question.strip().encode("utf-8")).hexdigest()[:16]


def record(
    *,
    latency_ms:     int,
    question:       str,
    status:         int,
    cache_hit:      bool             = False,
    intent:         Optional[str]    = None,
    preferred_docs: Optional[list]   = None,
    retrieved:      Optional[int]    = None,
    top_rerank:     Optional[float]  = None,
    answer_len:     Optional[int]    = None,
    error:          Optional[str]    = None,
) -> None:
    """Append one metric record. Never raises — best-effort."""
    rec = {
        "ts":             datetime.now(timezone.utc).isoformat(),
        "latency_ms":     latency_ms,
        "cache_hit":      cache_hit,
        "intent":         intent,
        "preferred_docs": preferred_docs or [],
        "retrieved":      retrieved,
        "top_rerank":     top_rerank,
        "question_len":   len(question or ""),
        "answer_len":     answer_len,
        "status":         status,
        "error":          error,
        "question_hash":  _hash_question(question or ""),
    }
    try:
        LOG_DIR.mkdir(exist_ok=True)
        line = json.dumps(rec, ensure_ascii=False)
        with _LOCK:
            with _PATH.open("a", encoding="utf-8") as f:
                f.write(line + "\n")
    except Exception as e:
        # Never break the request because metrics failed
        log.warning("metrics.record failed: %s", e)
