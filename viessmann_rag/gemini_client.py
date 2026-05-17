"""Gemini / Gemma client — matches the openai_client.py public surface.

Exposes:
  - embed(text)              → list[float], length GEMINI_EMBEDDING_DIM
  - chat_completion(...)     → object with .choices[0].message.content
  - chat_stream(...)         → generator yielding delta strings
  - QuotaExhausted           → mirror of openai_client.QuotaExhausted

Notes / gotchas baked in here (cribbed from ParkerJones):
  • Gemma models on the Gemini API DO NOT accept `responseMimeType`. We
    detect a Gemma model id and silently drop JSON-mode; callers can still
    parse fenced ```json blocks with their existing fallback logic.
  • Gemma also doesn't accept a top-level system role. We fold system
    content into the first user message as a "System:\n...\n\nUser:\n..."
    block.
  • Embedding endpoint is :batchEmbedContents for multi, :embedContent for
    single. We always use the single endpoint — pipeline embeds page by page.
"""
from __future__ import annotations

import json
import logging
import re
import time
from typing import Any, Iterator

import requests

from .config import (
    GEMINI_API_KEY,
    GEMINI_CHAT_MODEL,
    GEMINI_EMBEDDING_DIM,
    GEMINI_EMBEDDING_MODEL,
)

log = logging.getLogger("gemini")

_BASE = "https://generativelanguage.googleapis.com/v1beta/models"


class QuotaExhausted(RuntimeError):
    """Raised when Gemini returns RESOURCE_EXHAUSTED on the daily quota."""


def _is_gemma(model: str) -> bool:
    return model.startswith("gemma")


def _retry_after_seconds(err_body: str) -> float | None:
    """Pull 'retryDelay' out of a Gemini error body if present."""
    m = re.search(r'"retryDelay"\s*:\s*"(\d+(?:\.\d+)?)s"', err_body)
    return float(m.group(1)) if m else None


def _request(
    url: str, body: dict, *, retries: int = 4, timeout: int = 120,
) -> dict:
    """POST with quota-aware retry. Quota = bail; other 429 = backoff."""
    last_exc: Exception | None = None
    for attempt in range(retries):
        try:
            r = requests.post(
                url, json=body, timeout=timeout,
                headers={"Content-Type": "application/json"},
            )
        except requests.RequestException as e:
            last_exc = e
            if attempt == retries - 1:
                raise
            time.sleep(2 ** attempt)
            continue

        if r.status_code == 429:
            body_text = r.text
            # The daily quota error mentions "FreeTier" / "RPD" / "PerDay".
            if ("PerDay" in body_text or "RPD" in body_text
                    or "daily" in body_text.lower()):
                raise QuotaExhausted(body_text[:400])
            wait = _retry_after_seconds(body_text) or (2 ** attempt + 1)
            log.warning("Gemini 429 (attempt %d/%d), sleeping %.1fs",
                        attempt + 1, retries, wait)
            time.sleep(wait)
            continue

        if r.status_code >= 500 and attempt < retries - 1:
            log.warning("Gemini %d (attempt %d/%d), retrying",
                        r.status_code, attempt + 1, retries)
            time.sleep(2 ** attempt)
            continue

        r.raise_for_status()
        return r.json()

    if last_exc:
        raise last_exc
    raise RuntimeError("unreachable")


# ─── Embeddings ────────────────────────────────────────────────────────────
def embed(text: str, retries: int = 3) -> list[float]:
    """Embed `text` with gemini-embedding-001 at GEMINI_EMBEDDING_DIM dims."""
    url = (f"{_BASE}/{GEMINI_EMBEDDING_MODEL}:embedContent"
           f"?key={GEMINI_API_KEY}")
    body = {
        "content": {"parts": [{"text": text}]},
        "outputDimensionality": GEMINI_EMBEDDING_DIM,
        "taskType": "RETRIEVAL_DOCUMENT",
    }
    resp = _request(url, body, retries=retries)
    values = resp.get("embedding", {}).get("values") or []
    if len(values) != GEMINI_EMBEDDING_DIM:
        raise RuntimeError(
            f"Gemini returned {len(values)}-dim embedding, expected "
            f"{GEMINI_EMBEDDING_DIM}"
        )
    return values


def embed_query(text: str, retries: int = 3) -> list[float]:
    """Same as embed() but tags the request as a search-query task type."""
    url = (f"{_BASE}/{GEMINI_EMBEDDING_MODEL}:embedContent"
           f"?key={GEMINI_API_KEY}")
    body = {
        "content": {"parts": [{"text": text}]},
        "outputDimensionality": GEMINI_EMBEDDING_DIM,
        "taskType": "RETRIEVAL_QUERY",
    }
    resp = _request(url, body, retries=retries)
    return resp.get("embedding", {}).get("values") or []


# ─── Messages adapter (OpenAI shape → Gemini shape) ────────────────────────
def _to_gemini_contents(
    messages: list[dict], *, model: str,
) -> tuple[list[dict], str | None]:
    """Convert [{role, content}] → ([{role, parts:[{text}]}], system_str).

    Gemma can't take a top-level system role, so we return the system text
    separately. The caller folds it into the first user message.
    """
    system_chunks: list[str] = []
    out: list[dict] = []
    for m in messages:
        role = m.get("role", "user")
        content = m.get("content", "")
        if role == "system":
            system_chunks.append(content)
            continue
        gem_role = "model" if role == "assistant" else "user"
        out.append({"role": gem_role, "parts": [{"text": content}]})

    system_str = "\n\n".join(s for s in system_chunks if s).strip() or None

    if _is_gemma(model) and system_str and out:
        # Prepend system to the first user message — Gemma reads it fine
        first = out[0]
        if first["role"] == "user" and first["parts"]:
            first["parts"][0]["text"] = (
                f"System:\n{system_str}\n\nUser:\n{first['parts'][0]['text']}"
            )
            system_str = None  # consumed

    return out, system_str


# ─── Mock OpenAI-shaped response object ────────────────────────────────────
class _Message:
    __slots__ = ("content",)
    def __init__(self, content: str) -> None:
        self.content = content

class _Choice:
    __slots__ = ("message", "finish_reason")
    def __init__(self, message: _Message, finish_reason: str) -> None:
        self.message = message
        self.finish_reason = finish_reason

class _Response:
    __slots__ = ("choices",)
    def __init__(self, choices: list[_Choice]) -> None:
        self.choices = choices


# ─── Chat completion (non-streaming) ───────────────────────────────────────
def chat_completion(
    *,
    model: str | None = None,
    messages: list[dict],
    temperature: float = 0.1,
    max_tokens: int = 900,
    response_format: dict | None = None,
    retries: int = 4,
) -> _Response:
    """Drop-in replacement for openai_client.chat_completion.

    `response_format={"type": "json_object"}` is silently dropped for Gemma
    models (they reject it). Callers that need JSON should also tolerate
    fenced ```json blocks in the output.
    """
    model = model or GEMINI_CHAT_MODEL
    contents, system_str = _to_gemini_contents(messages, model=model)

    generation_config: dict[str, Any] = {
        "temperature":     temperature,
        "maxOutputTokens": max_tokens,
    }
    if response_format and not _is_gemma(model):
        if response_format.get("type") == "json_object":
            generation_config["responseMimeType"] = "application/json"

    body: dict[str, Any] = {
        "contents":         contents,
        "generationConfig": generation_config,
    }
    if system_str and not _is_gemma(model):
        body["systemInstruction"] = {"parts": [{"text": system_str}]}

    url = f"{_BASE}/{model}:generateContent?key={GEMINI_API_KEY}"
    resp = _request(url, body, retries=retries)

    candidates = resp.get("candidates") or []
    if not candidates:
        return _Response(choices=[_Choice(_Message(""), "stop")])
    cand = candidates[0]
    parts = cand.get("content", {}).get("parts") or []
    text = "".join(p.get("text", "") for p in parts)
    finish = cand.get("finishReason", "stop").lower()
    return _Response(choices=[_Choice(_Message(text), finish)])


# ─── Streaming chat (for the /api/chat/stream SSE endpoint) ────────────────
def chat_stream(
    *,
    model: str | None = None,
    messages: list[dict],
    temperature: float = 0.1,
    max_tokens: int = 900,
) -> Iterator[str]:
    """Yield text deltas from the Gemini streaming endpoint.

    Returns an iterator of strings (the new tokens). Mirrors the OpenAI
    streaming generator's external behavior so the chat server's loop
    works unchanged regardless of provider.
    """
    model = model or GEMINI_CHAT_MODEL
    contents, system_str = _to_gemini_contents(messages, model=model)

    body: dict[str, Any] = {
        "contents": contents,
        "generationConfig": {
            "temperature":     temperature,
            "maxOutputTokens": max_tokens,
        },
    }
    if system_str and not _is_gemma(model):
        body["systemInstruction"] = {"parts": [{"text": system_str}]}

    url = (f"{_BASE}/{model}:streamGenerateContent"
           f"?alt=sse&key={GEMINI_API_KEY}")
    with requests.post(
        url, json=body, stream=True, timeout=120,
        headers={"Content-Type": "application/json"},
    ) as r:
        if r.status_code == 429:
            body_text = r.text
            if "PerDay" in body_text or "daily" in body_text.lower():
                raise QuotaExhausted(body_text[:400])
            raise RuntimeError(f"Gemini stream 429: {body_text[:200]}")
        r.raise_for_status()

        for raw_line in r.iter_lines(decode_unicode=True):
            if not raw_line or not raw_line.startswith("data: "):
                continue
            payload = raw_line[6:].strip()
            if not payload or payload == "[DONE]":
                continue
            try:
                ev = json.loads(payload)
            except json.JSONDecodeError:
                continue
            for cand in ev.get("candidates") or []:
                for part in cand.get("content", {}).get("parts") or []:
                    text = part.get("text")
                    if text:
                        yield text
