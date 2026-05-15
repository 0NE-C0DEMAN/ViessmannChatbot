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
from .intent import classify
from .rerank import rerank
from .search import hybrid_search

log = logging.getLogger("retrieval")


def _search_variants(
    queries: list[str],
    product_line: Optional[str],
    document_type: Optional[str],
    per_query_n: int,
) -> list[dict]:
    """Run hybrid_search for each query variant, return deduped pool."""
    seen: set[str] = set()
    pool: list[dict] = []
    for q in queries:
        try:
            chunks = hybrid_search(q, product_line, document_type,
                                   match_count=per_query_n)
        except QuotaExhausted:
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
    return pool


def retrieve(
    question: str,
    product_line: Optional[str] = None,
    document_type: Optional[str] = None,
) -> list[dict]:
    """Return up to RERANK_TOP_K chunks, ordered by relevance.

    The pipeline:
      1. Classify the question to pick a preferred document_type
      2. Expand the question into 1-3 variants (original + Croatian + keywords)
      3. First pass: hybrid search filtered to the preferred document_type
      4. If the filtered pool is thin, fall back to unfiltered search
      5. Sort by hybrid_score, cap per-file (when several files compete)
      6. LLM rerank against the ORIGINAL question, take top_k

    If `document_type` is passed explicitly by the caller, intent classification
    is bypassed (manual override).
    """
    # 1. Intent — pick the doc_type to prefer (unless caller forced one)
    if document_type is None:
        intent = classify(question)
        log.info("Intent: %s  preferred=%s", intent.category, intent.preferred)
    else:
        intent = None
        log.info("Intent classification skipped (document_type=%r forced)", document_type)

    # 2. Query expansion
    queries = expand_query(question)
    log.info("Query expansion → %d variants", len(queries))
    per_query_n = HYBRID_CANDIDATE_COUNT if len(queries) == 1 else 30

    # 3. First pass: search restricted to the primary preferred doc_type
    pool: list[dict] = []
    if intent and intent.primary:
        pool = _search_variants(queries, product_line, intent.primary, per_query_n)
        log.info("First pass (doc_type=%s): %d candidates", intent.primary, len(pool))

    # 4. Fallback to unfiltered when the filtered pool is too thin to be
    #    useful (e.g. the canonical doc doesn't cover this specific question).
    if len(pool) < 10:
        log.info("Pool thin (%d); broadening to all document types", len(pool))
        unfiltered = _search_variants(queries, product_line, document_type, per_query_n)
        # Merge — keep ids we already have at higher rank
        seen = {c.get("chunk_id") or c.get("id") for c in pool}
        for c in unfiltered:
            cid = c.get("chunk_id") or c.get("id")
            if cid and cid not in seen:
                seen.add(cid)
                pool.append(c)
        log.info("After broadening: %d candidates", len(pool))

    if not pool:
        return []

    # 5. Diversify (best-first first so per-file caps drop weaker)
    pool.sort(key=lambda c: c.get("hybrid_score", 0), reverse=True)
    diversified = diversify(pool, max_per_file=DIVERSIFY_MAX_PER_FILE)[:20]

    # 6. LLM rerank — orders the final set; passes all top_k through to the
    #    answering model. We trust the system prompt (rules 3a/3b/6) to make
    #    the LLM refuse when it doesn't see the value verbatim, rather than
    #    using a hard rerank-score gate (the reranker scores 0 on chunks it
    #    isn't sure about, which would otherwise kill valid borderline cases).
    return rerank(question, diversified, top_k=RERANK_TOP_K)
