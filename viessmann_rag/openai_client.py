"""OpenAI client + retry wrappers.

Two failure modes worth treating differently:
  - RateLimitError with type='tokens' / 'requests' → retry with backoff
  - RateLimitError with 'insufficient_quota'      → account out of credit;
    no point retrying, surface a clear error
"""
from __future__ import annotations

import logging
import re
import time
from typing import Any

from openai import OpenAI, RateLimitError

from .config import EMBEDDING_MODEL, OPENAI_API_KEY

log = logging.getLogger("openai")

# Single shared client (thread-safe per OpenAI SDK)
client = OpenAI(api_key=OPENAI_API_KEY)


class QuotaExhausted(RuntimeError):
    """Raised when OpenAI returns insufficient_quota — caller decides UX."""


def _parse_retry_after(err: RateLimitError) -> float | None:
    """Read 'try again in 4.926s' / 'try again in 200ms' out of the message."""
    try:
        m = re.search(r"try again in ([\d.]+)\s*(s|ms)", str(err))
        if not m:
            return None
        val = float(m.group(1))
        return val if m.group(2) == "s" else val / 1000.0
    except Exception:
        return None


def _is_quota(err: RateLimitError) -> bool:
    return "insufficient_quota" in str(err)


# ─── Embeddings ────────────────────────────────────────────────────────────
def embed(text: str, retries: int = 3) -> list[float]:
    """Embed a single string. Retries with backoff on rate limits."""
    for attempt in range(retries):
        try:
            r = client.embeddings.create(model=EMBEDDING_MODEL, input=text)
            return r.data[0].embedding
        except RateLimitError as e:
            if _is_quota(e):
                raise QuotaExhausted(str(e)) from e
            if attempt == retries - 1:
                raise
            wait = _parse_retry_after(e) or (2 ** attempt + 1)
            log.warning("Embed 429 (attempt %d/%d), sleeping %.1fs",
                        attempt + 1, retries, wait)
            time.sleep(wait)
        except Exception as e:
            if attempt == retries - 1:
                raise
            log.warning("Embed error (attempt %d/%d): %s — retrying",
                        attempt + 1, retries, e)
            time.sleep(2 ** attempt)
    raise RuntimeError("unreachable")


# ─── Chat completions ──────────────────────────────────────────────────────
def chat_completion(
    *,
    model: str,
    messages: list[dict],
    temperature: float = 0.1,
    max_tokens: int = 900,
    response_format: dict | None = None,
    retries: int = 4,
) -> Any:
    """Wrapper around client.chat.completions.create with 429 retry.

    Raises QuotaExhausted on insufficient_quota so the chat server can return
    a user-friendly 503 instead of a 500.
    """
    kwargs: dict[str, Any] = {
        "model":       model,
        "messages":    messages,
        "temperature": temperature,
        "max_tokens":  max_tokens,
    }
    if response_format is not None:
        kwargs["response_format"] = response_format

    for attempt in range(retries):
        try:
            return client.chat.completions.create(**kwargs)
        except RateLimitError as e:
            if _is_quota(e):
                raise QuotaExhausted(str(e)) from e
            if attempt == retries - 1:
                raise
            wait = _parse_retry_after(e) or (2 ** attempt + 1)
            log.warning("%s 429 (attempt %d/%d), sleeping %.1fs",
                        model, attempt + 1, retries, wait)
            time.sleep(wait)
    raise RuntimeError("unreachable")
