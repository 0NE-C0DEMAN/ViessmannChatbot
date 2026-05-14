"""
Eval harness for the v2 chatbot.

Runs a battery of questions, captures answers + sources + timing,
and writes a single JSON report.

Usage:
    python eval.py --tag v2-baseline
    python eval.py --tag v2-baseline --concurrency 4
"""
import argparse
import concurrent.futures as cf
import json
import time
from pathlib import Path

import requests

BASE = "http://localhost:8081"
USER, PWD = "viessmann", "carrier"


CASES: list[dict] = [
    # ── single value lookups ──
    {"id": "Q01", "q": "What is the COP of model 101.A12 at A7/W35?",
     "truth": "4,70", "category": "single_value"},
    {"id": "Q02", "q": "What is the maximum supply water temperature for Vitocal 100-S?",
     "truth": "55°C (A-series 101.A12-A16), 58°C (B-series 101.B04-B08)", "category": "multi_variant"},
    {"id": "Q03", "q": "What is the sound power level (dB(A)) of the Vitocal 100-S outdoor units?",
     "truth": "62 dB(A) for 101.B04 and 101.B06, 64 dB(A) for 101.B08, 101.A12, 101.A14, 101.A16",
     "category": "table_continuation"},

    # ── multi-variant comparisons ──
    {"id": "Q04", "q": "Compare the SCOP at W35 for B-series and A-series Vitocal 100-S models.",
     "truth": "B-series: 4.45 / 4.45 / 4.46; A-series: 4.08 / 4.08 / 3.95", "category": "comparison"},
    {"id": "Q05", "q": "Which refrigerant safety group does each variant use?",
     "truth": "R32 → A2L (B-series). R410A → A1 (A-series)", "category": "multi_variant"},
    {"id": "Q06", "q": "What is GWP and what are the values for R32 and R410A?",
     "truth": "GWP = Global Warming Potential. R32: 675. R410A: 1924.", "category": "domain_knowledge"},

    # ── capability / categorical ──
    {"id": "Q07", "q": "Which Vitocal 100-S variants support active cooling?",
     "truth": "AWB-E-AC, AWB-M-E-AC variants (both .A and .B)", "category": "capability"},
    {"id": "Q08", "q": "What is the difference between type AWB-M and type AWB-M-E?",
     "truth": "AWB-M-E adds integrated electric flow heater (Protočni grijač ogrjevne vode)", "category": "capability"},
    {"id": "Q09", "q": "List all the type variants of Vitocal 100-S.",
     "truth": "AWB(-M) 101.A/B, AWB(-M)-E 101.A/B, AWB(-M)-E-AC 101.A/B (9 variants total in the type-overview table)", "category": "enumeration"},

    # ── Croatian queries ──
    {"id": "Q10", "q": "Koja je dimenzija unutarnje jedinice?",
     "truth": "370 × 450 × 880 mm (svi tipovi)", "category": "croatian"},
    {"id": "Q11", "q": "Kolika je minimalna temperatura ulaza zraka za grijanje?",
     "truth": "-20°C za B-seriju, -22°C za A-seriju", "category": "croatian_multi"},

    # ── refusal / hallucination tests ──
    {"id": "Q12", "q": "Tell me about model Vitodens 200.",
     "truth": "REFUSE — not in informacijski_list. Model Vitodens 200 is a different product line.",
     "category": "refusal"},
    {"id": "Q13", "q": "What is the warranty period for Vitocal 100-S?",
     "truth": "REFUSE — not in this document.", "category": "refusal"},
    {"id": "Q14", "q": "What is the price of Vitocal 100-S type AWB-M 101.B08?",
     "truth": "REFUSE — info says 'vidi cjenik' (see price list); price not in document.", "category": "refusal"},

    # ── reasoning ──
    {"id": "Q15", "q": "Among the 101.B variants at A2/W35, which has the highest COP?",
     "truth": "101.B04 with COP 3.84 (vs 3.51 for B06, 3.60 for B08)", "category": "reasoning"},
]


def login(s: requests.Session) -> None:
    r = s.post(f"{BASE}/api/login", json={"username": USER, "password": PWD}, timeout=10)
    r.raise_for_status()


def ask(s: requests.Session, q: str) -> dict:
    t0 = time.time()
    r = s.post(f"{BASE}/api/chat", json={"question": q, "history": []}, timeout=120)
    dt = time.time() - t0
    try:
        data = r.json()
    except Exception:
        data = {"error": f"non-JSON: {r.text[:200]}"}
    data["_elapsed_s"] = round(dt, 2)
    data["_status"] = r.status_code
    return data


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--tag", default=f"run-{int(time.time())}")
    ap.add_argument("--concurrency", type=int, default=4)
    args = ap.parse_args()

    s = requests.Session()
    login(s)

    out_path = Path(__file__).parent / "logs" / f"eval-{args.tag}.json"
    out_path.parent.mkdir(exist_ok=True)

    results = {}

    def run_one(case):
        # Each thread needs its own session (cookies are cookie-jar-per-session)
        ss = requests.Session()
        login(ss)
        ans = ask(ss, case["q"])
        return case["id"], {**case, **ans}

    with cf.ThreadPoolExecutor(max_workers=args.concurrency) as pool:
        for fut in cf.as_completed([pool.submit(run_one, c) for c in CASES]):
            qid, payload = fut.result()
            results[qid] = payload
            ans = payload.get("answer", payload.get("error", ""))[:120]
            line = f"[{qid}] {payload['_elapsed_s']:>5.1f}s  {ans}"
            print(line.encode("ascii", "replace").decode("ascii"))

    out_path.write_text(json.dumps(results, indent=2, ensure_ascii=False), encoding="utf-8")
    print(f"\nWrote {len(results)} results -> {out_path}")


if __name__ == "__main__":
    main()
