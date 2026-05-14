"""
Viessmann Chat v2 — Flask backend.

POST /api/chat   { question, product_line?, document_type?, history? }
                 → { answer, sources: [{file_name, page_number, ...}] }
POST /api/login  { username, password } → { ok: true } or 401
GET  /api/check-auth
"""
import logging
import logging.handlers
import os
import re
import time
from functools import wraps
from pathlib import Path

from dotenv import load_dotenv
from flask import Flask, jsonify, request, send_from_directory, session
from flask_cors import CORS
from openai import OpenAI, RateLimitError

load_dotenv(Path(__file__).parent / ".env")

from retrieval import retrieve
from prompts   import SYSTEM_PROMPT, NO_CONTEXT_REPLY

OPENAI_API_KEY = os.environ["OPENAI_API_KEY"]
CHAT_USERNAME  = os.environ.get("CHAT_USERNAME", "viessmann")
CHAT_PASSWORD  = os.environ.get("CHAT_PASSWORD", "carrier")
SECRET_KEY     = os.environ.get("FLASK_SECRET_KEY", "viessmann-v2")
PORT           = int(os.environ.get("CHAT_PORT", 8081))
CHAT_MODEL     = "gpt-4o"

# ─── Logging ───────────────────────────────────────────────────────────────
LOG_DIR = Path(__file__).parent / "logs"
LOG_DIR.mkdir(exist_ok=True)
_fmt = logging.Formatter("%(asctime)s %(levelname)-8s %(message)s", datefmt="%H:%M:%S")
_fh  = logging.handlers.RotatingFileHandler(LOG_DIR / "chat.log", maxBytes=5*1024*1024, backupCount=3, encoding="utf-8")
_fh.setFormatter(_fmt)
_ch  = logging.StreamHandler(); _ch.setFormatter(_fmt)
logging.basicConfig(level=logging.INFO, handlers=[_fh, _ch])
log = logging.getLogger("chat")

# ─── App ───────────────────────────────────────────────────────────────────
app = Flask(__name__, static_folder="static", static_url_path="/static")
app.secret_key = SECRET_KEY
CORS(app, supports_credentials=True)

oai = OpenAI(api_key=OPENAI_API_KEY)


def _retry_after(err: RateLimitError) -> float | None:
    """Parse 'Please try again in 4.926s.' from a 429 message."""
    try:
        msg = str(err)
        m = re.search(r"try again in ([\d.]+)\s*(s|ms)", msg)
        if not m:
            return None
        val = float(m.group(1))
        return val if m.group(2) == "s" else val / 1000.0
    except Exception:
        return None


def login_required(f):
    @wraps(f)
    def w(*a, **kw):
        if not session.get("logged_in"):
            return jsonify({"error": "Unauthorized"}), 401
        return f(*a, **kw)
    return w


# ─── Auth ──────────────────────────────────────────────────────────────────
@app.post("/api/login")
def login():
    d = request.get_json() or {}
    if (d.get("username") or "").strip() == CHAT_USERNAME and \
       (d.get("password") or "").strip() == CHAT_PASSWORD:
        session["logged_in"] = True
        log.info("Login OK")
        return jsonify({"ok": True})
    log.warning("Login failed for user=%s", d.get("username"))
    return jsonify({"error": "Pogrešno korisničko ime ili lozinka."}), 401


@app.post("/api/logout")
def logout():
    session.clear()
    return jsonify({"ok": True})


@app.get("/api/check-auth")
def check_auth():
    return jsonify({"logged_in": bool(session.get("logged_in"))})


# ─── Chat ──────────────────────────────────────────────────────────────────
@app.post("/api/chat")
@login_required
def chat():
    d = request.get_json() or {}
    question      = (d.get("question") or "").strip()
    product_line  = d.get("product_line")  or None
    document_type = d.get("document_type") or None
    history       = d.get("history") or []

    if not question:
        return jsonify({"error": "Pitanje ne smije biti prazno."}), 400

    log.info("Q: %s", question[:200])

    try:
        try:
            chunks = retrieve(oai, question, product_line, document_type)
        except RateLimitError as e:
            if "insufficient_quota" in str(e):
                log.error("OpenAI quota exhausted — account needs credit top-up")
                return jsonify({"error": "OpenAI API kvota je iscrpljena. Molimo dodajte sredstva u OpenAI račun."}), 503
            raise
        log.info("Retrieved %d chunks (post-rerank)", len(chunks))
        for i, c in enumerate(chunks[:5]):
            log.info("  [%d] %s p.%s  rr=%.2f  hyb=%.3f  sem=%.3f  kw=%.3f  table=%s",
                     i+1,
                     c.get("file_name"),
                     c.get("page_number"),
                     c.get("rerank_score", 0),
                     c.get("hybrid_score", 0),
                     c.get("semantic_score", 0),
                     c.get("keyword_score", 0),
                     c.get("has_table"))

        if not chunks:
            return jsonify({"answer": NO_CONTEXT_REPLY, "sources": []})

        context = "\n\n---\n\n".join(c["chunk_text"] for c in chunks)
        messages = [
            {"role": "system", "content": SYSTEM_PROMPT},
            *history,
            {"role": "user",   "content": f"Dokumentacija (izvadci):\n\n{context}\n\nPitanje: {question}"},
        ]

        # Retry on rate limits — OpenAI's 30K TPM ceiling is easy to hit when
        # several queries land within the same minute.
        resp = None
        for attempt in range(4):
            try:
                resp = oai.chat.completions.create(
                    model=CHAT_MODEL,
                    messages=messages,
                    temperature=0.1,
                    max_tokens=900,
                )
                break
            except RateLimitError as e:
                if "insufficient_quota" in str(e):
                    log.error("OpenAI quota exhausted — account needs credit top-up")
                    return jsonify({"error": "OpenAI API kvota je iscrpljena. Molimo dodajte sredstva u OpenAI račun."}), 503
                wait = _retry_after(e) or (2 ** attempt + 1)
                log.warning("gpt-4o 429 (attempt %d); sleeping %.1fs", attempt + 1, wait)
                time.sleep(wait)
        if resp is None:
            return jsonify({"error": "Asistent je trenutno preopterećen. Pokušajte ponovo za nekoliko sekundi."}), 503
        answer = resp.choices[0].message.content or NO_CONTEXT_REPLY
        log.info("Answer generated (%d chars)", len(answer))

        sources = [{
            "file_name":       c.get("file_name"),
            "page_number":     c.get("page_number"),
            "section_heading": c.get("section_heading"),
            "product_line":    c.get("product_line"),
            "document_type":   c.get("document_type"),
            "rerank_score":    round(c.get("rerank_score",  0), 2),
            "hybrid_score":    round(c.get("hybrid_score",  0), 3),
            "has_table":       c.get("has_table"),
        } for c in chunks]

        return jsonify({"answer": answer, "sources": sources})

    except Exception as e:
        log.error("Chat error: %s", e, exc_info=True)
        return jsonify({"error": "Greška pri obradi pitanja. Pokušajte ponovo."}), 500


# ─── Frontend ──────────────────────────────────────────────────────────────
@app.route("/")
def index():
    return send_from_directory(".", "index.html")


if __name__ == "__main__":
    log.info("Starting Viessmann Chat v2 on http://localhost:%d", PORT)
    app.run(host="0.0.0.0", port=PORT, debug=False)
