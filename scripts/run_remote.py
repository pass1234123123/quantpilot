"""Run a command on the remote server via SSH (reads QP_HOST/QP_USER/QP_PASS
from .env, then pipes stdin into .server/run.py). Usage:

    echo "command" | python scripts/run_remote.py
or:
    python scripts/run_remote.py "command"
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "packages" / "contracts" / "src"))
from quantpilot_contracts.envfile import load_env_file  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]
load_env_file(ROOT / ".env")

cmd = sys.argv[1] if len(sys.argv) > 1 else sys.stdin.read()
runner = ROOT / ".server" / "run.py"
proc = subprocess.run(
    [sys.executable, str(runner)],
    input=cmd,
    capture_output=True,
    text=True,
    env={**os.environ},
)
if proc.stdout:
    print(proc.stdout)
    sys.exit(proc.returncode or 0)
if proc.stderr:
    print("[stderr]", proc.stderr[:2000])
    sys.exit(proc.returncode or 1)