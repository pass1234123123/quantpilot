"""R3.3 P1-02 acceptance test: a foreign Redis on the port must survive us.

The exact scenario from the acceptance report: an existing
password-less Redis service is already listening on the requested test
port. The multiprocess test script (the R3.2 version) could then

  * connect to the foreign server (mistaking it for its own),
  * FLUSHDB its current database, and
  * redis-cli SHUTDOWN it,

because it never verified that the port actually belonged to the
redis-server it spawned.

This test plants a REAL password-less sentinel redis-server holding
keys, runs the hardened multiprocess script against that port, and
demands:

  1 the script FAILS immediately (non-zero exit, before any test runs)
  2 the sentinel is still alive and serving afterwards (no SHUTDOWN)
  3 every sentinel key survived with its exact value (no FLUSHDB /
     FLUSHALL / DEL of foreign keys)
  4 the sentinel process is still OUR spawned child (never killed)

The sentinel is deliberately password-less: that is the "existing
unprotected Redis service" from the report, the worst case. If the
hardening regresses, this test really does lose its keys.

Usage (from the repo root, venv active):
  python scripts/r33_port_conflict_test.py
"""

from __future__ import annotations

import os
import socket
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "packages" / "contracts" / "src"))

import redis as redis_lib  # noqa: E402

from quantpilot_contracts.envfile import load_env_file  # noqa: E402

load_env_file(ROOT / ".env")

SENTINEL_KEYS = {
    "foreign:string": "precious-data-must-survive",
    "foreign:counter": "42",
    "foreign:hash": "v1,v2,v3",
}

LOG_LINES: list[str] = []
RESULTS: list[tuple[str, bool]] = []


def log(message: str) -> None:
    line = time.strftime("%Y-%m-%d %H:%M:%S") + " | " + message
    LOG_LINES.append(line)
    print(line, flush=True)


def check(name: str, ok: bool, detail: str = "") -> bool:
    RESULTS.append((name, ok))
    log(("PASS  " if ok else "FAIL  ") + name + (f"  [{detail}]" if detail else ""))
    return ok


def pick_free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


def sentinel_start(redis_dir: Path, port: int, log_handle) -> subprocess.Popen:
    """A password-less foreign Redis - exactly the unprotected service
    the acceptance report warned about."""
    proc = subprocess.Popen(
        [
            str(redis_dir / "redis-server.exe"),
            "--port", str(port),
            "--bind", "127.0.0.1",
            # NO requirepass: this is the worst-case stranger
            "--save", "",
            "--appendonly", "no",
        ],
        stdout=log_handle,
        stderr=subprocess.STDOUT,
        cwd=str(redis_dir),
    )
    client = redis_lib.Redis(
        host="127.0.0.1", port=port,
        socket_connect_timeout=0.5, protocol=2,
    )
    deadline = time.time() + 15.0
    while time.time() < deadline:
        if proc.poll() is not None:
            raise RuntimeError("sentinel redis-server exited during startup")
        try:
            if client.ping():
                return proc
        except Exception:
            pass
        time.sleep(0.3)
    raise RuntimeError(f"sentinel redis-server did not come up on :{port}")


def main() -> int:
    redis_dir = Path(
        os.environ.get(
            "R32_REDIS_DIR",
            str(Path(os.environ.get("TEMP", ".")) / "qp-r32-redis" / "pkg"),
        )
    )
    if not (redis_dir / "redis-server.exe").exists():
        log(f"redis-server.exe not found in {redis_dir}")
        return 2

    log_dir = ROOT / "artifacts" / "r3.3"
    log_dir.mkdir(parents=True, exist_ok=True)
    sentinel_port = pick_free_port()
    sentinel_log = open(log_dir / "port-conflict-sentinel.log", "ab")
    sentinel: subprocess.Popen | None = None

    try:
        # ---- plant the foreign service ---------------------------------
        log(f"starting password-less sentinel redis on 127.0.0.1:{sentinel_port}")
        sentinel = sentinel_start(redis_dir, sentinel_port, sentinel_log)
        client = redis_lib.Redis(
            host="127.0.0.1", port=sentinel_port,
            decode_responses=True, protocol=2,
        )
        for key, value in SENTINEL_KEYS.items():
            client.set(key, value)
        before_keys = sorted(client.keys("*"))
        check(
            "setup: sentinel holds foreign keys",
            before_keys == sorted(SENTINEL_KEYS.keys()),
            f"keys={before_keys}",
        )

        # ---- run the hardened script against the occupied port --------
        log("running the multiprocess script against the occupied port ...")
        result = subprocess.run(
            [
                sys.executable,
                str(ROOT / "scripts" / "r32_real_redis_multiprocess.py"),
                "--redis-port", str(sentinel_port),
                "--log-dir", str(log_dir / "port-conflict-run"),
            ],
            capture_output=True,
            text=True,
            cwd=str(ROOT),
            timeout=120,
        )
        tail = (result.stdout or "").strip().splitlines()[-6:]
        log("multiprocess script stdout tail: " + " / ".join(tail))

        # 1. must fail immediately
        check(
            "1 script fails on the occupied port (non-zero exit)",
            result.returncode != 0,
            f"exit={result.returncode}",
        )

        # 2. the sentinel must still be alive and serving
        alive = sentinel.poll() is None
        pong = False
        try:
            pong = bool(client.ping())
        except Exception:
            pong = False
        check(
            "2 foreign redis still alive and serving (no SHUTDOWN)",
            alive and pong,
            f"poll={sentinel.poll()} ping={pong}",
        )

        # 3. every foreign key survived with its exact value
        after_keys = sorted(client.keys("*"))
        keys_intact = after_keys == before_keys
        values_intact = all(
            client.get(key) == value for key, value in SENTINEL_KEYS.items()
        )
        check(
            "3 foreign keys AND values unchanged (no FLUSHDB/FLUSHALL/DEL)",
            keys_intact and values_intact,
            f"keys={after_keys} values_ok={values_intact}",
        )

        # 4. extra evidence from the run's own log: the failure must be
        #    the port preflight, not a later stage
        run_log = log_dir / "port-conflict-run" / "multiprocess-test.log"
        refused_early = False
        if run_log.exists():
            text = run_log.read_text(encoding="utf-8")
            refused_early = (
                "preflight: requested redis port free" in text
                and "FAIL" in text
                and "boot:" not in text
            )
        check(
            "4 the refusal came from the port preflight (nothing started)",
            refused_early,
            f"log={run_log}",
        )

        passed = sum(1 for _, ok in RESULTS if ok)
        failed = len(RESULTS) - passed
        log(f"=== SUMMARY: {passed} passed, {failed} failed, {len(RESULTS)} checks ===")
        return 0 if failed == 0 else 1
    finally:
        if sentinel is not None and sentinel.poll() is None:
            sentinel.terminate()
            try:
                sentinel.wait(timeout=10)
            except subprocess.TimeoutExpired:
                sentinel.kill()
                sentinel.wait(timeout=10)
            log("stopped the sentinel redis (our own child)")
        log_file = log_dir / "port-conflict-test.log"
        log_file.write_text("\n".join(LOG_LINES) + "\n", encoding="utf-8")
        print(f"full log -> {log_file}")


if __name__ == "__main__":
    raise SystemExit(main())
