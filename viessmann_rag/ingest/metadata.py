"""Filename → metadata, and file hashing helpers."""
from __future__ import annotations

import hashlib
import re
from pathlib import Path


def parse_metadata(file_name: str) -> tuple[str, str]:
    """`5832352_Vitocal_100-S_informacijski_list.pdf` →
       ('Vitocal 100-S informacijski list', 'informacijski_list')

    Same naming convention as the original processor — keeps backward
    compatibility with anything indexing on `product_line` / `document_type`.
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
