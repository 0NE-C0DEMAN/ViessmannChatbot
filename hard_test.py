"""Manual stress test — hammer the running v1.2.0 server with hard questions.

Captures answer, latency, status, source diversity. Also tests streaming
first-token latency and cache hits.
"""
from __future__ import annotations
import json, time, sys
import requests

BASE = "http://localhost:8081"
USER, PWD = "viessmann", "carrier"

HARD = [
    # ── 1-3: Vague / underspecified ──
    ("vague-cop",      "What's the COP?"),
    ("vague-general",  "How does it work?"),
    ("vague-product",  "Što je Vitocal?"),

    # ── 4-6: Precise spec lookups (exact values, multiple variants) ──
    ("spec-refrigerant-charge",
        "What is the exact refrigerant charge in kg for model 101.B08?"),
    ("spec-compressor-voltage-cro",
        "Koji je nazivni napon kompresora za tip 101.B08 pri 230 V mreži?"),
    ("spec-power-compare",
        "Compare the electrical input (kW) at A2/W35 between models 101.B04 and 101.A16."),

    # ── 7-8: Multi-step / reasoning ──
    ("reason-lowest-gwp",
        "Which Vitocal 100-S model uses the refrigerant with the lowest GWP, and what is its maximum heating capacity?"),
    ("reason-fuses",
        "If I install model AWB-M-E-AC 101.B08, what fuse rating do I need for the compressor and for the network supply?"),

    # ── 9-10: Croatian-only / mixed ──
    ("cro-weight",
        "Koliko teži vanjska jedinica za tip 101.A16?"),
    ("cro-noise",
        "Kolika je razina zvučne snage u dB(A) za 101.A14?"),

    # ── 11-13: Hallucination / refusal traps ──
    ("trap-warranty",
        "What is the warranty period and average lifespan of Vitocal 100-S?"),
    ("trap-price-eur",
        "What is the price of model 101.A14 in EUR?"),
    ("trap-vitocal-compare",
        "What is the difference between Vitocal 100-S and Vitocal 200-S?"),

    # ── 14: Capability / out-of-range ──
    ("capability-coldlimit",
        "Can the Vitocal 100-S operate when the outside temperature is -25°C?"),
]


def login(s):
    s.post(f"{BASE}/api/login", json={"username": USER, "password": PWD}).raise_for_status()


def ask(s, q):
    t0 = time.time()
    r = s.post(f"{BASE}/api/chat", json={"question": q, "history": []}, timeout=180)
    dt = time.time() - t0
    try:
        body = r.json()
    except Exception:
        body = {"error": r.text[:200]}
    return r.status_code, round(dt, 2), body


def main():
    s = requests.Session()
    login(s)

    print(f"{'='*100}")
    print("PART 1 - 14 hard questions via /api/chat")
    print(f"{'='*100}\n")

    results = []
    for tag, q in HARD:
        status, dt, body = ask(s, q)
        ans = (body.get("answer") or body.get("error") or "")
        src = body.get("sources", [])
        files = sorted({(s_.get("file_name") or "")[:30] for s_ in src})
        results.append({"tag": tag, "q": q, "status": status, "elapsed_s": dt,
                        "answer": ans, "sources": len(src), "files": files})
        # Safe print
        snip = ans.replace("\n", " / ")[:180]
        print(f"[{tag:30s}] {dt:>5.1f}s status={status} src={len(src):>2}")
        print(f"  Q: {q[:150]}")
        print(f"  A: {snip}")
        print()

    # Save full report
    with open("logs/hard-test-report.json", "w", encoding="utf-8") as f:
        json.dump(results, f, indent=2, ensure_ascii=False)

    # ── PART 2: streaming first-token latency ──
    print(f"\n{'='*100}")
    print("PART 2 - Streaming first-token latency")
    print(f"{'='*100}\n")

    q = "What is the nominal thermal output of model 101.B06 at A2/W35?"
    t0 = time.time()
    first_token_t = None
    last_t = None
    n_tokens = 0
    answer = []
    with requests.post(f"{BASE}/api/chat/stream",
                       json={"question": q, "history": [], "nocache": True},
                       cookies=s.cookies, stream=True, timeout=180) as r:
        for line in r.iter_lines(decode_unicode=True):
            if not line or not line.startswith("data:"):
                continue
            try:
                ev = json.loads(line[5:].strip())
            except Exception:
                continue
            now = time.time() - t0
            if ev.get("type") == "token":
                if first_token_t is None:
                    first_token_t = now
                answer.append(ev.get("content", ""))
                n_tokens += 1
                last_t = now
            elif ev.get("type") == "done":
                break
            elif ev.get("type") == "error":
                print(f"  Stream error: {ev}")
                break

    print(f"  Q: {q}")
    print(f"  First-token latency: {first_token_t:.2f}s" if first_token_t else "  No tokens received")
    print(f"  Total stream duration: {last_t:.2f}s" if last_t else "")
    print(f"  Tokens received: {n_tokens}")
    print(f"  Answer ({len(''.join(answer))} chars): " + "".join(answer)[:200].replace("\n", " / "))

    # ── PART 3: cache hit on repeat ──
    print(f"\n{'='*100}")
    print("PART 3 - Cache hit on repeat (same question, no nocache)")
    print(f"{'='*100}\n")
    q_cache = HARD[3][1]   # the refrigerant-charge question we asked earlier
    print(f"  Asking again: {q_cache}")
    status, dt, body = ask(s, q_cache)
    print(f"  status={status} elapsed={dt:.3f}s (cache hit should be < 0.1s)")
    print(f"  A: {(body.get('answer') or '')[:150]}")

    # ── PART 4: final health check ──
    print(f"\n{'='*100}")
    print("PART 4 - Final /api/health")
    print(f"{'='*100}\n")
    h = requests.get(f"{BASE}/api/health").json()
    print(json.dumps(h, indent=2))


if __name__ == "__main__":
    # Make stdout tolerant to Croatian chars on Windows
    if sys.stdout.encoding.lower() != "utf-8":
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    main()
