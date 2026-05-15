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

# ─── OpenAI ────────────────────────────────────────────────────────────────
OPENAI_API_KEY        = _required("OPENAI_API_KEY")

# Models — change here, not in business logic
EMBEDDING_MODEL = "text-embedding-3-small"   # 1536-dim, matches migration.sql
RERANK_MODEL    = "gpt-4o-mini"              # cheap, fast, good enough
EXPAND_MODEL    = "gpt-4o-mini"              # multi-query expansion
CHAT_MODEL      = "gpt-4o"                   # final answer generation

# ─── Chat server ───────────────────────────────────────────────────────────
CHAT_USERNAME    = os.environ.get("CHAT_USERNAME", "viessmann")
CHAT_PASSWORD    = os.environ.get("CHAT_PASSWORD", "carrier")
FLASK_SECRET_KEY = os.environ.get("FLASK_SECRET_KEY", "viessmann-rag-secret")
CHAT_PORT        = int(os.environ.get("CHAT_PORT", 8081))

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
