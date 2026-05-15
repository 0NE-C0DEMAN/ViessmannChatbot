"""End-to-end Drive ingest test for a single PDF.

Path:
  1. Build Drive service from the service account JSON
  2. List PDFs in the Viessmann folder tree
  3. Pick the smallest one (informacijski_list, 12 pages)
  4. Download via Drive API
  5. Run the existing process_pdf_bytes pipeline (Drive file_id as key)
  6. Verify the new row in Supabase
  7. Ask one chat question that hits this file specifically
  8. Clean up — delete chunks + registry row so we're back to 15 PDFs
"""
from __future__ import annotations
import json, time, sys
import requests

from viessmann_rag.config import (
    GOOGLE_ROOT_FOLDER_ID, SUPABASE_URL, SUPABASE_SERVICE_KEY,
)
from viessmann_rag.ingest.drive import (
    get_drive_service, list_subfolders, list_pdfs, download_pdf,
)
from viessmann_rag.ingest.pipeline import process_pdf_bytes

H = {"apikey": SUPABASE_SERVICE_KEY,
     "Authorization": f"Bearer {SUPABASE_SERVICE_KEY}",
     "Content-Type": "application/json"}

CHAT = "http://localhost:8081"


def step(n, label):
    print(f"\n{'='*80}\nSTEP {n} — {label}\n{'='*80}")


def main():
    # ── 1. Build Drive service ─────────────────────────────────────────
    step(1, "Auth via service account")
    svc = get_drive_service()
    print(f"  Drive service built OK")

    # ── 2. List PDFs in the Viessmann folder tree ─────────────────────
    step(2, "List PDFs visible to the service account")
    subs = list_subfolders(svc, GOOGLE_ROOT_FOLDER_ID)
    valid_parents = {GOOGLE_ROOT_FOLDER_ID} | {s["id"] for s in subs}
    all_pdfs = list_pdfs(svc)
    in_tree = [p for p in all_pdfs
               if any(pp in valid_parents for pp in (p.get("parents") or []))]
    print(f"  Subfolders: {len(subs)}  PDFs in tree: {len(in_tree)}")

    # Pick the informacijski_list (smallest, the spec sheet)
    target = next((p for p in in_tree
                   if "informacijski_list" in p.get("name", "")), None)
    if target is None:
        sys.exit("No informacijski_list PDF found in Drive — aborting test")
    print(f"  Target: {target['name']}")
    print(f"  Drive file_id: {target['id']}")
    print(f"  md5:           {target.get('md5Checksum','-')[:16]}")

    # ── 3. Download bytes ─────────────────────────────────────────────
    step(3, "Download PDF bytes via Drive API")
    t0 = time.time()
    pdf_bytes = download_pdf(svc, target["id"])
    print(f"  Downloaded {len(pdf_bytes):,} bytes in {time.time()-t0:.2f}s")

    # ── 4. Run the full ingest pipeline (Drive file_id as key) ────────
    step(4, "Run ingest pipeline on the downloaded bytes")
    t0 = time.time()
    process_pdf_bytes(
        file_id=target["id"],
        file_name=target["name"],
        pdf_bytes=pdf_bytes,
        md5=target.get("md5Checksum"),
    )
    print(f"  Ingest done in {time.time()-t0:.1f}s")

    # ── 5. Verify in Supabase ─────────────────────────────────────────
    step(5, "Verify in document_registry_v2 + document_chunks_v2")
    r = requests.get(f"{SUPABASE_URL}/rest/v1/document_registry_v2",
                     params={"file_id": f"eq.{target['id']}"},
                     headers=H, timeout=30).json()
    if not r:
        sys.exit("Registry row not found after ingest")
    reg = r[0]
    print(f"  Registry row: file_name={reg['file_name']!r}")
    print(f"                product_line={reg['product_line']!r}")
    print(f"                document_type={reg['document_type']!r}")
    print(f"                page_count={reg['page_count']}")
    print(f"                status={reg['status']}")

    # count chunks
    rc = requests.head(f"{SUPABASE_URL}/rest/v1/document_chunks_v2",
                       params={"file_id": f"eq.{target['id']}", "select": "file_id"},
                       headers={**H, "Prefer": "count=exact", "Range": "0-0"},
                       timeout=30)
    cr = rc.headers.get("Content-Range", "0-0/0")
    chunk_count = cr.split("/")[-1]
    print(f"  Chunks stored: {chunk_count}")

    # ── 6. Ask a question that should land on this file ──────────────
    step(6, "Ask a question that should hit this Drive-ingested file")
    sess = requests.Session()
    sess.post(f"{CHAT}/api/login",
              json={"username": "viessmann", "password": "carrier"}).raise_for_status()
    q = "What is the exact refrigerant charge in kg for model 101.B08?"
    print(f"  Q: {q}")
    t0 = time.time()
    rr = sess.post(f"{CHAT}/api/chat",
                   json={"question": q, "history": [], "nocache": True},
                   timeout=180)
    dt = time.time() - t0
    body = rr.json()
    ans = (body.get("answer") or body.get("error") or "")
    print(f"  Status: {rr.status_code}  ({dt:.1f}s)")
    print(f"  A: {ans[:300]}")
    src_files = [s.get("file_name") for s in body.get("sources", [])]
    drive_hits = [f for f, s in zip(src_files, body.get("sources", []))
                  if s.get("file_name") == target["name"]]
    print(f"  Sources count: {len(src_files)}, "
          f"of which from our test file: {len(drive_hits)}")

    # ── 7. Clean up — delete chunks + registry row ───────────────────
    step(7, "Clean up — delete the Drive-keyed row + its chunks")
    rd = requests.delete(f"{SUPABASE_URL}/rest/v1/document_chunks_v2",
                         params={"file_id": f"eq.{target['id']}"},
                         headers=H, timeout=30)
    print(f"  delete chunks → HTTP {rd.status_code}")
    rr = requests.delete(f"{SUPABASE_URL}/rest/v1/document_registry_v2",
                         params={"file_id": f"eq.{target['id']}"},
                         headers=H, timeout=30)
    print(f"  delete registry row → HTTP {rr.status_code}")

    # ── 8. Final verification: back to 15 rows ────────────────────────
    step(8, "Final state — registry should be back to 15 rows")
    r2 = requests.get(f"{SUPABASE_URL}/rest/v1/document_registry_v2",
                      params={"select": "file_id"},
                      headers={**H, "Prefer": "count=exact", "Range": "0-0"},
                      timeout=30)
    cr2 = r2.headers.get("Content-Range", "0-0/?")
    print(f"  Total registry rows: {cr2.split('/')[-1]}")

    rc2 = requests.head(f"{SUPABASE_URL}/rest/v1/document_chunks_v2",
                        params={"select": "file_id"},
                        headers={**H, "Prefer": "count=exact", "Range": "0-0"},
                        timeout=30)
    cr3 = rc2.headers.get("Content-Range", "0-0/?")
    print(f"  Total chunks:         {cr3.split('/')[-1]}")

    print(f"\n{'='*80}\nDRIVE E2E TEST COMPLETE\n{'='*80}")


if __name__ == "__main__":
    if sys.stdout.encoding.lower() != "utf-8":
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    main()
