"""In-memory query cache with LRU eviction + TTL expiry.

Skips full retrieval+answer for repeated questions. Big win on cost and
latency: a cached hit is ~1ms; a fresh call is ~15-25s and costs ~$0.09.

Caveats:
- Process-local (resets on server restart). For multi-worker deployments,
  swap the backing dict for Redis using the same interface.
- Keys hash on (question + product_line + document_type + history). If the
  user adds a follow-up that includes prior turns, the key changes — so we
  don't accidentally return stale answers for an evolving conversation.
- Bypassed when the request includes `nocache=true`.
"""
from __future__ import annotations

import hashlib
import json
import logging
import threading
import time
from collections import OrderedDict
from typing import Any, Optional

log = logging.getLogger("cache")


class QueryCache:
    """LRU cache with TTL. Thread-safe."""

    def __init__(self, maxsize: int = 1000, ttl_seconds: int = 3600):
        self.maxsize     = maxsize
        self.ttl_seconds = ttl_seconds
        self._store: "OrderedDict[str, tuple[float, Any]]" = OrderedDict()
        self._lock   = threading.Lock()
        self._hits   = 0
        self._misses = 0
        self._evict  = 0

    @staticmethod
    def make_key(question: str, product_line: Optional[str],
                 document_type: Optional[str], history: list[dict]) -> str:
        """Stable hash of the request shape.

        We canonicalize history so different conversational paths don't
        share a cache entry — but a single question repeated by another
        user does."""
        payload = {
            "q":  question.strip(),
            "pl": product_line or "",
            "dt": document_type or "",
            "h":  [(m.get("role", ""), m.get("content", ""))
                   for m in (history or [])],
        }
        blob = json.dumps(payload, ensure_ascii=False, sort_keys=True)
        return hashlib.sha256(blob.encode("utf-8")).hexdigest()

    def get(self, key: str) -> Optional[Any]:
        with self._lock:
            entry = self._store.get(key)
            if entry is None:
                self._misses += 1
                return None
            stored_at, value = entry
            if time.time() - stored_at > self.ttl_seconds:
                # Expired — drop and miss
                del self._store[key]
                self._misses += 1
                return None
            # LRU bump
            self._store.move_to_end(key)
            self._hits += 1
            return value

    def set(self, key: str, value: Any) -> None:
        with self._lock:
            self._store[key] = (time.time(), value)
            self._store.move_to_end(key)
            while len(self._store) > self.maxsize:
                self._store.popitem(last=False)
                self._evict += 1

    def stats(self) -> dict:
        with self._lock:
            total = self._hits + self._misses
            return {
                "size":     len(self._store),
                "maxsize":  self.maxsize,
                "hits":     self._hits,
                "misses":   self._misses,
                "evictions": self._evict,
                "hit_rate": round(self._hits / total, 3) if total else 0.0,
            }

    def clear(self) -> None:
        with self._lock:
            self._store.clear()


# Module-level singleton — one cache per process.
cache = QueryCache(maxsize=1000, ttl_seconds=3600)
