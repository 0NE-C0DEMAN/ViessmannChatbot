"""Thin REST client for Supabase tables and RPCs.

We don't use `supabase-py` because the heavy lifting (vector + full-text)
already happens server-side in the `search_chunks_v2` SQL function — we just
need POST/DELETE/PATCH against PostgREST.
"""
from __future__ import annotations

import logging
from typing import Any

import requests

from .config import SUPABASE_SERVICE_KEY, SUPABASE_URL

log = logging.getLogger("supabase")

HEADERS = {
    "apikey":        SUPABASE_SERVICE_KEY,
    "Authorization": f"Bearer {SUPABASE_SERVICE_KEY}",
    "Content-Type":  "application/json",
}


# ─── Chunks ────────────────────────────────────────────────────────────────
def delete_chunks(file_id: str) -> None:
    r = requests.delete(
        f"{SUPABASE_URL}/rest/v1/document_chunks_v2",
        params={"file_id": f"eq.{file_id}"},
        headers=HEADERS, timeout=30,
    )
    if r.status_code >= 300:
        log.error("delete_chunks %s → %d %s", file_id, r.status_code, r.text[:300])
        r.raise_for_status()


def insert_chunks(rows: list[dict]) -> None:
    r = requests.post(
        f"{SUPABASE_URL}/rest/v1/document_chunks_v2",
        headers={**HEADERS, "Prefer": "return=minimal"},
        json=rows, timeout=60,
    )
    if r.status_code >= 300:
        log.error("insert_chunks → %d %s", r.status_code, r.text[:400])
        r.raise_for_status()


# ─── Registry ──────────────────────────────────────────────────────────────
def upsert_registry(meta: dict) -> None:
    r = requests.post(
        f"{SUPABASE_URL}/rest/v1/document_registry_v2",
        headers={**HEADERS, "Prefer": "resolution=merge-duplicates,return=minimal"},
        params={"on_conflict": "file_id"},
        json=meta, timeout=30,
    )
    if r.status_code >= 300:
        log.error("upsert_registry → %d %s", r.status_code, r.text[:300])
        r.raise_for_status()


def mark_deleted(file_id: str) -> None:
    """Soft-delete: keep registry row but flag it. Chunks stay untouched
    so operators can audit before purging."""
    r = requests.patch(
        f"{SUPABASE_URL}/rest/v1/document_registry_v2",
        params={"file_id": f"eq.{file_id}"},
        headers={**HEADERS, "Prefer": "return=minimal"},
        json={"status": "deleted"},
        timeout=30,
    )
    if r.status_code >= 300:
        log.error("mark_deleted → %d %s", r.status_code, r.text[:300])


def get_registry() -> list[dict]:
    """Return ALL registry rows (active and deleted)."""
    r = requests.get(
        f"{SUPABASE_URL}/rest/v1/document_registry_v2",
        params={"select": "file_id,file_name,md5_checksum,status"},
        headers=HEADERS, timeout=30,
    )
    r.raise_for_status()
    return r.json() or []


# ─── RPC ───────────────────────────────────────────────────────────────────
def call_rpc(name: str, payload: dict, timeout: int = 30) -> Any:
    """Call a Supabase Postgres function. Raises on HTTP error."""
    r = requests.post(
        f"{SUPABASE_URL}/rest/v1/rpc/{name}",
        headers=HEADERS, json=payload, timeout=timeout,
    )
    r.raise_for_status()
    return r.json()
