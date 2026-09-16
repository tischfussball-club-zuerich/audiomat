"""An in-memory ring of recent log records, so the web UI can show the log
without a terminal and without depending on systemd being the launcher."""

from __future__ import annotations

import collections
import logging
import threading
import time
from typing import Any

MAX_RECORDS = 800
LEVELS = ("DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL")


class RingHandler(logging.Handler):
    def __init__(self, capacity: int = MAX_RECORDS):
        super().__init__()
        self._lock_ring = threading.Lock()
        self._records: collections.deque[dict[str, Any]] = collections.deque(maxlen=capacity)

    def emit(self, record: logging.LogRecord) -> None:
        try:
            message = record.getMessage()
            if record.exc_info:
                message += "\n" + self.format(record).split("\n", 1)[-1] if self.formatter else ""
        except Exception:  # noqa: BLE001 - logging must never raise
            message = "<unprintable log record>"
        entry = {
            "time": time.strftime("%H:%M:%S", time.localtime(record.created)),
            "epoch": record.created,
            "level": record.levelname,
            "logger": record.name,
            "message": message[:4000],
        }
        with self._lock_ring:
            self._records.append(entry)

    def records(self, min_level: str = "INFO", limit: int = 300) -> list[dict[str, Any]]:
        try:
            threshold = logging.getLevelName(min_level.upper())
            threshold = threshold if isinstance(threshold, int) else logging.INFO
        except Exception:  # noqa: BLE001
            threshold = logging.INFO
        with self._lock_ring:
            items = list(self._records)
        out = [r for r in items if logging.getLevelName(r["level"]) >= threshold] if threshold > logging.DEBUG else items
        return out[-max(1, min(limit, MAX_RECORDS)) :]


_RING: RingHandler | None = None


def install(level: int = logging.INFO) -> RingHandler:
    """Attach the ring to the package logger (idempotent)."""
    global _RING  # noqa: PLW0603
    if _RING is None:
        _RING = RingHandler()
        _RING.setLevel(logging.DEBUG)
        _RING.setFormatter(logging.Formatter("%(message)s"))
        logging.getLogger("tfcz").addHandler(_RING)
        logging.getLogger("tfcz").setLevel(min(level, logging.INFO))
    return _RING


def ring() -> RingHandler | None:
    return _RING
