"""Minimal structured JSON logging for the agent (doc 6.2: structured JSON logs)."""

from __future__ import annotations

import json
import sys
import threading
from datetime import datetime, timezone

_lock = threading.Lock()


def log(level: str, event: str, **fields: object) -> None:
    entry = {
        "ts": datetime.now(timezone.utc).isoformat(),
        "level": level,
        "event": event,
        **fields,
    }
    line = json.dumps(entry, ensure_ascii=False, default=str)
    with _lock:
        print(line, file=sys.stderr, flush=True)


def info(event: str, **fields: object) -> None:
    log("INFO", event, **fields)


def warn(event: str, **fields: object) -> None:
    log("WARN", event, **fields)


def error(event: str, **fields: object) -> None:
    log("ERROR", event, **fields)
