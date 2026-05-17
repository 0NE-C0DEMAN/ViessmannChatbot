"""Centralized configuration.

Loads `.env` from the repository root the first time this module is imported.
All other modules read constants from here — never from `os.environ` directly.
"""
from __future__ import annotations

import os
from pathlib import Path

from dotenv import load_dotenv

# Repo root = parent of the package
REPO_ROOT = Path(__file__).resolve().parent.parent
WEB_DIR   = REPO_ROOT / "web"
LOG_DIR   = REPO_ROOT / "logs"

# Load .env from the repo root
load_dotenv(REPO_ROOT / ".env")


def _required(name: str) -> str:
    val = os.environ.get(name)
    if not val:
        raise SystemExit(
            f"Missing required environment variable: {name}\n"
            f"Copy .env.example to .env and fill in your values."
        )
    return val


# ─── Supabase ──────────────────────────────────────────────────────────────
SUPABASE_URL          = _required("SUPABASE_URL")
SUPABASE_SERVICE_KEY  = _required("SUPABASE_SERVICE_KEY")

# ─── LLM provider switch ───────────────────────────────────────────────────
# "openai"  → gpt-4o + text-embedding-3-small  (default, paid)
# "gemini"  → gemma-4-26b-a4b-it + gemini-embedding-001  (free tier)
# Picked once at process start so retrieval + ingest + chat all agree.
LLM_PROVIDER = os.environ.get("LLM_PROVIDER", "openai").lower()
if LLM_PROVIDER not in ("openai", "gemini"):
    raise SystemExit(f"LLM_PROVIDER must be 'openai' or 'gemini', got: {LLM_PROVIDER}")

# When INGEST_DUAL=true, every ingest run embeds each chunk with BOTH
# providers and writes to both columns. Lets us hot-swap LLM_PROVIDER later
# without re-ingesting. Free-side cost is ~0; OpenAI side adds the usual
# ~$0.00004/page when its column is still empty.
INGEST_DUAL = os.environ.get("INGEST_DUAL", "false").lower() in ("1", "true", "yes")

# ─── OpenAI ────────────────────────────────────────────────────────────────
# Required when LLM_PROVIDER=openai OR INGEST_DUAL=true. Optional otherwise.
if LLM_PROVIDER == "openai" or INGEST_DUAL:
    OPENAI_API_KEY = _required("OPENAI_API_KEY")
else:
    OPENAI_API_KEY = os.environ.get("OPENAI_API_KEY", "")

EMBEDDING_MODEL = "text-embedding-3-small"   # 1536-dim, OpenAI side
RERANK_MODEL    = "gpt-4o-mini"
EXPAND_MODEL    = "gpt-4o-mini"
CHAT_MODEL      = "gpt-4o"

# ─── Gemini ────────────────────────────────────────────────────────────────
# Required when LLM_PROVIDER=gemini OR INGEST_DUAL=true. Optional otherwise.
if LLM_PROVIDER == "gemini" or INGEST_DUAL:
    GEMINI_API_KEY = _required("GEMINI_API_KEY")
else:
    GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY", "")

# Gemma 4 26B A4B is Google's "highest free-tier rate limits" recommendation
# (per ParkerJones config + Gemini API docs). Embeddings use gemini-embedding
# with output_dimensionality=1536 so the column type stays vector(1536) —
# both columns interchangeable at storage level.
# Two-model split inside the Gemma family:
#   - Gemma 4 26B A4B for the FINAL ANSWER. Best quality (matches gpt-4o
#     on our eval). One call per question, so the mandatory thinking
#     phase (~5-10s) is paid once.
#   - Gemma 3 27B for the FOUR retrieval helpers (intent/expand/HyDE/
#     rerank). No thinking phase (Gemma 3 is the previous, non-thinking
#     generation) → ~1-2s each → ~4-8s total for the helpers.
# Total expected per-question latency: ~10-15s end-to-end.
#
# Quota math (Gemini free tier, May 2026):
#   - gemma-4-26b-a4b-it: 1,500 RPD → ~1,500 questions/day cap on answers
#   - gemma-3-27b-it:    14,400 RPD → 4× helpers/q = 3,600 q/day cap
# So the binding limit is the answer model at 1,500/day — plenty for a
# few coworkers.
#
# Caveat: neither Gemma 3 nor Gemma 4 supports `responseMimeType:
# application/json` on the Gemini API (it returns 400). Our gemini_client
# detects gemma prefixes and drops that param; helper prompts already
# enforce JSON structurally and the parsers tolerate fenced ```json blocks.
GEMINI_CHAT_MODEL      = os.environ.get("GEMINI_CHAT_MODEL",   "gemma-4-26b-a4b-it")
# Helpers want a non-thinking, JSON-mode-capable model. Gemma 3 is not
# available on the free tier (verified via models.list API); only Gemma 4
# is, and Gemma 4 always thinks. So helpers go to Gemini 2.5 Flash-Lite
# (1,000 RPD, ~1s per call, native JSON mode). Answer stays on Gemma 4.
GEMINI_RERANK_MODEL    = os.environ.get("GEMINI_RERANK_MODEL", "gemini-2.5-flash")
GEMINI_EMBEDDING_MODEL = os.environ.get("GEMINI_EMBEDDING_MODEL", "gemini-embedding-001")
GEMINI_EMBEDDING_DIM   = 1536

# ─── Chat server ───────────────────────────────────────────────────────────
CHAT_USERNAME    = os.environ.get("CHAT_USERNAME", "viessmann")
CHAT_PASSWORD    = os.environ.get("CHAT_PASSWORD", "carrier")
FLASK_SECRET_KEY = os.environ.get("FLASK_SECRET_KEY", "viessmann-rag-secret")
# PORT (HF Spaces / generic PaaS standard) wins if set, otherwise CHAT_PORT,
# otherwise 8081 for local dev.
CHAT_PORT        = int(os.environ.get("PORT") or os.environ.get("CHAT_PORT") or 8081)

# ─── Google Drive (optional — only required for `ingest.py --drive`) ───────
GOOGLE_CLIENT_ID      = os.environ.get("GOOGLE_CLIENT_ID")
GOOGLE_CLIENT_SECRET  = os.environ.get("GOOGLE_CLIENT_SECRET")
GOOGLE_ROOT_FOLDER_ID = os.environ.get("GOOGLE_ROOT_FOLDER_ID")
POLL_INTERVAL_SECONDS = int(os.environ.get("POLL_INTERVAL_SECONDS", 60))
GOOGLE_TOKEN_FILE           = REPO_ROOT / "google_token.json"
GOOGLE_SERVICE_ACCOUNT_FILE = REPO_ROOT / os.environ.get(
    "GOOGLE_SERVICE_ACCOUNT_FILE", "google_service_account.json"
)
GOOGLE_SCOPES               = ["https://www.googleapis.com/auth/drive.readonly"]


def service_account_configured() -> bool:
    """True iff a service account JSON exists at GOOGLE_SERVICE_ACCOUNT_FILE."""
    return GOOGLE_SERVICE_ACCOUNT_FILE.exists()


def drive_configured() -> bool:
    """True iff Drive ingest can be attempted with either credential type.

    Service account requires only the JSON file (it contains client_id /
    private_key) and a GOOGLE_ROOT_FOLDER_ID. OAuth user mode requires the
    client_id / secret env vars too.
    """
    if service_account_configured() and GOOGLE_ROOT_FOLDER_ID:
        return True
    return bool(GOOGLE_CLIENT_ID and GOOGLE_CLIENT_SECRET and GOOGLE_ROOT_FOLDER_ID)


# ─── Retrieval tuning ──────────────────────────────────────────────────────
HYBRID_CANDIDATE_COUNT  = 50    # per query variant
DIVERSIFY_MAX_PER_FILE  = 4     # cap when several files compete
RERANK_TOP_K            = 10    # what we ultimately send to gpt-4o
RERANK_EXCERPT_CHARS    = 1500  # how much of each chunk to show the reranker
RERANK_CONFIDENCE_FLOOR = 5.0   # if the best rerank score is below this,
                                # treat as "no real answer" and refuse —
                                # stops gpt-4o confabulating off noise
SEMANTIC_WEIGHT         = 0.7   # vector vs full-text balance in the hybrid RPC

# ─── Ingest tuning ─────────────────────────────────────────────────────────
INGEST_BATCH_SIZE = 10   # insert chunks this many at a time
