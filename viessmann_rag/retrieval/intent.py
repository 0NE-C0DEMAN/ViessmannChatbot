"""Query intent classifier.

Routes the user's question to the most appropriate document type(s) so the
hybrid search can prefer the right kind of source. This mirrors what
production RAG systems (Vertex AI Search, Perplexity, etc.) do when a
corpus has heterogeneous document types — a spec question shouldn't be
answered from an installation manual, and an install procedure shouldn't
be answered from a marketing datasheet.

The doc_types we route to here match the values produced by
`ingest.metadata.parse_metadata` and stored on `document_chunks_v2`:

    informacijski_list       (spec datasheet, canonical)
    upute_za_projektiranje   (engineering / planning guide)
    upute_za_montazu         (installation manual)
    upute_za_servis          (service / troubleshooting manual)
    upute_za_upotrebu        (end-user manual)

`classify()` returns the list of document types to prefer, in priority
order. The retrieval pipeline runs a first-pass search restricted to those
types; if that pool is too small or the LLM rerank has no confident hits,
it falls back to an unfiltered search.
"""
from __future__ import annotations

import json
import logging
from dataclasses import dataclass

from ..llm import EXPAND_MODEL, QuotaExhausted, chat_completion

log = logging.getLogger("retrieval")

_VALID_TYPES = {
    "informacijski_list",
    "upute_za_projektiranje",
    "upute_za_montazu",
    "upute_za_servis",
    "upute_za_upotrebu",
}


@dataclass
class Intent:
    """Result of classification."""
    category: str          # 'spec' | 'capability' | 'install' | 'service' | 'user' | 'design' | 'general'
    preferred: list[str]   # ordered preferred document_types

    @property
    def primary(self) -> str | None:
        return self.preferred[0] if self.preferred else None


_SYSTEM = """\
Classify the user's question about Viessmann heating products into ONE of these
intent categories, and pick the document types most likely to contain the
answer (in priority order).

Categories and their preferred document_types:

  spec         → informacijski_list, upute_za_projektiranje
                 (numerical specs: COP, SCOP, GWP, pressures, voltages,
                  dimensions, weights, sound levels, refrigerant charge,
                  fuse ratings, temperature limits, dB(A), refrigerant
                  safety group, model differences, model code lists, type
                  variants, capabilities)

  capability   → informacijski_list, upute_za_projektiranje
                 (which models support X, what variants exist, type overview)

  design       → upute_za_projektiranje, informacijski_list
                 (engineering sizing, hydraulic calculations, control logic,
                  system design, application context)

  install      → upute_za_montazu, upute_za_projektiranje
                 (installation steps, mounting, electrical hookup,
                  commissioning, refrigerant filling)

  service      → upute_za_servis, upute_za_montazu
                 (troubleshooting, error codes, fault diagnosis,
                  replacement procedures, maintenance)

  user         → upute_za_upotrebu, informacijski_list
                 (operating the device, daily use, hot-water settings,
                  alarms shown to the user)

  general      → (all types, no preference)
                 (greetings, off-topic, unclear, ambiguous, or covers
                  multiple categories)

Output JSON only:
  {"category": "spec", "preferred": ["informacijski_list", "upute_za_projektiranje"]}
"""


def classify(question: str) -> Intent:
    """Classify the user's question. Falls back to 'general' (no filter) on any
    error so the request still completes."""
    try:
        resp = chat_completion(
            model=EXPAND_MODEL,
            messages=[
                {"role": "system", "content": _SYSTEM},
                {"role": "user",   "content": question},
            ],
            response_format={"type": "json_object"},
            temperature=0,
            max_tokens=120,
        )
        parsed = json.loads(resp.choices[0].message.content or "{}")
        if isinstance(parsed, list):
            data = {}
            for item in parsed:
                if isinstance(item, dict):
                    data.update(item)
        elif isinstance(parsed, dict):
            data = parsed
        else:
            data = {}
    except QuotaExhausted:
        raise
    except Exception as e:
        log.warning("Intent classify failed (%s) — falling back to general", e)
        return Intent(category="general", preferred=[])

    category = (data.get("category") or "general").strip()
    raw_pref = data.get("preferred") or []
    preferred = [p for p in raw_pref if isinstance(p, str) and p in _VALID_TYPES]

    if not preferred and category != "general":
        log.warning("Intent classify returned no valid preferred types — coercing to general")
        category = "general"

    return Intent(category=category, preferred=preferred)
