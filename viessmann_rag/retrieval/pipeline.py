"""End-to-end retrieval: question → expand → search × N → dedup → diversify → rerank.

Pre-search stages (intent classification, query expansion, HyDE) run in
parallel — they're independent and all involve OpenAI calls, so doing them
sequentially wastes 2-4s per query. Same for the per-variant hybrid_search
RPC calls: independent network round-trips that can be fanned out.
"""
from __future__ import annotations

import logging
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Optional

from ..config import (
    DIVERSIFY_MAX_PER_FILE,
    HYBRID_CANDIDATE_COUNT,
    RERANK_TOP_K,
)
from ..openai_client import QuotaExhausted
from .diversify import diversify
from .expand import expand_query
from .hyde import hypothetical_doc
from .intent import Intent, classify
from .rerank import rerank
from .search import hybrid_search

# Intents that benefit from a HyDE query (cross-language question/answer gap)
_HYDE_INTENTS = {"spec", "capability"}

log = logging.getLogger("retrieval")


def _search_variants(
    queries: list[str],
    product_line: Optional[str],
    document_type: Optional[str],
    per_query_n: int,
) -> list[dict]:
    """Run hybrid_search for each query variant in parallel; return deduped pool.

    Each variant search is an OpenAI embedding call + a Supabase RPC round
    trip. They're independent, so we fan them out and merge.
    """
    if not queries:
        return []
    if len(queries) == 1:
        try:
            return hybrid_search(queries[0], product_line, document_type,
                                 match_count=per_query_n)
        except QuotaExhausted:
            raise
        except Exception as e:
            log.warning("hybrid_search failed for variant (%s): %s",
                        queries[0][:60], e)
            return []

    seen: set[str] = set()
    pool: list[dict] = []
    with ThreadPoolExecutor(max_workers=min(4, len(queries))) as ex:
        future_to_q = {
            ex.submit(hybrid_search, q, product_line, document_type, per_query_n): q
            for q in queries
        }
        for fut in as_completed(future_to_q):
            q = future_to_q[fut]
            try:
                chunks = fut.result()
            except QuotaExhausted:
                # Cancel pending searches and propagate — quota is global,
                # subsequent searches would also fail.
                for f in future_to_q:
                    f.cancel()
                raise
            except Exception as e:
                log.warning("hybrid_search failed for variant (%s): %s",
                            q[:60], e)
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
    # 1-2. Pre-search stages — run intent classifier, query expansion, and
    #      (speculative) HyDE in parallel. We don't know whether HyDE applies
    #      until intent comes back, so we fire it speculatively; if intent
    #      ends up non-spec/capability we just discard it. Cost of the
    #      occasional unused HyDE call is ~$0.0002, well worth ~1-2s saved.
    intent: Optional[Intent] = None
    queries: list[str] = []
    hyde_result: Optional[str] = None

    if document_type is None:
        with ThreadPoolExecutor(max_workers=3) as ex:
            f_intent = ex.submit(classify, question)
            f_expand = ex.submit(expand_query, question)
            f_hyde   = ex.submit(hypothetical_doc, question)
            try:
                intent  = f_intent.result()
                queries = f_expand.result()
            except QuotaExhausted:
                f_hyde.cancel()
                raise
            # Resolve HyDE only if its category needs it
            if intent and intent.category in _HYDE_INTENTS:
                try:
                    hyde_result = f_hyde.result()
                except QuotaExhausted:
                    raise
                except Exception as e:
                    log.warning("HyDE resolve failed: %s", e)
                    hyde_result = None
            else:
                f_hyde.cancel()
        log.info("Intent: %s  preferred=%s", intent.category, intent.preferred)
    else:
        # Caller forced document_type — skip intent classification, still
        # parallelize expand + HyDE.
        log.info("Intent classification skipped (document_type=%r forced)",
                 document_type)
        with ThreadPoolExecutor(max_workers=2) as ex:
            f_expand = ex.submit(expand_query, question)
            f_hyde   = ex.submit(hypothetical_doc, question)
            try:
                queries = f_expand.result()
                hyde_result = f_hyde.result()
            except QuotaExhausted:
                raise
            except Exception as e:
                log.warning("Pre-search stage failed: %s", e)

    if hyde_result:
        queries.append(hyde_result)
        log.info("HyDE doc (%d chars) added", len(hyde_result))
    log.info("Query variants: %d", len(queries))
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
