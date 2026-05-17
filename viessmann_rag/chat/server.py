"""Flask chat server.

Routes:
  GET  /                  → frontend (index.html)
  GET  /static/*          → frontend assets
  GET  /api/health        → version + uptime + cache stats (public)
  POST /api/login         → sets session cookie
  POST /api/logout        → clears session
  GET  /api/check-auth    → { logged_in: bool }
  POST /api/chat          → JSON: { question, history? } → { answer, sources }
  POST /api/chat/stream   → SSE: emits 'sources' then 'token' events, ends with 'done'
"""
from __future__ import annotations

import json
import logging
import queue
import threading
import time
from functools import wraps
from typing import Any

from flask import (
    Flask, Response, jsonify, request, send_from_directory, session,
    stream_with_context,
)
from flask_cors import CORS

from .. import __version__
from ..cache import cache as query_cache
from ..config import (
    CHAT_PASSWORD,
    CHAT_PORT,
    CHAT_USERNAME,
    FLASK_SECRET_KEY,
    LLM_PROVIDER,
    WEB_DIR,
)
from ..llm import CHAT_MODEL, QuotaExhausted, chat_completion, chat_stream
from ..logging_setup import configure
from ..metrics import record as metric
from ..prompts import NO_CONTEXT_REPLY, SYSTEM_PROMPT
from ..retrieval import retrieve

log = logging.getLogger("chat")

_QUOTA_BODY = {
    "error": "OpenAI API kvota je iscrpljena. Molimo dodajte sredstva u OpenAI račun."
}

_STARTED_AT = time.time()


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

    # Background Drive poller — runs inside this Flask process so the chat UI
    # can subscribe to live progress events. Guarded internally; safe to call
    # if create_app() is ever invoked more than once.
    from ..ingest.progress import controller as ingest_controller
    ingest_controller.start_background_poll()

    return app


# ─── Auth decorator ────────────────────────────────────────────────────────
def login_required(f):
    @wraps(f)
    def w(*a: Any, **kw: Any):
        if not session.get("logged_in"):
            return jsonify({"error": "Unauthorized"}), 401
        return f(*a, **kw)
    return w


# ─── Shared retrieval-and-context build (used by both /api/chat variants) ──
def _build_chat_context(d: dict) -> tuple[str, str, list, str, list, dict]:
    """Pull params from request, run retrieval, build OpenAI messages.

    Returns:
        question, product_line, history, system_prompt, sources_meta, debug

    `sources_meta` is the list of dicts we serialize back to the client.
    `debug` carries fields for the metrics log.

    Raises QuotaExhausted if retrieval can't complete.
    Returns ([], ...) sources_meta if no chunks found — caller handles refusal.
    """
    question      = (d.get("question") or "").strip()
    product_line  = d.get("product_line")  or None
    document_type = d.get("document_type") or None
    history       = d.get("history") or []
    if not question:
        raise ValueError("empty_question")

    chunks = retrieve(question, product_line, document_type)
    log.info("Retrieved %d chunks (post-rerank)", len(chunks))
    for i, c in enumerate(chunks[:5]):
        log.info(
            "  [%d] %s p.%s  rr=%.2f  hyb=%.3f  table=%s",
            i + 1, c.get("file_name"), c.get("page_number"),
            c.get("rerank_score", 0), c.get("hybrid_score", 0),
            c.get("has_table"),
        )

    sources_meta = [{
        "file_name":       c.get("file_name"),
        "page_number":     c.get("page_number"),
        "section_heading": c.get("section_heading"),
        "product_line":    c.get("product_line"),
        "document_type":   c.get("document_type"),
        "rerank_score":    round(c.get("rerank_score", 0), 2),
        "hybrid_score":    round(c.get("hybrid_score", 0), 3),
        "has_table":       c.get("has_table"),
    } for c in chunks]

    if not chunks:
        return question, product_line, history, "", sources_meta, {
            "retrieved": 0, "top_rerank": None,
        }

    # Strip the "[Document: filename.pdf · Page N]" header we add at ingest
    # time. Frane's team doesn't want any document/page references in the
    # answer — easiest way to guarantee that is to never show the model the
    # filename in the first place. Combined with rule 4 in SYSTEM_PROMPT,
    # this is belt-and-suspenders.
    def _strip_doc_header(t: str) -> str:
        if t.startswith("[Document:"):
            nl = t.find("\n")
            if nl != -1:
                return t[nl + 1:]
        return t

    context = "\n\n---\n\n".join(_strip_doc_header(c["chunk_text"]) for c in chunks)
    user_msg = f"Dokumentacija (izvadci):\n\n{context}\n\nPitanje: {question}"

    return question, product_line, history, user_msg, sources_meta, {
        "retrieved":  len(chunks),
        "top_rerank": round(chunks[0].get("rerank_score", 0), 2),
    }


def _register_routes(app: Flask) -> None:
    # ─── Public: health ────────────────────────────────────────────────
    @app.get("/api/health")
    def health():
        return jsonify({
            "status":     "ok",
            "version":    __version__,
            "uptime_s":   int(time.time() - _STARTED_AT),
            "cache":      query_cache.stats(),
        })

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

    # ─── Chat (non-streaming, JSON) ────────────────────────────────────
    @app.post("/api/chat")
    @login_required
    def chat():
        t0 = time.time()
        d = request.get_json() or {}
        question = (d.get("question") or "").strip()
        nocache  = bool(d.get("nocache"))

        if not question:
            return jsonify({"error": "Pitanje ne smije biti prazno."}), 400

        # Cache lookup
        cache_key = query_cache.make_key(
            question, d.get("product_line"), d.get("document_type"),
            d.get("history") or [],
        )
        if not nocache:
            cached = query_cache.get(cache_key)
            if cached:
                log.info("Cache HIT for %s", cache_key[:12])
                metric(
                    latency_ms=int((time.time() - t0) * 1000),
                    question=question, status=200, cache_hit=True,
                    answer_len=len(cached.get("answer") or ""),
                )
                return jsonify(cached)

        log.info("Q: %s", question[:200])

        try:
            try:
                q, _pl, history, user_msg, sources_meta, dbg = \
                    _build_chat_context(d)
            except QuotaExhausted:
                metric(latency_ms=int((time.time() - t0) * 1000),
                       question=question, status=503, error="quota_exhausted")
                return jsonify(_QUOTA_BODY), 503

            if not sources_meta:
                resp_body = {"answer": NO_CONTEXT_REPLY, "sources": []}
                query_cache.set(cache_key, resp_body)
                metric(latency_ms=int((time.time() - t0) * 1000),
                       question=question, status=200,
                       retrieved=0, answer_len=len(NO_CONTEXT_REPLY))
                return jsonify(resp_body)

            messages = [
                {"role": "system", "content": SYSTEM_PROMPT},
                *history,
                {"role": "user", "content": user_msg},
            ]

            try:
                resp = chat_completion(
                    model=CHAT_MODEL, messages=messages,
                    temperature=0.1, max_tokens=900,
                )
            except QuotaExhausted:
                metric(latency_ms=int((time.time() - t0) * 1000),
                       question=question, status=503, error="quota_exhausted")
                return jsonify(_QUOTA_BODY), 503

            answer = resp.choices[0].message.content or NO_CONTEXT_REPLY
            log.info("Answer generated (%d chars)", len(answer))

            resp_body = {"answer": answer, "sources": sources_meta}
            query_cache.set(cache_key, resp_body)

            metric(
                latency_ms=int((time.time() - t0) * 1000),
                question=question, status=200,
                retrieved=dbg["retrieved"], top_rerank=dbg["top_rerank"],
                answer_len=len(answer),
            )
            return jsonify(resp_body)

        except Exception as e:
            log.error("Chat error: %s", e, exc_info=True)
            metric(latency_ms=int((time.time() - t0) * 1000),
                   question=question, status=500, error=type(e).__name__)
            return jsonify(
                {"error": "Greška pri obradi pitanja. Pokušajte ponovo."}
            ), 500

    # ─── Chat (streaming, SSE) ─────────────────────────────────────────
    @app.post("/api/chat/stream")
    @login_required
    def chat_stream():
        t0 = time.time()
        d = request.get_json() or {}
        question = (d.get("question") or "").strip()
        nocache  = bool(d.get("nocache"))

        if not question:
            return jsonify({"error": "Pitanje ne smije biti prazno."}), 400

        cache_key = query_cache.make_key(
            question, d.get("product_line"), d.get("document_type"),
            d.get("history") or [],
        )

        # If we have a cached result, stream it back as a single token block —
        # gives the client a consistent SSE protocol regardless of cache state.
        if not nocache:
            cached = query_cache.get(cache_key)
            if cached:
                log.info("Cache HIT (stream) for %s", cache_key[:12])

                def cached_stream():
                    yield _sse({"type": "sources", "sources": cached["sources"]})
                    yield _sse({"type": "token", "content": cached["answer"]})
                    yield _sse({"type": "done", "cached": True})

                metric(latency_ms=int((time.time() - t0) * 1000),
                       question=question, status=200, cache_hit=True,
                       answer_len=len(cached.get("answer") or ""))
                return Response(stream_with_context(cached_stream()),
                                mimetype="text/event-stream",
                                headers={"X-Accel-Buffering": "no",
                                         "Cache-Control": "no-cache"})

        log.info("Q (stream): %s", question[:200])

        # Build context up-front (retrieval is not streamed; only the answer is)
        try:
            try:
                _q, _pl, history, user_msg, sources_meta, dbg = \
                    _build_chat_context(d)
            except QuotaExhausted:
                metric(latency_ms=int((time.time() - t0) * 1000),
                       question=question, status=503, error="quota_exhausted")

                def quota_stream():
                    yield _sse({"type": "error", **_QUOTA_BODY})
                return Response(stream_with_context(quota_stream()),
                                mimetype="text/event-stream", status=503)

            if not sources_meta:
                # No chunks — emit the refusal as a single token then done
                resp_body = {"answer": NO_CONTEXT_REPLY, "sources": []}
                query_cache.set(cache_key, resp_body)

                def empty_stream():
                    yield _sse({"type": "sources", "sources": []})
                    yield _sse({"type": "token", "content": NO_CONTEXT_REPLY})
                    yield _sse({"type": "done"})

                metric(latency_ms=int((time.time() - t0) * 1000),
                       question=question, status=200,
                       retrieved=0, answer_len=len(NO_CONTEXT_REPLY))
                return Response(stream_with_context(empty_stream()),
                                mimetype="text/event-stream",
                                headers={"X-Accel-Buffering": "no",
                                         "Cache-Control": "no-cache"})

            messages = [
                {"role": "system", "content": SYSTEM_PROMPT},
                *history,
                {"role": "user", "content": user_msg},
            ]
        except Exception as e:
            log.error("Stream prep error: %s", e, exc_info=True)
            metric(latency_ms=int((time.time() - t0) * 1000),
                   question=question, status=500, error=type(e).__name__)

            def err_stream():
                yield _sse({"type": "error",
                            "error": "Greška pri obradi pitanja. Pokušajte ponovo."})
            return Response(stream_with_context(err_stream()),
                            mimetype="text/event-stream", status=500)

        def generate():
            # 1. Sources event first
            yield _sse({"type": "sources", "sources": sources_meta})

            # 2. Token stream — chat_stream() iterates delta strings.
            #    Same interface for both providers (see llm.py).
            collected: list[str] = []
            try:
                for delta in chat_stream(
                    model=CHAT_MODEL, messages=messages,
                    temperature=0.1, max_tokens=900,
                ):
                    collected.append(delta)
                    yield _sse({"type": "token", "content": delta})
            except QuotaExhausted:
                yield _sse({"type": "error", **_QUOTA_BODY})
                return
            except Exception as e:
                log.error("Stream LLM error (%s): %s",
                          LLM_PROVIDER, e, exc_info=True)
                yield _sse({"type": "error",
                            "error": "Greška pri generiranju odgovora."})
                return

            answer = "".join(collected) or NO_CONTEXT_REPLY
            # Cache the assembled answer for the next identical request
            query_cache.set(cache_key, {"answer": answer, "sources": sources_meta})

            metric(latency_ms=int((time.time() - t0) * 1000),
                   question=question, status=200,
                   retrieved=dbg["retrieved"], top_rerank=dbg["top_rerank"],
                   answer_len=len(answer))

            yield _sse({"type": "done"})

        return Response(stream_with_context(generate()),
                        mimetype="text/event-stream",
                        headers={"X-Accel-Buffering": "no",
                                 "Cache-Control": "no-cache"})

    # ─── Drive ingest: status + manual trigger + live SSE events ───────
    @app.get("/api/ingest/status")
    @login_required
    def ingest_status():
        from ..ingest.progress import controller
        return jsonify(controller.state)

    @app.post("/api/ingest/poll")
    @login_required
    def ingest_poll():
        """Kick off a Drive sync. Returns immediately; progress via SSE."""
        from ..ingest.progress import controller
        # Run in a daemon thread so the HTTP response doesn't block on the
        # actual sync (which can take minutes when there are new PDFs).
        threading.Thread(
            target=controller.trigger_run, args=("manual",),
            daemon=True, name="ManualDriveSync",
        ).start()
        return jsonify({"ok": True, "trigger": "manual"})

    @app.get("/api/ingest/events")
    @login_required
    def ingest_events():
        """SSE stream of progress events. One subscriber per browser tab."""
        from ..ingest.progress import controller

        def stream():
            q = controller.subscribe()
            try:
                while True:
                    try:
                        event = q.get(timeout=25)
                        yield _sse(event)
                    except queue.Empty:
                        # Comment line — keeps reverse-proxies from closing
                        # the connection during quiet periods.
                        yield ": heartbeat\n\n"
            except GeneratorExit:
                controller.unsubscribe(q)
                raise

        return Response(
            stream_with_context(stream()),
            mimetype="text/event-stream",
            headers={"X-Accel-Buffering": "no", "Cache-Control": "no-cache"},
        )

    # ─── Frontend ──────────────────────────────────────────────────────
    @app.route("/")
    def index():
        return send_from_directory(str(WEB_DIR), "index.html")


def _sse(payload: dict) -> str:
    """Serialize a single Server-Sent Event."""
    return f"data: {json.dumps(payload, ensure_ascii=False)}\n\n"


# ─── Entry point ───────────────────────────────────────────────────────────
def run() -> None:
    configure("chat")
    log.info("Starting Viessmann Chat v%s on http://localhost:%d",
             __version__, CHAT_PORT)
    app = create_app()
    # threaded=True so SSE streams from multiple clients don't block each other
    app.run(host="0.0.0.0", port=CHAT_PORT, debug=False, threaded=True)


if __name__ == "__main__":
    run()
