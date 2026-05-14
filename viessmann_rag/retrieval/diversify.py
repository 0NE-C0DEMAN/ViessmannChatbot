"""Per-file diversification — prevents one PDF from monopolizing results."""
from __future__ import annotations


def diversify(chunks: list[dict], max_per_file: int = 4) -> list[dict]:
    """Cap how many chunks come from any one file. Skipped entirely when the
    candidate pool is already coming from ≤ 2 distinct files (otherwise we'd
    just throw away relevant pages).

    Also normalizes the RPC's `chunk_id` field back to `id` so downstream
    callers don't need to know about the OUT-param rename.
    """
    for c in chunks:
        if "id" not in c and "chunk_id" in c:
            c["id"] = c["chunk_id"]

    distinct_files = len({c.get("file_id") for c in chunks})
    if distinct_files <= 2:
        return chunks

    counts: dict[str, int] = {}
    out: list[dict] = []
    for c in chunks:
        fid = c.get("file_id") or ""
        if counts.get(fid, 0) >= max_per_file:
            continue
        counts[fid] = counts.get(fid, 0) + 1
        out.append(c)
    return out
