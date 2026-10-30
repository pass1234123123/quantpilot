"""Persistent pending-event queue (doc 6.1: local offline cache / event re-send).

Events that cannot be published (NATS or cloud API unavailable) are queued to
memory and mirrored to a JSONL file so they survive agent restarts. node_id is
stamped at publish time, not at enqueue time.
"""

from __future__ import annotations

import json
import threading
from pathlib import Path
from typing import Any


class PendingEvents:
    def __init__(self, state_dir: Path) -> None:
        self._lock = threading.Lock()
        self._state_dir = state_dir
        self._file = state_dir / "pending_events.jsonl"
        self._items: list[dict[str, Any]] = []
        state_dir.mkdir(parents=True, exist_ok=True)
        self._load()

    def _load(self) -> None:
        if not self._file.exists():
            return
        for line in self._file.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                self._items.append(json.loads(line))
            except json.JSONDecodeError:
                continue

    def _save(self) -> None:
        tmp = self._file.with_suffix(".jsonl.tmp")
        with open(tmp, "w", encoding="utf-8") as fh:
            for item in self._items:
                fh.write(json.dumps(item, ensure_ascii=False, default=str) + "\n")
        tmp.replace(self._file)

    def add(self, item: dict[str, Any]) -> None:
        with self._lock:
            self._items.append(item)
            self._save()

    def snapshot(self) -> list[dict[str, Any]]:
        with self._lock:
            return list(self._items)

    def remove(self, event_id: str) -> None:
        with self._lock:
            before = len(self._items)
            self._items = [i for i in self._items if i.get("event_id") != event_id]
            if len(self._items) != before:
                self._save()

    def __len__(self) -> int:
        with self._lock:
            return len(self._items)
