"""In-process Drive-sync controller with live progress broadcasting.

Replaces the old `python ingest.py --drive --loop` subprocess. Runs the
poll loop in a daemon thread inside the Flask app so the chat UI can
subscribe to a live event stream (SSE) and render progress as it happens.

Key behaviors:
  - Only one sync runs at a time (run_lock). A manual trigger while an
    auto-poll is in flight is dropped — the user sees the in-flight run.
  - Subscribers get a "snapshot" event on subscribe so late-joining tabs
    can render the current state immediately.
  - Heartbeats are emitted from the SSE handler, not here, so this module
    stays UI-agnostic.
"""
from __future__ import annotations

import logging
import queue
import threading
import time
from datetime import datetime, timedelta, timezone
from typing import Any, Optional

from ..config import POLL_INTERVAL_SECONDS, drive_configured

log = logging.getLogger("ingest")


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _iso_in(seconds: int) -> str:
    return (datetime.now(timezone.utc) + timedelta(seconds=seconds)).isoformat()


class IngestController:
    """Singleton-ish — instantiate once at module bottom, import everywhere."""

    def __init__(self) -> None:
        self._run_lock = threading.Lock()
        self._sub_lock = threading.Lock()
        self._subscribers: list[queue.Queue] = []
        self._thread_started = False

        self.state: dict[str, Any] = {
            "status":          "idle",   # idle | scanning | processing | done | error
            "trigger":         None,     # "auto" | "manual" | None
            "scanned":         None,     # int — PDFs found in Drive on last scan
            "to_process":      0,        # int — new/changed since last scan
            "processed":       0,        # int — done in current run
            "current_file":    None,     # str — currently embedding
            "last_run_at":     None,     # ISO timestamp of last complete run
            "last_new_count":  0,        # how many NEW files surfaced last run
            "next_run_at":     None,     # ISO ts of scheduled next auto-poll
            "interval_s":      POLL_INTERVAL_SECONDS,
            "error":           None,
        }

    # ─── SSE subscriber management ─────────────────────────────────────
    def subscribe(self) -> queue.Queue:
        """Returns a Queue. The caller blocks on `q.get()` and yields events."""
        q: queue.Queue = queue.Queue(maxsize=200)
        with self._sub_lock:
            self._subscribers.append(q)
        # Snapshot so late joiners can paint their initial state
        q.put({"type": "snapshot", **self.state})
        return q

    def unsubscribe(self, q: queue.Queue) -> None:
        with self._sub_lock:
            if q in self._subscribers:
                self._subscribers.remove(q)

    def _broadcast(self, event: dict) -> None:
        # Stamp the trigger so the client can distinguish manual vs auto without
        # tracking it locally. Cheap and keeps the protocol self-describing.
        event = {**event}
        event.setdefault("trigger", self.state.get("trigger"))
        with self._sub_lock:
            dead: list[queue.Queue] = []
            for q in self._subscribers:
                try:
                    q.put_nowait(event)
                except queue.Full:
                    dead.append(q)  # slow consumer → drop
            for q in dead:
                self._subscribers.remove(q)

    # ─── State helper ──────────────────────────────────────────────────
    def _update(self, **kw: Any) -> None:
        self.state.update(kw)

    # ─── Single run (shared by auto-poll and manual trigger) ───────────
    def trigger_run(self, trigger: str) -> bool:
        """Run one sync. Returns False if another run is already in flight."""
        # Local import — avoids a cycle through drive.py at module load.
        from .drive import get_drive_service, run_once

        if not self._run_lock.acquire(blocking=False):
            log.info("Sync already in progress — %s trigger skipped", trigger)
            return False

        try:
            self._update(
                status="scanning", trigger=trigger,
                processed=0, current_file=None, error=None,
                scanned=None, to_process=0,
            )
            self._broadcast({"type": "run_start", **self.state})

            try:
                service = get_drive_service()

                def on_scan_done(found: int, to_process: int, to_delete: int) -> None:
                    self._update(
                        scanned=found, to_process=to_process,
                        status="processing" if to_process else "done",
                    )
                    self._broadcast({
                        "type": "scan_done",
                        "found": found, "to_process": to_process,
                        "to_delete": to_delete,
                        **self.state,
                    })

                def on_file_start(file_name: str, idx: int, total: int) -> None:
                    self._update(current_file=file_name, processed=idx - 1)
                    self._broadcast({
                        "type": "file_start",
                        "file": file_name, "idx": idx, "total": total,
                        **self.state,
                    })

                def on_file_done(file_name: str, idx: int, total: int,
                                 ok: bool) -> None:
                    self._update(processed=idx, current_file=None)
                    self._broadcast({
                        "type": "file_done",
                        "file": file_name, "idx": idx, "total": total,
                        "ok": ok,
                        **self.state,
                    })

                run_once(
                    service,
                    on_scan_done=on_scan_done,
                    on_file_start=on_file_start,
                    on_file_done=on_file_done,
                )

                new_count = self.state.get("to_process", 0)
                self._update(
                    status="done",
                    last_run_at=_now_iso(),
                    last_new_count=new_count,
                    current_file=None,
                )
                self._broadcast({"type": "run_done", **self.state})

            except Exception as e:  # noqa: BLE001
                log.exception("Sync run failed")
                self._update(status="error", error=str(e), current_file=None)
                self._broadcast({
                    "type": "run_error", "error": str(e), **self.state,
                })
        finally:
            self._run_lock.release()

        return True

    # ─── Background poll loop ──────────────────────────────────────────
    def start_background_poll(self) -> None:
        if self._thread_started:
            return
        if not drive_configured():
            log.info("Drive not configured — background poller not started")
            return
        self._thread_started = True
        t = threading.Thread(
            target=self._poll_loop, daemon=True, name="DrivePoller",
        )
        t.start()
        log.info("Drive poller thread started — interval=%ds",
                 POLL_INTERVAL_SECONDS)

    def _poll_loop(self) -> None:
        # Brief delay so gunicorn finishes booting before we hammer Drive.
        time.sleep(5)
        while True:
            self._update(next_run_at=_iso_in(POLL_INTERVAL_SECONDS))
            try:
                self.trigger_run(trigger="auto")
            except Exception as e:  # noqa: BLE001
                log.error("Poll loop iteration crashed: %s", e, exc_info=True)
            time.sleep(POLL_INTERVAL_SECONDS)


# Single instance the rest of the app imports.
controller = IngestController()
