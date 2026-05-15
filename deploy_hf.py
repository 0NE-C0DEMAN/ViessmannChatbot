"""One-shot HF Spaces deploy.

Reads secrets from .env, creates the Space (idempotent), sets all required
Space-side env vars, then uploads the repo contents (ignoring local secrets
and dev artifacts).

Run once: `py -3.11 deploy_hf.py`
"""
from __future__ import annotations

import os
import secrets
from pathlib import Path

from dotenv import load_dotenv
from huggingface_hub import HfApi

REPO_ROOT = Path(__file__).resolve().parent
load_dotenv(REPO_ROOT / ".env")

HF_TOKEN  = os.environ["HF_TOKEN"]               # exported from shell
REPO_ID   = "SamTwo/viessmann-rag"
SDK       = "docker"

# Secrets the deployed Space needs at runtime.
# Drive vars are included so the Space also runs `ingest.py --drive --loop`
# in the background and auto-syncs new PDFs from Frane's Drive to Supabase.
service_account_json = (REPO_ROOT / "google_service_account.json").read_text(encoding="utf-8")

# Note: FLASK_SECRET_KEY is intentionally NOT re-set on redeploys — rotating
# it would invalidate every active browser session. Set once on first deploy
# (uncomment the line below); leave commented thereafter.
SECRETS = {
    "SUPABASE_URL":               os.environ["SUPABASE_URL"],
    "SUPABASE_SERVICE_KEY":       os.environ["SUPABASE_SERVICE_KEY"],
    "OPENAI_API_KEY":             os.environ["OPENAI_API_KEY"],
    "CHAT_USERNAME":              "Frane",
    "CHAT_PASSWORD":              "Frane@123",
    # "FLASK_SECRET_KEY":         secrets.token_urlsafe(48),
    "GOOGLE_SERVICE_ACCOUNT_JSON": service_account_json,
    "GOOGLE_ROOT_FOLDER_ID":       os.environ["GOOGLE_ROOT_FOLDER_ID"],
    "POLL_INTERVAL_SECONDS":       "300",
}

# Stuff we never want uploaded.
IGNORE = [
    ".env",
    ".git/*",
    ".gitignore",
    "google_service_account.json",
    "google_token.json",
    "logs/*",
    "logs",
    "venv/*",
    ".venv/*",
    "__pycache__/*",
    "**/__pycache__/*",
    "*.pyc",
    "deploy_hf.py",   # don't ship this script
]

api = HfApi(token=HF_TOKEN)

print(f"[1/3] Creating Space {REPO_ID} (idempotent)...")
api.create_repo(
    repo_id=REPO_ID,
    repo_type="space",
    space_sdk=SDK,
    exist_ok=True,
    private=False,
)
print("      ok")

print(f"[2/3] Setting {len(SECRETS)} Space secrets...")
for k, v in SECRETS.items():
    api.add_space_secret(repo_id=REPO_ID, key=k, value=v)
    print(f"      set {k}")

print(f"[3/3] Uploading repo contents...")
api.upload_folder(
    folder_path=str(REPO_ROOT),
    repo_id=REPO_ID,
    repo_type="space",
    ignore_patterns=IGNORE,
    commit_message="Initial deploy from local repo",
)
print("      done")

print(f"\nLive Space: https://huggingface.co/spaces/{REPO_ID}")
