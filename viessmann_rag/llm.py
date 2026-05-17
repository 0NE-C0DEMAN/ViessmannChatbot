"""Provider-agnostic facade — call sites import from here, not the
concrete openai_client / gemini_client.

Two reasons this is a thin module:
  1. We pick the provider once at import time from the LLM_PROVIDER env var
     and never switch mid-process. So a module-level re-export is fine; no
     dispatch overhead per call.
  2. Both clients expose the SAME public surface (embed, chat_completion,
     chat_stream, QuotaExhausted), so the rest of the code is provider-
     blind by design.

Special-purpose exports:
  - embed_query: prefers Gemini's RETRIEVAL_QUERY task type when on Gemini;
    on OpenAI it's identical to embed() (no asymmetric encoding).
  - SEARCH_RPC: which Supabase RPC retrieval should call, so we hit the
    column matching the active provider.

For dual-mode ingest we still need both raw clients, so we expose them as
`openai_raw` / `gemini_raw` for that one use-site (ingest/pipeline.py).
"""
from __future__ import annotations

from .config import LLM_PROVIDER

if LLM_PROVIDER == "gemini":
    from . import gemini_client as _impl
    from .gemini_client import (
        QuotaExhausted,
        chat_completion,
        chat_stream,
        embed,
        embed_query,
    )
    from .config import GEMINI_CHAT_MODEL, GEMINI_RERANK_MODEL
    # Re-export model IDs under the provider-neutral names so the retrieval
    # modules and chat server don't have to branch on LLM_PROVIDER.
    CHAT_MODEL   = GEMINI_CHAT_MODEL
    RERANK_MODEL = GEMINI_RERANK_MODEL
    EXPAND_MODEL = GEMINI_RERANK_MODEL   # mini tasks share the rerank model
    SEARCH_RPC = "search_chunks_v2_gem"
    EMBEDDING_COLUMN = "embedding_gem"
else:
    from . import openai_client as _impl
    from .openai_client import (
        QuotaExhausted,
        chat_completion,
        embed,
    )
    from .config import (
        CHAT_MODEL as CHAT_MODEL,
        EXPAND_MODEL as EXPAND_MODEL,
        RERANK_MODEL as RERANK_MODEL,
    )

    # OpenAI side doesn't need a different query embedding; alias it.
    embed_query = embed

    # OpenAI SDK doesn't expose a single-function streaming wrapper, so
    # roll one ourselves that matches the gemini_client.chat_stream shape
    # (iterator of delta strings). Lets chat/server.py use the same
    # streaming code regardless of provider.
    from .openai_client import client as _oai_client

    def chat_stream(
        *,
        model: str,
        messages: list[dict],
        temperature: float = 0.1,
        max_tokens: int = 900,
    ):
        stream = _oai_client.chat.completions.create(
            model=model, messages=messages,
            temperature=temperature, max_tokens=max_tokens,
            stream=True,
        )
        for chunk in stream:
            delta = (chunk.choices[0].delta.content
                     if chunk.choices else None)
            if delta:
                yield delta

    SEARCH_RPC = "search_chunks_v2"
    EMBEDDING_COLUMN = "embedding"


# Raw clients exposed for dual-ingest only (pipeline.py). Other call sites
# should use the dispatched functions above.
def _get_dual_clients():
    """Returns (openai_module, gemini_module). Used by ingest dual mode."""
    from . import openai_client as oai
    from . import gemini_client as gem
    return oai, gem
