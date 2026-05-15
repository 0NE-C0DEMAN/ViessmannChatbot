"""Hypothetical Document Embeddings (HyDE).

For spec/capability questions, the user's *question* often looks very
different from the *answer* in embedding space:

    Q: "What is the COP at A7/W35?"            (sounds like a question)
    A: "Nazivni toplinski učin kW 3,56 4,48..." (looks like a table)

Cosine similarity between the two is moderate at best. HyDE side-steps this:
ask the LLM to *hallucinate* a plausible answer paragraph, embed THAT, and
use it as an extra retrieval query. The fake answer's embedding lands much
closer to the real answer chunk in space.

Cost: 1 extra gpt-4o-mini call (~$0.0002) + 1 extra embedding (~$0.00002).
Effect: noticeably better recall on cross-language and table-lookup queries.

We only trigger HyDE when the intent classifier says the question is a
spec or capability lookup (where the gap is biggest). Conversational /
procedural questions don't benefit much and we save the call.
"""
from __future__ import annotations

import logging

from ..config import EXPAND_MODEL
from ..openai_client import QuotaExhausted, chat_completion

log = logging.getLogger("retrieval")

_SYSTEM = """\
You generate a HYPOTHETICAL answer paragraph for a question about Viessmann
heating products (Vitocal heat pumps, Vitodens boilers). The paragraph is
NOT shown to the user — it is used only as a search query against technical
PDFs in Croatian.

Write a short Croatian paragraph that LOOKS LIKE the answer would look in
a real datasheet: include plausible model codes (101.B04, 101.A12, etc.),
units (kW, °C, bar, dB(A), MPa), and the kind of terminology a technical
manual would use (e.g. "Nazivni toplinski učin", "Rashladno sredstvo",
"Sigurnosna grupa"). Length: 2-4 sentences. Plausible values are fine —
they don't need to be correct, just realistic-looking.

Do NOT write "I don't know" or refuse. Output the paragraph only, no preamble.
"""


def hypothetical_doc(question: str) -> str | None:
    """Return a plausible hypothetical-answer paragraph, or None on failure."""
    try:
        resp = chat_completion(
            model=EXPAND_MODEL,
            messages=[
                {"role": "system", "content": _SYSTEM},
                {"role": "user",   "content": question},
            ],
            temperature=0.2,
            max_tokens=180,
        )
        text = (resp.choices[0].message.content or "").strip()
        return text or None
    except QuotaExhausted:
        # Bubble up — caller (retrieve) decides how to handle
        raise
    except Exception as e:
        log.warning("HyDE failed (%s) — skipping", e)
        return None
