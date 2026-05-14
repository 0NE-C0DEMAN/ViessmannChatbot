"""
Retrieval pipeline for Viessmann RAG v2.

  question  →  embed  →  hybrid_search (top 30)  →  diversify (max 3/file)
            →  LLM rerank with gpt-4o-mini (top 15 → top 6)
            →  return ranked chunks
"""
import json
import logging
import os
from typing import Optional

import requests
from openai import OpenAI

log = logging.getLogger("retrieval")

SUPABASE_URL = os.environ["SUPABASE_URL"]
SUPABASE_KEY = os.environ["SUPABASE_SERVICE_KEY"]

EMBEDDING_MODEL = "text-embedding-3-small"
RERANK_MODEL    = "gpt-4o-mini"
EXPAND_MODEL    = "gpt-4o-mini"

HEADERS = {
    "apikey":        SUPABASE_KEY,
    "Authorization": f"Bearer {SUPABASE_KEY}",
    "Content-Type":  "application/json",
}


# ─── 0. Query expansion (multi-query retrieval) ───────────────────────────
_EXPAND_SYSTEM = (
    "Given a technical question about Viessmann heating products "
    "(Vitocal heat pumps, Vitodens boilers), generate two alternative search "
    "queries to improve retrieval recall against Croatian technical PDFs.\n\n"
    "Output JSON only, with these keys:\n"
    '  {"croatian": "...", "keywords": "..."}\n\n'
    "- croatian: a natural Croatian translation/paraphrase of the question, "
    "using technical terms a Viessmann manual would use (e.g. 'toplinska "
    "snaga', 'rashladno sredstvo', 'radni tlak'). If the question is already "
    "in Croatian, give an English paraphrase instead.\n"
    "- keywords: a keyword-rich version with the technical terms, model codes, "
    "units, and likely Croatian section names. Comma-separated, no full "
    "sentences."
)


def expand_query(oai: OpenAI, question: str) -> list[str]:
    """Return [original, croatian_variant, keywords_variant]. Best-effort."""
    try:
        resp = oai.chat.completions.create(
            model=EXPAND_MODEL,
            messages=[
                {"role": "system", "content": _EXPAND_SYSTEM},
                {"role": "user",   "content": question},
            ],
            response_format={"type": "json_object"},
            temperature=0,
            max_tokens=200,
        )
        data = json.loads(resp.choices[0].message.content or "{}")
        cro = (data.get("croatian") or "").strip()
        kw  = (data.get("keywords") or "").strip()
        out = [question]
        if cro and cro != question:
            out.append(cro)
        if kw:
            out.append(kw)
        return out
    except Exception as e:
        log.warning("Query expansion failed (%s) — using original only", e)
        return [question]


# ─── 1. Hybrid search ──────────────────────────────────────────────────────
def hybrid_search(
    oai: OpenAI,
    question: str,
    product_line: Optional[str] = None,
    document_type: Optional[str] = None,
    match_count: int = 30,
    semantic_weight: float = 0.7,
) -> list[dict]:
    emb = oai.embeddings.create(model=EMBEDDING_MODEL, input=question).data[0].embedding
    r = requests.post(
        f"{SUPABASE_URL}/rest/v1/rpc/search_chunks_v2",
        headers=HEADERS,
        json={
            "q_embedding":     emb,
            "q_text":          question,
            "n":               match_count,
            "f_product_line":  product_line,
            "f_document_type": document_type,
            "w_sem":           semantic_weight,
        },
        timeout=30,
    )
    r.raise_for_status()
    return r.json() or []


# ─── 2. Diversification (per-file cap) ─────────────────────────────────────
def diversify(chunks: list[dict], max_per_file: int = 3) -> list[dict]:
    # Normalize: the RPC returns `chunk_id` (renamed from `id` to dodge plpgsql
    # variable-name conflicts). Restore `id` for downstream consumers.
    for c in chunks:
        if "id" not in c and "chunk_id" in c:
            c["id"] = c["chunk_id"]

    # If candidates come from very few files, diversifying just throws away
    # relevant pages. Only apply the per-file cap when several files compete.
    distinct_files = len({c.get("file_id") for c in chunks})
    if distinct_files <= 2:
        return chunks

    counts: dict[str, int] = {}
    out: list[dict] = []
    for c in chunks:
        fid = c.get("file_id") or ""
        if counts.get(fid, 0) >= max_per_file:
            continue
        counts[fid] = counts.get(fid, 0) + 1
        out.append(c)
    return out


# ─── 3. LLM-based reranker ─────────────────────────────────────────────────
_RERANK_SYSTEM = (
    "You are a relevance scorer for a Croatian technical-documentation RAG system "
    "about Viessmann heating products.\n\n"
    "Given a user question and a list of candidate excerpts, score each excerpt "
    "0-10 by how well it answers the question.\n\n"
    "Output JSON only:\n"
    '  {"scores": [{"i": 0, "s": 8}, {"i": 1, "s": 3}, ...]}\n\n'
    "Scoring guide:\n"
    "  10 — direct answer with the exact values the question asks for\n"
    "  7-9 — highly relevant; contains the data but may need cross-referencing\n"
    "  4-6 — on topic but does not contain the specific answer\n"
    "  1-3 — tangentially related (mentions the topic but no useful detail)\n"
    "  0   — irrelevant (cover page, table of contents, boilerplate)\n\n"
    "Excerpts containing numerical specs / tables with values matching the "
    "question's model variants should score high. Mere mentions of topic words "
    "without data score low."
)


def rerank(
    oai: OpenAI,
    question: str,
    candidates: list[dict],
    top_k: int = 6,
    excerpt_chars: int = 1500,
) -> list[dict]:
    if not candidates:
        return []

    excerpts = []
    for i, c in enumerate(candidates):
        text = (c.get("chunk_text") or "")[:excerpt_chars]
        excerpts.append(f"[{i}]\n{text}")

    user_msg = f"Question: {question}\n\nCandidates:\n\n" + "\n\n".join(excerpts)

    try:
        resp = oai.chat.completions.create(
            model=RERANK_MODEL,
            messages=[
                {"role": "system", "content": _RERANK_SYSTEM},
                {"role": "user",   "content": user_msg},
            ],
            response_format={"type": "json_object"},
            temperature=0,
            max_tokens=600,
        )
        raw = resp.choices[0].message.content or "{}"
        data = json.loads(raw)
        scores = {
            int(item["i"]): float(item["s"])
            for item in data.get("scores", [])
            if "i" in item and "s" in item
        }
    except Exception as e:
        log.warning("Rerank failed (%s) — using hybrid ordering", e)
        return candidates[:top_k]

    for i, c in enumerate(candidates):
        c["rerank_score"] = scores.get(i, 0.0)

    # Use rerank to ORDER, but always pass top_k — multi-variant questions
    # need complementary pages even if the reranker is confident about one.
    # gpt-4o handles 128K tokens; a few irrelevant chunks don't hurt.
    candidates.sort(key=lambda x: x["rerank_score"], reverse=True)
    return candidates[:top_k]


# ─── 4. End-to-end retrieve ────────────────────────────────────────────────
def retrieve(
    oai: OpenAI,
    question: str,
    product_line: Optional[str] = None,
    document_type: Optional[str] = None,
) -> list[dict]:
    # Multi-query: search with the original + a Croatian paraphrase + a
    # keyword variant. Union the candidate pools (dedup by chunk_id).
    # Cross-language gap was the root cause of Q07/Q11 missing pages.
    queries = expand_query(oai, question)
    log.info("Query expansion → %d variants", len(queries))

    seen: set[str] = set()
    pool: list[dict] = []
    per_query_n = 30 if len(queries) > 1 else 50
    for q in queries:
        try:
            chunks = hybrid_search(oai, q, product_line, document_type,
                                   match_count=per_query_n)
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

    # Sort the unioned pool by hybrid_score so diversify sees best-first
    pool.sort(key=lambda c: c.get("hybrid_score", 0), reverse=True)
    diversified = diversify(pool, max_per_file=4)[:20]
    # Rerank with the ORIGINAL question (we want the user's intent)
    return rerank(oai, question, diversified, top_k=10)
