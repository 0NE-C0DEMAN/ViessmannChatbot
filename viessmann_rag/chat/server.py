"""Flask chat server.

Routes:
  GET  /                  → frontend (index.html)
  GET  /static/*          → frontend assets
  POST /api/login         → sets session cookie
  POST /api/logout        → clears session
  GET  /api/check-auth    → { logged_in: bool }
  POST /api/chat          → { question, product_line?, document_type?, history? }
                            → { answer, sources: [{file_name, page_number, ...}] }
"""
from __future__ import annotations

import logging
from functools import wraps
from typing import Any

from flask import Flask, jsonify, request, send_from_directory, session
from flask_cors import CORS

from ..config import (
    CHAT_MODEL,
    CHAT_PASSWORD,
    CHAT_PORT,
    CHAT_USERNAME,
    FLASK_SECRET_KEY,
    WEB_DIR,
)
from ..logging_setup import configure
from ..openai_client import QuotaExhausted, chat_completion
from ..prompts import NO_CONTEXT_REPLY, SYSTEM_PROMPT
from ..retrieval import retrieve

log = logging.getLogger("chat")


# ─── Quota error helper ────────────────────────────────────────────────────
_QUOTA_BODY = {
    "error": "OpenAI API kvota je iscrpljena. Molimo dodajte sredstva u OpenAI račun."
}


# ─── App factory ───────────────────────────────────────────────────────────
def create_app() -> Flask:
    app = Flask(
        __name__,
        static_folder=str(WEB_DIR / "static"),
        static_url_path="/static",
    )
    app.secret_key = FLASK_SECRET_KEY
    CORS(app, supports_credentials=True)

    _register_routes(app)
    return app


# ─── Auth decorator ────────────────────────────────────────────────────────
def login_required(f):
    @wraps(f)
    def w(*a: Any, **kw: Any):
        if not session.get("logged_in"):
            return jsonify({"error": "Unauthorized"}), 401
        return f(*a, **kw)
    return w


def _register_routes(app: Flask) -> None:
    # ─── Auth ──────────────────────────────────────────────────────────
    @app.post("/api/login")
    def login():
        d = request.get_json() or {}
        u = (d.get("username") or "").strip()
        p = (d.get("password") or "").strip()
        if u == CHAT_USERNAME and p == CHAT_PASSWORD:
            session["logged_in"] = True
            log.info("Login OK")
            return jsonify({"ok": True})
        log.warning("Login failed for user=%s", u)
        return jsonify({"error": "Pogrešno korisničko ime ili lozinka."}), 401

    @app.post("/api/logout")
    def logout():
        session.clear()
        return jsonify({"ok": True})

    @app.get("/api/check-auth")
    def check_auth():
        return jsonify({"logged_in": bool(session.get("logged_in"))})

    # ─── Chat ──────────────────────────────────────────────────────────
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
                chunks = retrieve(question, product_line, document_type)
            except QuotaExhausted:
                log.error("Quota exhausted on retrieve")
                return jsonify(_QUOTA_BODY), 503

            log.info("Retrieved %d chunks (post-rerank)", len(chunks))
            for i, c in enumerate(chunks[:5]):
                log.info(
                    "  [%d] %s p.%s  rr=%.2f  hyb=%.3f  sem=%.3f  kw=%.3f  table=%s",
                    i + 1,
                    c.get("file_name"),
                    c.get("page_number"),
                    c.get("rerank_score", 0),
                    c.get("hybrid_score", 0),
                    c.get("semantic_score", 0),
                    c.get("keyword_score", 0),
                    c.get("has_table"),
                )

            if not chunks:
                return jsonify({"answer": NO_CONTEXT_REPLY, "sources": []})

            context = "\n\n---\n\n".join(c["chunk_text"] for c in chunks)
            messages = [
                {"role": "system", "content": SYSTEM_PROMPT},
                *history,
                {"role": "user",
                 "content": f"Dokumentacija (izvadci):\n\n{context}\n\nPitanje: {question}"},
            ]

            try:
                resp = chat_completion(
                    model=CHAT_MODEL, messages=messages,
                    temperature=0.1, max_tokens=900,
                )
            except QuotaExhausted:
                log.error("Quota exhausted on chat")
                return jsonify(_QUOTA_BODY), 503

            answer = resp.choices[0].message.content or NO_CONTEXT_REPLY
            log.info("Answer generated (%d chars)", len(answer))

            sources = [{
                "file_name":       c.get("file_name"),
                "page_number":     c.get("page_number"),
                "section_heading": c.get("section_heading"),
                "product_line":    c.get("product_line"),
                "document_type":   c.get("document_type"),
                "rerank_score":    round(c.get("rerank_score", 0), 2),
                "hybrid_score":    round(c.get("hybrid_score", 0), 3),
                "has_table":       c.get("has_table"),
            } for c in chunks]

            return jsonify({"answer": answer, "sources": sources})

        except Exception as e:
            log.error("Chat error: %s", e, exc_info=True)
            return jsonify(
                {"error": "Greška pri obradi pitanja. Pokušajte ponovo."}
            ), 500

    # ─── Frontend ──────────────────────────────────────────────────────
    @app.route("/")
    def index():
        return send_from_directory(str(WEB_DIR), "index.html")


# ─── Entry point ───────────────────────────────────────────────────────────
def run() -> None:
    configure("chat")
    log.info("Starting Viessmann Chat on http://localhost:%d", CHAT_PORT)
    app = create_app()
    app.run(host="0.0.0.0", port=CHAT_PORT, debug=False)


if __name__ == "__main__":
    run()
