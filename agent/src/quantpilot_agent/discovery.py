"""MT5 terminal discovery (PRD 3.1 / 10.4, acceptance AT-001).

Scans %APPDATA%/MetaQuotes/Terminal/*/origin.txt. Each subdirectory with an
origin.txt is a terminal data directory; origin.txt holds the absolute path
of the terminal executable that created it. Discovery is read-only.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from .log import info


@dataclass(frozen=True)
class DiscoveredTerminal:
    data_dir: Path
    exe_path: str


def _terminals_root() -> Path:
    appdata = os.environ.get("APPDATA")
    if not appdata:
        raise RuntimeError("APPDATA not set; not a Windows session?")
    return Path(appdata) / "MetaQuotes" / "Terminal"


def discover_terminals() -> list[DiscoveredTerminal]:
    root = _terminals_root()
    found: list[DiscoveredTerminal] = []
    if not root.exists():
        info("discovery.root_missing", root=str(root))
        return found
    for entry in sorted(root.iterdir()):
        if not entry.is_dir():
            continue
        origin = entry / "origin.txt"
        if not origin.exists():
            continue
        try:
            exe_path = origin.read_text(encoding="utf-16").strip().rstrip("\x00")
        except UnicodeDecodeError:
            exe_path = origin.read_text(encoding="utf-8", errors="replace").strip().rstrip("\x00")
        if not exe_path:
            continue
        found.append(DiscoveredTerminal(data_dir=entry, exe_path=exe_path))
    info("discovery.found", count=len(found), root=str(root))
    for t in found:
        info("discovery.terminal", data_dir=str(t.data_dir), exe_path=t.exe_path)
    return found
