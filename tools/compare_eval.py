"""Render a side-by-side Markdown table from two eval-*.json runs.

Usage:
    py tools/compare_eval.py logs/eval-openai.json logs/eval-gemini.json \
        --out logs/compare_openai_vs_gemini.md
"""
from __future__ import annotations

import argparse
import json
import pathlib
import sys


def load(path: pathlib.Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def short(text: str, n: int = 220) -> str:
    text = (text or "").replace("\n", " ").replace("|", "\\|").strip()
    return text if len(text) <= n else text[: n - 1] + "…"


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("a", help="path to first eval JSON (e.g. eval-openai.json)")
    ap.add_argument("b", help="path to second eval JSON (e.g. eval-gemini.json)")
    ap.add_argument("--out", default="logs/compare.md")
    ap.add_argument("--label-a", default=None,
                    help="Column header for the first run (default: filename)")
    ap.add_argument("--label-b", default=None)
    args = ap.parse_args()

    pa, pb = pathlib.Path(args.a), pathlib.Path(args.b)
    A = load(pa)
    B = load(pb)
    la = args.label_a or pa.stem.replace("eval-", "")
    lb = args.label_b or pb.stem.replace("eval-", "")

    qids = sorted(set(A) | set(B))

    md = [
        f"# Eval comparison — {la} vs {lb}",
        "",
        f"Questions: **{len(qids)}**",
        "",
        f"| # | Question | Truth | {la} | {lb} |",
        f"|---|---|---|---|---|",
    ]
    a_ok = b_ok = 0
    a_refused = b_refused = 0
    a_quota = b_quota = 0
    a_time_total = b_time_total = 0.0

    for qid in qids:
        ra = A.get(qid, {})
        rb = B.get(qid, {})

        q     = ra.get("q") or rb.get("q") or ""
        truth = ra.get("truth") or rb.get("truth") or ""

        ans_a = ra.get("answer") or ra.get("error") or ""
        ans_b = rb.get("answer") or rb.get("error") or ""

        ta = ra.get("_elapsed_s")
        tb = rb.get("_elapsed_s")
        a_time_total += ta or 0.0
        b_time_total += tb or 0.0

        # Heuristic counters
        REFUSAL = "Podatak nije pronađen"
        if REFUSAL in ans_a: a_refused += 1
        else: a_ok += 1
        if REFUSAL in ans_b: b_refused += 1
        else: b_ok += 1
        if "kvota" in ans_a.lower(): a_quota += 1
        if "kvota" in ans_b.lower(): b_quota += 1

        cell_a = f"{short(ans_a)}<br/>_({ta:.1f}s)_" if ta else short(ans_a)
        cell_b = f"{short(ans_b)}<br/>_({tb:.1f}s)_" if tb else short(ans_b)

        md.append(f"| {qid} | {short(q, 80)} | {short(truth, 80)} | {cell_a} | {cell_b} |")

    md += [
        "",
        "## Summary",
        "",
        f"| Metric | {la} | {lb} |",
        f"|---|---|---|",
        f"| Answered (non-refusal) | {a_ok} | {b_ok} |",
        f"| Refused | {a_refused} | {b_refused} |",
        f"| Quota errors | {a_quota} | {b_quota} |",
        f"| Total time | {a_time_total:.1f}s | {b_time_total:.1f}s |",
        f"| Mean per Q | {a_time_total/max(1,len(qids)):.1f}s | {b_time_total/max(1,len(qids)):.1f}s |",
    ]

    out = pathlib.Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text("\n".join(md), encoding="utf-8")
    print(f"Wrote {out}")
    print("\n".join(md[-9:]))


if __name__ == "__main__":
    sys.exit(main())
