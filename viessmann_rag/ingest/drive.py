"""Google Drive ingest mode — OAuth, diff against registry, fetch, ingest.

Mirrors the original `viessmann_processor.py` so existing OAuth credentials
and `google_token.json` files transfer over without re-authorization.
"""
from __future__ import annotations

import io
import logging
import time
from typing import Any

from ..config import (
    GOOGLE_CLIENT_ID,
    GOOGLE_CLIENT_SECRET,
    GOOGLE_ROOT_FOLDER_ID,
    GOOGLE_SCOPES,
    GOOGLE_SERVICE_ACCOUNT_FILE,
    GOOGLE_TOKEN_FILE,
    POLL_INTERVAL_SECONDS,
    drive_configured,
    service_account_configured,
)
from ..supabase_client import get_registry, mark_deleted
from .pipeline import process_pdf_bytes

log = logging.getLogger("ingest")


# ─── OAuth ─────────────────────────────────────────────────────────────────
def get_drive_service() -> Any:
    """Return an authenticated Drive v3 service.

    Two credential types are supported (in priority order):

    1. **Service account** — if `google_service_account.json` exists next
       to the script (or wherever `GOOGLE_SERVICE_ACCOUNT_FILE` points).
       This is the preferred mode for production: no browser, no token
       expiry, no test-user limits. The Drive folder MUST be shared with
       the service account's `client_email` for it to see any files.

    2. **OAuth user** — if no service account is configured, fall back to
       `InstalledAppFlow`. Reuses `google_token.json` if present; opens a
       browser for fresh consent if not (or if the cached refresh token
       has been revoked at Google's end).
    """
    # Lazy-import so users who only run --dir don't need the google libs.
    from googleapiclient.discovery import build

    if not drive_configured():
        raise SystemExit(
            "Drive mode requires either a service account JSON file OR "
            "GOOGLE_CLIENT_ID + GOOGLE_CLIENT_SECRET in .env, plus "
            "GOOGLE_ROOT_FOLDER_ID."
        )

    if service_account_configured():
        from google.oauth2 import service_account
        log.info("Drive auth: service account (%s)", GOOGLE_SERVICE_ACCOUNT_FILE.name)
        creds = service_account.Credentials.from_service_account_file(
            str(GOOGLE_SERVICE_ACCOUNT_FILE),
            scopes=GOOGLE_SCOPES,
        )
        return build("drive", "v3", credentials=creds)

    return _get_drive_service_oauth_user(build)


def _get_drive_service_oauth_user(build_fn) -> Any:
    """OAuth user flow — `InstalledAppFlow` + cached `google_token.json`."""
    from google.auth.exceptions import RefreshError
    from google.auth.transport.requests import Request
    from google.oauth2.credentials import Credentials
    from google_auth_oauthlib.flow import InstalledAppFlow

    log.info("Drive auth: OAuth user flow")

    client_config = {
        "installed": {
            "client_id":     GOOGLE_CLIENT_ID,
            "client_secret": GOOGLE_CLIENT_SECRET,
            "redirect_uris": ["urn:ietf:wg:oauth:2.0:oob", "http://localhost"],
            "auth_uri":      "https://accounts.google.com/o/oauth2/auth",
            "token_uri":     "https://oauth2.googleapis.com/token",
        }
    }

    creds = None
    if GOOGLE_TOKEN_FILE.exists():
        creds = Credentials.from_authorized_user_file(GOOGLE_TOKEN_FILE, GOOGLE_SCOPES)

    if not creds or not creds.valid:
        # Try a normal refresh first.
        refreshed = False
        if creds and creds.expired and creds.refresh_token:
            try:
                creds.refresh(Request())
                refreshed = True
            except RefreshError as e:
                log.warning(
                    "Cached refresh token is no longer valid (%s) — "
                    "starting fresh OAuth consent flow.", e,
                )
                creds = None

        # Fresh consent flow (opens browser).
        if not refreshed:
            flow = InstalledAppFlow.from_client_config(client_config, GOOGLE_SCOPES)
            creds = flow.run_local_server(port=0)

        GOOGLE_TOKEN_FILE.write_text(creds.to_json())
        log.info("Google token saved → %s", GOOGLE_TOKEN_FILE)

    return build_fn("drive", "v3", credentials=creds)


# ─── Drive listing helpers ─────────────────────────────────────────────────
def list_subfolders(service: Any, parent_id: str) -> list[dict]:
    """All direct subfolders of `parent_id`."""
    results: list[dict] = []
    page_token = None
    q = (f"mimeType='application/vnd.google-apps.folder' "
         f"and trashed=false and '{parent_id}' in parents")
    while True:
        resp = service.files().list(
            q=q, fields="nextPageToken, files(id, name)",
            pageToken=page_token,
        ).execute()
        results.extend(resp.get("files", []))
        page_token = resp.get("nextPageToken")
        if not page_token:
            break
    return results


def list_pdfs(service: Any) -> list[dict]:
    """All PDFs the OAuth account can see. The caller filters by parent."""
    results: list[dict] = []
    page_token = None
    q = "mimeType='application/pdf' and trashed=false"
    while True:
        resp = service.files().list(
            q=q,
            fields="nextPageToken, files(id, name, parents, md5Checksum, modifiedTime)",
            pageToken=page_token,
        ).execute()
        results.extend(resp.get("files", []))
        page_token = resp.get("nextPageToken")
        if not page_token:
            break
    return results


def download_pdf(service: Any, file_id: str) -> bytes:
    from googleapiclient.http import MediaIoBaseDownload

    req = service.files().get_media(fileId=file_id)
    buf = io.BytesIO()
    downloader = MediaIoBaseDownload(buf, req)
    done = False
    while not done:
        _, done = downloader.next_chunk()
    return buf.getvalue()


# ─── Sync logic ────────────────────────────────────────────────────────────
def _diff_against_registry(
    drive_files: list[dict],
    registry: list[dict],
) -> tuple[list[dict], list[dict]]:
    """Returns (to_process, to_mark_deleted).

    A Drive file is processed when:
      - it's new (no row matches its file_id OR its md5), OR
      - its md5 has changed since last ingest (under the same file_id).

    Critically, an md5 already present in the registry under a DIFFERENT
    file_id is treated as "already ingested" and skipped. This makes
    ingestion idempotent across re-keys — e.g. a corpus first loaded
    with `--dir` (filename-stem file_ids) won't be re-embedded when the
    same content is later pulled via `--drive` (Drive-API file_ids).

    A registry row is marked deleted only when neither its file_id nor
    its md5 appears in Drive — otherwise the content is still there,
    just under a different key.
    """
    drive_by_id     = {f["id"]: f for f in drive_files}
    drive_md5s      = {f["md5Checksum"] for f in drive_files
                       if f.get("md5Checksum")}
    registry_by_id  = {r["file_id"]: r for r in registry if r.get("file_id")}
    registry_by_md5 = {r["md5_checksum"]: r for r in registry
                       if r.get("md5_checksum")}

    to_process: list[dict] = []
    for f in drive_files:
        md5 = f.get("md5Checksum") or ""
        reg_by_id = registry_by_id.get(f["id"])

        # Case 1: same Drive file_id already registered.
        if reg_by_id:
            if reg_by_id.get("md5_checksum") != md5:
                log.info("  re-ingest %s — md5 changed", f.get("name"))
                to_process.append(f)
            # else: same file_id + same md5 → no-op
            continue

        # Case 2: different file_id, but content already ingested under
        # another key (e.g. originally from --dir). Skip embedding.
        if md5 and md5 in registry_by_md5:
            existing = registry_by_md5[md5]
            log.info("  skip %s — content already ingested as %s (md5 match)",
                     f.get("name"), existing.get("file_name"))
            continue

        # Case 3: truly new file (new id AND new content).
        to_process.append(f)

    # Deletion: row is "gone" only if BOTH its file_id and its md5 are
    # absent from the current Drive listing. If a different file_id now
    # carries the same content, we keep the old row — chunks are valid.
    to_delete = [
        r for r in registry
        if r.get("file_id")
           and r["file_id"] not in drive_by_id
           and (r.get("md5_checksum") or "") not in drive_md5s
           and r.get("status") != "deleted"
    ]
    return to_process, to_delete


def run_once(
    service: Any,
    *,
    on_scan_done: Any = None,
    on_file_start: Any = None,
    on_file_done: Any = None,
) -> None:
    """One full Drive scan + ingest pass.

    The optional `on_*` callbacks let an outer controller (progress.py)
    broadcast events to UI subscribers. They're keyword-only and default
    to None so the CLI entry point keeps working without a wrapper.

        on_scan_done(found_count, to_process_count, to_delete_count)
        on_file_start(file_name, idx, total)        # idx is 1-based
        on_file_done(file_name, idx, total, ok)     # ok: bool
    """
    log.info("=== Drive sync run ===")

    subfolders = list_subfolders(service, GOOGLE_ROOT_FOLDER_ID)  # type: ignore[arg-type]
    valid_parents = {GOOGLE_ROOT_FOLDER_ID} | {f["id"] for f in subfolders}
    log.info("Root + %d subfolders", len(subfolders))

    all_pdfs = list_pdfs(service)
    drive_files = [
        f for f in all_pdfs
        if any(p in valid_parents for p in (f.get("parents") or []))
    ]
    log.info("PDFs in folder tree: %d", len(drive_files))

    registry = get_registry()
    to_process, to_delete = _diff_against_registry(drive_files, registry)
    log.info("To process: %d  |  To mark deleted: %d",
             len(to_process), len(to_delete))

    if on_scan_done:
        try:
            on_scan_done(len(drive_files), len(to_process), len(to_delete))
        except Exception as e:  # noqa: BLE001
            log.warning("on_scan_done callback raised: %s", e)

    for reg in to_delete:
        log.info("  marking deleted: %s", reg.get("file_name", reg["file_id"]))
        mark_deleted(reg["file_id"])

    total = len(to_process)
    for idx, f in enumerate(to_process, start=1):
        file_name = f.get("name", f["id"])
        if on_file_start:
            try:
                on_file_start(file_name, idx, total)
            except Exception as e:  # noqa: BLE001
                log.warning("on_file_start callback raised: %s", e)

        ok = True
        try:
            pdf_bytes = download_pdf(service, f["id"])
            process_pdf_bytes(
                file_id=f["id"],
                file_name=file_name,
                pdf_bytes=pdf_bytes,
                md5=f.get("md5Checksum"),
            )
        except Exception as e:
            log.error("✗ %s — %s", file_name, e, exc_info=True)
            ok = False

        if on_file_done:
            try:
                on_file_done(file_name, idx, total, ok)
            except Exception as e:  # noqa: BLE001
                log.warning("on_file_done callback raised: %s", e)

    log.info("=== Drive sync done ===\n")


def ingest_drive(loop: bool = False, **callbacks: Any) -> None:
    """One-shot Drive sync, or continuous polling when `loop=True`.

    `callbacks` are forwarded to `run_once` (on_scan_done / on_file_start /
    on_file_done). Used by the CLI (no callbacks) and by the in-process
    progress controller (all three set).
    """
    service = get_drive_service()

    if not loop:
        run_once(service, **callbacks)
        return

    log.info("Loop mode: polling every %d seconds.", POLL_INTERVAL_SECONDS)
    while True:
        try:
            run_once(service, **callbacks)
        except Exception as e:
            log.error("Sync run failed: %s", e, exc_info=True)
        time.sleep(POLL_INTERVAL_SECONDS)
