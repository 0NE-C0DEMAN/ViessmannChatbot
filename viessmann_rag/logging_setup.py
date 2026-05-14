"""Logging setup — one rotating file handler per component."""
from __future__ import annotations

import logging
import logging.handlers

from .config import LOG_DIR

_FMT = logging.Formatter(
    "%(asctime)s %(levelname)-8s %(message)s", datefmt="%H:%M:%S",
)


def configure(component: str) -> logging.Logger:
    """Configure root logging for a CLI / server entry point.

    `component` is used to name the log file (e.g. 'ingest' → logs/ingest.log).
    Safe to call multiple times — handlers are added only once per component.
    """
    LOG_DIR.mkdir(exist_ok=True)

    root = logging.getLogger()
    if any(getattr(h, "_component", None) == component for h in root.handlers):
        return logging.getLogger(component)

    fh = logging.handlers.RotatingFileHandler(
        LOG_DIR / f"{component}.log",
        maxBytes=5 * 1024 * 1024,
        backupCount=3,
        encoding="utf-8",
    )
    fh.setFormatter(_FMT)
    fh._component = component  # type: ignore[attr-defined]

    ch = logging.StreamHandler()
    ch.setFormatter(_FMT)
    ch._component = component  # type: ignore[attr-defined]

    root.setLevel(logging.INFO)
    root.addHandler(fh)
    root.addHandler(ch)

    return logging.getLogger(component)
