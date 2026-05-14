"""
Viessmann ingest pipeline (v2).

Two modes:
  --dir <path>     Ingest PDFs from a local directory (one-shot)
  --drive          Ingest PDFs from Google Drive (one-shot)
  --drive --loop   Continuously poll Drive and ingest new / re-ingest modified

Pipeline (per PDF):
  1. pdfplumber.extract_text(layout=True)   — layout-preserving text per page
  2. pdfplumber.extract_tables(lines_strict) — strict-line tables as markdown
  3. One chunk = one page (with page_number metadata)
  4. text-embedding-3-small embeddings
  5. Upsert into document_chunks_v2 + document_registry_v2

Examples
--------
    py -3.11 ingest.py --dir "C:\\docs\\Vitocal"
    py -3.11 ingest.py --dir "C:\\docs\\Vitocal" --file 5832352_info.pdf
    py -3.11 ingest.py --drive
    py -3.11 ingest.py --drive --loop
"""
import argparse
import hashlib
import io
import logging
import logging.handlers
import os
import re
import sys
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

import requests
from dotenv import load_dotenv
from openai import OpenAI

from pdf_parser import extract_pdf

# ── Config ─────────────────────────────────────────────────────────────────
load_dotenv(Path(__file__).parent / ".env")

SUPABASE_URL    = os.environ["SUPABASE_URL"]
SUPABASE_KEY    = os.environ["SUPABASE_SERVICE_KEY"]
OPENAI_API_KEY  = os.environ["OPENAI_API_KEY"]
EMBEDDING_MODEL = "text-embedding-3-small"

# Drive config (only required when running --drive)
GOOGLE_CLIENT_ID       = os.environ.get("GOOGLE_CLIENT_ID")
GOOGLE_CLIENT_SECRET   = os.environ.get("GOOGLE_CLIENT_SECRET")
GOOGLE_ROOT_FOLDER_ID  = os.environ.get("GOOGLE_ROOT_FOLDER_ID")
POLL_INTERVAL_SECONDS  = int(os.environ.get("POLL_INTERVAL_SECONDS", 60))

GOOGLE_TOKEN_FILE = Path(__file__).parent / "google_token.json"
GOOGLE_SCOPES     = ["https://www.googleapis.com/auth/drive.readonly"]

HEADERS = {
    "apikey":        SUPABASE_KEY,
    "Authorization": f"Bearer {SUPABASE_KEY}",
    "Content-Type":  "application/json",
}

# ── Logging ────────────────────────────────────────────────────────────────
LOG_DIR = Path(__file__).parent / "logs"
LOG_DIR.mkdir(exist_ok=True)
_fmt = logging.Formatter("%(asctime)s %(levelname)-8s %(message)s", datefmt="%H:%M:%S")
_fh = logging.handlers.RotatingFileHandler(
    LOG_DIR / "ingest.log", maxBytes=5*1024*1024, backupCount=3, encoding="utf-8",
)
_fh.setFormatter(_fmt)
_ch = logging.StreamHandler(); _ch.setFormatter(_fmt)
logging.basicConfig(level=logging.INFO, handlers=[_fh, _ch])
log = logging.getLogger("ingest")

oai = OpenAI(api_key=OPENAI_API_KEY)


# ── Metadata helpers ───────────────────────────────────────────────────────
def parse_metadata(file_name: str) -> tuple[str, str]:
    """Same naming convention as Frane's original:
       '5832352_Vitocal_100-S_informacijski_list.pdf'
         → ('Vitocal 100-S informacijski list', 'informacijski_list')
    """
    name = re.sub(r"\.pdf$", "", file_name, flags=re.IGNORECASE)
    parts = name.split("_")
    if parts and re.match(r"^\d+$", parts[0]):
        parts = parts[1:]
    product_line = " ".join(parts)

    lower = file_name.lower()
    if "projektiranje" in lower:
        doc_type = "upute_za_projektiranje"
    elif "montaz" in lower:
        doc_type = "upute_za_montazu"
    elif "servis" in lower:
        doc_type = "upute_za_servis"
    elif "informacijski" in lower or "info_list" in lower:
        doc_type = "informacijski_list"
    elif "upotrebu" in lower:
        doc_type = "upute_za_upotrebu"
    else:
        doc_type = "ostalo"
    return product_line, doc_type


def md5_bytes(data: bytes) -> str:
    h = hashlib.md5()
    h.update(data)
    return h.hexdigest()


def md5_file(path: Path) -> str:
    h = hashlib.md5()
    with open(path, "rb") as f:
        for blk in iter(lambda: f.read(8192), b""):
            h.update(blk)
    return h.hexdigest()


# ── Supabase REST helpers ──────────────────────────────────────────────────
def delete_chunks(file_id: str) -> None:
    r = requests.delete(
        f"{SUPABASE_URL}/rest/v1/document_chunks_v2",
        params={"file_id": f"eq.{file_id}"},
        headers=HEADERS, timeout=30,
    )
    if r.status_code >= 300:
        log.error("delete_chunks %s → %d %s", file_id, r.status_code, r.text[:300])
        r.raise_for_status()


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
    """Soft-delete: keep registry row but mark status. Caller may also want to
    delete chunks; we leave that to the operator for safety."""
    r = requests.patch(
        f"{SUPABASE_URL}/rest/v1/document_registry_v2",
        params={"file_id": f"eq.{file_id}"},
        headers={**HEADERS, "Prefer": "return=minimal"},
        json={"status": "deleted"},
        timeout=30,
    )
    if r.status_code >= 300:
        log.error("mark_deleted → %d %s", r.status_code, r.text[:300])


def insert_chunks(rows: list[dict]) -> None:
    r = requests.post(
        f"{SUPABASE_URL}/rest/v1/document_chunks_v2",
        headers={**HEADERS, "Prefer": "return=minimal"},
        json=rows, timeout=60,
    )
    if r.status_code >= 300:
        log.error("insert_chunks → %d %s", r.status_code, r.text[:400])
        r.raise_for_status()


def get_registry() -> list[dict]:
    """Return ALL registry rows (active and deleted)."""
    r = requests.get(
        f"{SUPABASE_URL}/rest/v1/document_registry_v2",
        params={"select": "file_id,file_name,md5_checksum,status"},
        headers=HEADERS, timeout=30,
    )
    r.raise_for_status()
    return r.json() or []


# ── Embedding ──────────────────────────────────────────────────────────────
def embed(text: str, retries: int = 3) -> list[float]:
    for attempt in range(retries):
        try:
            r = oai.embeddings.create(model=EMBEDDING_MODEL, input=text)
            return r.data[0].embedding
        except Exception as e:
            if attempt == retries - 1:
                raise
            log.warning("Embed attempt %d failed: %s — retrying", attempt + 1, e)
            time.sleep(2 ** attempt)
    raise RuntimeError("unreachable")


# ── Core: process bytes ────────────────────────────────────────────────────
def process_pdf_bytes(file_id: str, file_name: str, pdf_bytes: bytes,
                      md5: Optional[str] = None) -> None:
    """The hot path — same regardless of source (local file or Drive download).
    Writes the PDF to a temp file because the extractor accepts a Path."""
    product_line, doc_type = parse_metadata(file_name)
    md5 = md5 or md5_bytes(pdf_bytes)

    log.info("=== %s ===", file_name)
    log.info("  file_id=%s  product_line=%s  doc_type=%s",
             file_id[:24] + "…" if len(file_id) > 24 else file_id,
             product_line, doc_type)

    with tempfile.NamedTemporaryFile(suffix=".pdf", delete=False) as tmp:
        tmp.write(pdf_bytes)
        tmp_path = Path(tmp.name)

    try:
        pages = extract_pdf(tmp_path)
    finally:
        try:
            tmp_path.unlink()
        except Exception:
            pass

    log.info("  Extracted %d pages", len(pages))

    # Clear old chunks before re-ingest (safe to re-run)
    delete_chunks(file_id)

    upsert_registry({
        "file_id":           file_id,
        "file_name":         file_name,
        "product_line":      product_line,
        "document_type":     doc_type,
        "md5_checksum":      md5,
        "page_count":        len(pages),
        "status":            "active",
        "last_processed_at": datetime.now(timezone.utc).isoformat(),
    })

    batch: list[dict] = []
    stored = 0
    for p in pages:
        if not p.text.strip() or len(p.text.strip()) < 30:
            log.info("  p.%d skipped (sparse, %d chars)", p.page_number, p.char_count)
            continue

        chunk_text = f"[Document: {file_name} · Page {p.page_number}]\n{p.text}"

        try:
            emb = embed(chunk_text)
        except Exception as e:
            log.error("  p.%d embed failed: %s", p.page_number, e)
            continue

        batch.append({
            "file_id":         file_id,
            "file_name":       file_name,
            "product_line":    product_line,
            "document_type":   doc_type,
            "page_number":     p.page_number,
            "section_heading": p.section_heading,
            "chunk_text":      chunk_text,
            "has_table":       p.has_table,
            "token_estimate":  p.char_count // 4,
            "embedding":       emb,
        })
        stored += 1

        if len(batch) >= 10:
            insert_chunks(batch)
            log.info("  flushed batch (%d / %d pages)", stored, len(pages))
            batch = []

    if batch:
        insert_chunks(batch)

    log.info("  ✓ stored %d / %d pages", stored, len(pages))


# ── LOCAL mode ─────────────────────────────────────────────────────────────
def ingest_local(directory: Path, single_file: Optional[str] = None) -> None:
    pdfs = sorted(directory.rglob("*.pdf"))
    if single_file:
        pdfs = [p for p in pdfs if p.name == single_file]
    log.info("Ingesting %d PDFs from %s", len(pdfs), directory)

    t0 = time.time()
    ok = 0
    for p in pdfs:
        try:
            process_pdf_bytes(
                file_id=p.stem,            # local: use filename stem as id
                file_name=p.name,
                pdf_bytes=p.read_bytes(),
                md5=md5_file(p),
            )
            ok += 1
        except Exception as e:
            log.error("✗ %s — %s", p.name, e, exc_info=True)
    log.info("Local ingest done: %d / %d PDFs in %.1fs", ok, len(pdfs), time.time() - t0)


# ── DRIVE mode (mirrors Frane's original processor) ────────────────────────
def get_drive_service():
    """OAuth on first run (opens browser); reuses google_token.json on subsequent runs."""
    from google.auth.transport.requests import Request
    from google.oauth2.credentials import Credentials
    from google_auth_oauthlib.flow import InstalledAppFlow
    from googleapiclient.discovery import build

    if not (GOOGLE_CLIENT_ID and GOOGLE_CLIENT_SECRET and GOOGLE_ROOT_FOLDER_ID):
        raise SystemExit(
            "Drive mode requires GOOGLE_CLIENT_ID, GOOGLE_CLIENT_SECRET, "
            "and GOOGLE_ROOT_FOLDER_ID in .env"
        )

    creds = None
    if GOOGLE_TOKEN_FILE.exists():
        creds = Credentials.from_authorized_user_file(GOOGLE_TOKEN_FILE, GOOGLE_SCOPES)

    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            creds.refresh(Request())
        else:
            client_config = {
                "installed": {
                    "client_id":     GOOGLE_CLIENT_ID,
                    "client_secret": GOOGLE_CLIENT_SECRET,
                    "redirect_uris": ["urn:ietf:wg:oauth:2.0:oob", "http://localhost"],
                    "auth_uri":      "https://accounts.google.com/o/oauth2/auth",
                    "token_uri":     "https://oauth2.googleapis.com/token",
                }
            }
            flow = InstalledAppFlow.from_client_config(client_config, GOOGLE_SCOPES)
            creds = flow.run_local_server(port=0)

        GOOGLE_TOKEN_FILE.write_text(creds.to_json())
        log.info("Google token saved → %s", GOOGLE_TOKEN_FILE)

    return build("drive", "v3", credentials=creds)


def list_subfolders(service, parent_id: str) -> list[dict]:
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


def list_pdfs(service) -> list[dict]:
    """List ALL PDFs the OAuth account can see; caller filters by parent."""
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


def download_pdf(service, file_id: str) -> bytes:
    from googleapiclient.http import MediaIoBaseDownload
    req = service.files().get_media(fileId=file_id)
    buf = io.BytesIO()
    downloader = MediaIoBaseDownload(buf, req)
    done = False
    while not done:
        _, done = downloader.next_chunk()
    return buf.getvalue()


def run_once_drive(service) -> None:
    log.info("=== Drive sync run ===")

    subfolders = list_subfolders(service, GOOGLE_ROOT_FOLDER_ID)
    valid_parents = {GOOGLE_ROOT_FOLDER_ID} | {f["id"] for f in subfolders}
    log.info("Root + %d subfolders", len(subfolders))

    all_pdfs = list_pdfs(service)
    drive_files = [
        f for f in all_pdfs
        if any(p in valid_parents for p in (f.get("parents") or []))
    ]
    log.info("PDFs in folder tree: %d", len(drive_files))

    registry = get_registry()
    registry_map = {r["file_id"]: r for r in registry if r.get("file_id")}
    drive_map    = {f["id"]: f for f in drive_files}

    # New = in Drive but not in registry, OR md5 changed
    to_process: list[dict] = []
    for f in drive_files:
        reg = registry_map.get(f["id"])
        md5 = f.get("md5Checksum") or ""
        if not reg:
            to_process.append(f)
        elif reg.get("md5_checksum") != md5:
            log.info("  modified: %s (md5 changed)", f.get("name"))
            to_process.append(f)

    # Deleted = in registry, not in Drive, status != deleted
    deleted = [
        r for r in registry
        if r.get("file_id") and r["file_id"] not in drive_map
           and r.get("status") != "deleted"
    ]

    log.info("To process: %d  |  To mark deleted: %d", len(to_process), len(deleted))

    for reg in deleted:
        log.info("  marking deleted: %s", reg.get("file_name", reg["file_id"]))
        mark_deleted(reg["file_id"])

    for f in to_process:
        try:
            pdf_bytes = download_pdf(service, f["id"])
            process_pdf_bytes(
                file_id=f["id"],
                file_name=f.get("name", f["id"]),
                pdf_bytes=pdf_bytes,
                md5=f.get("md5Checksum"),
            )
        except Exception as e:
            log.error("✗ %s — %s", f.get("name"), e, exc_info=True)

    log.info("=== Drive sync done ===\n")


def ingest_drive(loop: bool = False) -> None:
    service = get_drive_service()

    if not loop:
        run_once_drive(service)
        return

    log.info("Loop mode: polling every %d seconds.", POLL_INTERVAL_SECONDS)
    while True:
        try:
            run_once_drive(service)
        except Exception as e:
            log.error("Sync run failed: %s", e, exc_info=True)
        time.sleep(POLL_INTERVAL_SECONDS)


# ── CLI ────────────────────────────────────────────────────────────────────
def main():
    ap = argparse.ArgumentParser(description="Viessmann RAG ingest pipeline (v2)")
    src = ap.add_mutually_exclusive_group(required=True)
    src.add_argument("--dir",  help="Local directory of PDFs (recursed)")
    src.add_argument("--drive", action="store_true", help="Pull PDFs from Google Drive")

    ap.add_argument("--file", help="(--dir mode) only process this filename")
    ap.add_argument("--loop", action="store_true",
                    help="(--drive mode) keep polling every POLL_INTERVAL_SECONDS")
    args = ap.parse_args()

    if args.dir:
        directory = Path(args.dir)
        if not directory.exists():
            log.error("Directory not found: %s", directory)
            sys.exit(1)
        ingest_local(directory, single_file=args.file)
    else:
        ingest_drive(loop=args.loop)


if __name__ == "__main__":
    main()
