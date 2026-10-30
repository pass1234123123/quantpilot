"""Tiny .env loader (no external dependency).

Precedence: real environment variables win over .env file values.
"""

from __future__ import annotations

import os
from pathlib import Path


def load_env_file(path: str | Path = ".env") -> dict[str, str]:
    env_path = Path(path)
    loaded: dict[str, str] = {}
    if not env_path.exists():
        return loaded
    for raw_line in env_path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        if key:
            loaded[key] = value
    for key, value in loaded.items():
        os.environ.setdefault(key, value)
    return loaded
