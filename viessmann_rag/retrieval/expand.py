"""Multi-query expansion.

Given the user's question, ask gpt-4o-mini to produce:
  1. A Croatian paraphrase (or English paraphrase if the question is Croatian)
  2. A keyword-rich variant with technical terms and model codes

We then embed each variant and union the candidate pools — this is what closes
the cross-language retrieval gap on English questions against Croatian PDFs.
"""
from __future__ import annotations

import json
import logging

from ..config import EXPAND_MODEL
from ..openai_client import QuotaExhausted, chat_completion

log = logging.getLogger("retrieval")

_SYSTEM = (
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


def expand_query(question: str) -> list[str]:
    """Return [original, paraphrase, keywords]. Best-effort — falls back to
    just [original] if the LLM call fails."""
    try:
        resp = chat_completion(
            model=EXPAND_MODEL,
            messages=[
                {"role": "system", "content": _SYSTEM},
                {"role": "user",   "content": question},
            ],
            response_format={"type": "json_object"},
            temperature=0,
            max_tokens=200,
        )
        data = json.loads(resp.choices[0].message.content or "{}")
    except QuotaExhausted:
        # Propagate — caller (retrieve) decides whether to give up or fall
        # back to the original query. We never want to silently mask quota.
        raise
    except Exception as e:
        log.warning("Query expansion failed (%s) — using original only", e)
        return [question]

    out = [question]
    cro = (data.get("croatian") or "").strip()
    if cro and cro != question:
        out.append(cro)
    kw = (data.get("keywords") or "").strip()
    if kw:
        out.append(kw)
    return out
