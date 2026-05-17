"""LLM-based reranker.

We use rerank to ORDER, not to filter — multi-variant questions need
complementary pages even when the reranker is confident about one. The LLM
context window is plenty large to read all top_k chunks.
"""
from __future__ import annotations

import json
import logging

from ..config import RERANK_EXCERPT_CHARS, RERANK_TOP_K
from ..llm import RERANK_MODEL, chat_completion

log = logging.getLogger("retrieval")

_SYSTEM = (
    "You are a relevance scorer for a Croatian technical-documentation RAG "
    "system about Viessmann heating products.\n\n"
    "Given a user question and a list of candidate excerpts, score each "
    "excerpt 0-10 by how well it answers the question.\n\n"
    "Output JSON only:\n"
    '  {"scores": [{"i": 0, "s": 8}, {"i": 1, "s": 3}, ...]}\n\n'
    "Scoring guide:\n"
    "  10 — direct answer with the exact values the question asks for\n"
    "  7-9 — highly relevant; contains the data but may need cross-referencing\n"
    "  4-6 — on topic but does not contain the specific answer\n"
    "  1-3 — tangentially related (mentions the topic but no useful detail)\n"
    "  0   — irrelevant (cover page, table of contents, boilerplate)\n\n"
    "Excerpts containing numerical specs / tables with values matching the "
    "question's model variants should score high. Mere mentions of topic "
    "words without data score low."
)


def rerank(
    question: str,
    candidates: list[dict],
    top_k: int = RERANK_TOP_K,
    excerpt_chars: int = RERANK_EXCERPT_CHARS,
) -> list[dict]:
    """Score each candidate against `question`, sort, return top_k."""
    if not candidates:
        return []

    excerpts = []
    for i, c in enumerate(candidates):
        text = (c.get("chunk_text") or "")[:excerpt_chars]
        excerpts.append(f"[{i}]\n{text}")
    user_msg = (
        f"Question: {question}\n\nCandidates:\n\n"
        + "\n\n".join(excerpts)
    )

    try:
        resp = chat_completion(
            model=RERANK_MODEL,
            messages=[
                {"role": "system", "content": _SYSTEM},
                {"role": "user",   "content": user_msg},
            ],
            response_format={"type": "json_object"},
            temperature=0,
            max_tokens=600,
        )
        parsed = json.loads(resp.choices[0].message.content or "{}")
        # Tolerate {"scores":[...]} OR a bare list of {i,s} objects — some
        # smaller models pick the latter shape even when the prompt asks
        # for the former.
        if isinstance(parsed, list):
            score_items = parsed
        elif isinstance(parsed, dict):
            score_items = parsed.get("scores", [])
        else:
            score_items = []
        scores = {
            int(item["i"]): float(item["s"])
            for item in score_items
            if isinstance(item, dict) and "i" in item and "s" in item
        }
    except Exception as e:
        log.warning("Rerank failed (%s) — using hybrid ordering", e)
        return candidates[:top_k]

    for i, c in enumerate(candidates):
        c["rerank_score"] = scores.get(i, 0.0)

    candidates.sort(key=lambda x: x["rerank_score"], reverse=True)
    return candidates[:top_k]
