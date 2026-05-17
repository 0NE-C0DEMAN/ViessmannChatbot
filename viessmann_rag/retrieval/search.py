"""Hybrid search via the Supabase `search_chunks_v2` RPC.

The RPC does the heavy lifting:
  - Cosine similarity over HNSW vector index
  - Full-text rank (`ts_rank_cd`)
  - Trigram similarity fallback (helps with part-number-like tokens)
  - Weighted combination into a single hybrid_score
"""
from __future__ import annotations

from typing import Optional

from ..config import SEMANTIC_WEIGHT
from ..llm import SEARCH_RPC, embed_query
from ..supabase_client import call_rpc


def hybrid_search(
    question: str,
    product_line: Optional[str] = None,
    document_type: Optional[str] = None,
    match_count: int = 30,
    semantic_weight: float = SEMANTIC_WEIGHT,
) -> list[dict]:
    """Embed `question` and call the Supabase hybrid-search RPC.

    Returns rows with keys: chunk_id, file_id, file_name, product_line,
    document_type, page_number, section_heading, chunk_text, has_table,
    semantic_score, keyword_score, hybrid_score.
    """
    q_emb = embed_query(question)
    return call_rpc(
        SEARCH_RPC,
        {
            "q_embedding":     q_emb,
            "q_text":          question,
            "n":               match_count,
            "f_product_line":  product_line,
            "f_document_type": document_type,
            "w_sem":           semantic_weight,
        },
    ) or []
