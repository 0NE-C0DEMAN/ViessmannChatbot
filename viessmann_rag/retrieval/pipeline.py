"""End-to-end retrieval: question → expand → search × N → dedup → diversify → rerank."""
from __future__ import annotations

import logging
from typing import Optional

from ..config import (
    DIVERSIFY_MAX_PER_FILE,
    HYBRID_CANDIDATE_COUNT,
    RERANK_TOP_K,
)
from ..openai_client import QuotaExhausted
from .diversify import diversify
from .expand import expand_query
from .rerank import rerank
from .search import hybrid_search

log = logging.getLogger("retrieval")


def retrieve(
    question: str,
    product_line: Optional[str] = None,
    document_type: Optional[str] = None,
) -> list[dict]:
    """Return up to RERANK_TOP_K chunks, ordered by relevance.

    The pipeline:
      1. Expand the question into 1-3 variants (original + Croatian + keywords)
      2. Run hybrid search for each variant, union and dedup the candidates
      3. Sort by hybrid_score, cap per-file (when several files compete)
      4. LLM rerank against the ORIGINAL question, take top_k
    """
    queries = expand_query(question)
    log.info("Query expansion → %d variants", len(queries))

    # With multiple variants we want a deeper pool per variant (recall trumps
    # latency when the question crosses languages). The dedup step trims any
    # overlap before diversify / rerank see the result.
    per_query_n = HYBRID_CANDIDATE_COUNT if len(queries) == 1 else 30

    seen: set[str] = set()
    pool: list[dict] = []
    for q in queries:
        try:
            chunks = hybrid_search(q, product_line, document_type,
                                   match_count=per_query_n)
        except QuotaExhausted:
            # Quota is global — every subsequent variant would also fail.
            # Bubble up so the chat endpoint returns 503 with the right
            # Croatian error message instead of a generic "not found".
            raise
        except Exception as e:
            log.warning("hybrid_search failed for variant (%s): %s", q[:60], e)
            continue
        for c in chunks:
            cid = c.get("chunk_id") or c.get("id")
            if not cid or cid in seen:
                continue
            seen.add(cid)
            pool.append(c)

    if not pool:
        return []

    # Best-first before diversifying so per-file caps drop weaker chunks
    pool.sort(key=lambda c: c.get("hybrid_score", 0), reverse=True)
    diversified = diversify(pool, max_per_file=DIVERSIFY_MAX_PER_FILE)[:20]

    return rerank(question, diversified, top_k=RERANK_TOP_K)
