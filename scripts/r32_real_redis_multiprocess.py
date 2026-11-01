"""R3.2 P2: real-Redis multi-process login-throttle verification.

Spawns ONE real redis-server plus TWO independent API processes
(uvicorn, different ports) in redis_required mode and verifies the six
mandated behaviors over real HTTP:

  1 instance A's failure counts are visible to instance B (shared Redis)
  2 after instance A restarts the counts are still there (Redis-backed,
    not per-process memory)
  3 with Redis stopped BOTH instances answer 503 LOGIN_THROTTLE_UNAVAILABLE
    (fail-closed; no silent degradation to per-instance counting)
  4 after Redis recovers both instances resume shared counting
    automatically (the 60 s stand-down window expires, no restart needed)
  5 concurrent login attempts cannot bypass the limit (atomic Lua
    INCR+EXPIRE: every recorded failure is counted exactly once)
  6 a correct login clears ONLY the (ip, username) pair bucket; the
    pure-IP spray bucket survives

This is NOT a pytest test: it drives real processes over HTTP and needs
the dev PostgreSQL tunnel (every login writes a system_events row).

Prerequisites:
  * scripts/dev_pg_tunnel.py running (QP_PG_DSN in the root .env works)
  * QP_API_KEY + QP_JWT_SECRET set in the root .env
  * a real Redis build for Windows (tporadowski 5.0.14.1): set
    R32_REDIS_DIR to the folder holding redis-server.exe / redis-cli.exe,
    or drop it in %TEMP%\\qp-r32-redis\\pkg (auto-detected)

Usage (from the repo root, venv active):
  python scripts/r32_real_redis_multiprocess.py
"""

from __future__ import annotations

import argparse
import os
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "packages" / "contracts" / "src"))

import httpx  # noqa: E402
import psycopg  # noqa: E402
import redis as redis_lib  # noqa: E402

from quantpilot_contracts.envfile import load_env_file  # noqa: E402

load_env_file(ROOT / ".env")

LOG_LINES: list[str] = []
RESULTS: list[tuple[str, bool]] = []

PAIR_KEY = "qp:login:pair:{ip}|{username}"
IP_KEY = "qp:login:ip:{ip}"
CLIENT_IP = "127.0.0.1"  # both API processes see the loopback client


def log(message: str) -> None:
    line = time.strftime("%Y-%m-%d %H:%M:%S") + " | " + message
    LOG_LINES.append(line)
    print(line, flush=True)


def check(name: str, ok: bool, detail: str = "") -> bool:
    RESULTS.append((name, ok))
    log(("PASS  " if ok else "FAIL  ") + name + (f"  [{detail}]" if detail else ""))
    return ok


# ---------------------------------------------------------------- helpers


def login(port: int, username: str, password: str) -> tuple[int, str]:
    r = httpx.post(
        f"http://127.0.0.1:{port}/api/v1/auth/login",
        json={"username": username, "password": password},
        timeout=15,
    )
    try:
        code = (r.json().get("error") or {}).get("code", "")
    except Exception:
        code = ""
    return r.status_code, code


def wait_http_healthy(port: int, timeout: float = 60.0) -> bool:
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            if httpx.get(f"http://127.0.0.1:{port}/healthz", timeout=2).status_code == 200:
                return True
        except Exception:
            pass
        time.sleep(0.5)
    return False


def wait_redis_up(port: int, timeout: float = 15.0) -> bool:
    # protocol=2: redis-py defaults to the RESP3 handshake (HELLO 3) which
    # real Redis 5.x rejects; the test server is tporadowski 5.0.14.1
    client = redis_lib.Redis(host="127.0.0.1", port=port, socket_connect_timeout=0.5, protocol=2)
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            if client.ping():
                return True
        except Exception:
            pass
        time.sleep(0.3)
    return False


def wait_redis_down(port: int, timeout: float = 10.0) -> bool:
    # protocol=2: redis-py defaults to the RESP3 handshake (HELLO 3) which
    # real Redis 5.x rejects; the test server is tporadowski 5.0.14.1
    client = redis_lib.Redis(host="127.0.0.1", port=port, socket_connect_timeout=0.5, protocol=2)
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            client.ping()
        except Exception:
            return True
        time.sleep(0.3)
    return False


# ---------------------------------------------------------------- process mgmt


def redis_start(redis_dir: Path, port: int, log_handle) -> subprocess.Popen:
    proc = subprocess.Popen(
        [
            str(redis_dir / "redis-server.exe"),
            "--port", str(port),
            "--bind", "127.0.0.1",
            "--save", "",
            "--appendonly", "no",
        ],
        stdout=log_handle,
        stderr=subprocess.STDOUT,
        cwd=str(redis_dir),
    )
    if not wait_redis_up(port):
        raise RuntimeError(f"redis-server did not come up on :{port}")
    return proc


def redis_stop(redis_dir: Path, port: int) -> None:
    subprocess.run(
        [str(redis_dir / "redis-cli.exe"), "-p", str(port), "shutdown", "nosave"],
        capture_output=True,
        timeout=10,
    )
    if not wait_redis_down(port):
        raise RuntimeError(f"redis-server on :{port} did not stop")


def api_start(port: int, redis_port: int, log_handle) -> subprocess.Popen:
    env = dict(os.environ)
    env.update(
        {
            "QP_LOGIN_THROTTLE_MODE": "redis_required",
            "QP_LOGIN_THROTTLE_REDIS_URL": f"redis://127.0.0.1:{redis_port}/0",
            "PYTHONUNBUFFERED": "1",
        }
    )
    proc = subprocess.Popen(
        [
            sys.executable,
            "-m", "uvicorn",
            "quantpilot_api.app:app",
            "--host", "127.0.0.1",
            "--port", str(port),
        ],
        cwd=str(ROOT),
        env=env,
        stdout=log_handle,
        stderr=subprocess.STDOUT,
    )
    if not wait_http_healthy(port):
        raise RuntimeError(f"API instance on :{port} did not become healthy")
    return proc


def stop_proc(proc: subprocess.Popen | None, name: str) -> None:
    if proc is None or proc.poll() is not None:
        return
    proc.terminate()
    try:
        proc.wait(timeout=10)
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.wait(timeout=10)
    log(f"stopped {name}")


def ensure_test_user(username: str, password: str) -> None:
    result = subprocess.run(
        [
            sys.executable,
            str(ROOT / "scripts" / "create_user.py"),
            username,
            password,
            "--role", "VIEWER",
        ],
        capture_output=True,
        text=True,
        cwd=str(ROOT),
        timeout=60,
    )
    out = (result.stdout or "") + (result.stderr or "")
    if result.returncode != 0 and "already exists" not in out:
        raise RuntimeError(f"create_user failed: {out.strip()}")


# ---------------------------------------------------------------- the six tests


def test_1_shared_counts(redis: redis_lib.Redis, port_a: int, port_b: int) -> None:
    log("--- test 1: instance A's counts are visible to instance B ---")
    user = "shareduser"
    statuses = [login(port_a, user, "wrong-password") for _ in range(10)]
    ok_401 = all(s == (401, "INVALID_CREDENTIALS") for s in statuses)
    check("1a ten failed logins via A all 401", ok_401, str(statuses[-1]))

    status_a = login(port_a, user, "wrong-password")
    check(
        "1b 11th attempt via A blocked (429)",
        status_a == (429, "TOO_MANY_ATTEMPTS"),
        str(status_a),
    )
    # B's in-process buckets are EMPTY: only the shared Redis counter can block
    status_b = login(port_b, user, "wrong-password")
    check(
        "1c instance B sees A's counts (429)",
        status_b == (429, "TOO_MANY_ATTEMPTS"),
        str(status_b),
    )
    pair = redis.get(PAIR_KEY.format(ip=CLIENT_IP, username=user))
    check("1d shared pair counter == 10", pair is not None and int(pair) == 10, str(pair))
    ttl = redis.ttl(PAIR_KEY.format(ip=CLIENT_IP, username=user))
    check("1e pair counter has TTL (atomic INCR+EXPIRE)", ttl > 0, f"ttl={ttl}")


def test_3_outage_503(port_a: int, port_b: int) -> None:
    log("--- test 3: Redis stopped -> both instances answer 503 ---")
    for name, port in (("A", port_a), ("B", port_b)):
        status = login(port, "outageuser", "wrong-password")
        check(
            f"3 instance {name} rejects login with 503 (fail-closed)",
            status == (503, "LOGIN_THROTTLE_UNAVAILABLE"),
            str(status),
        )


def test_4_recovery(port_a: int, port_b: int, standdown_wait: float) -> None:
    log("--- test 4: Redis recovers -> shared counting resumes automatically ---")
    status = login(port_a, "recoveruser", "wrong-password")
    check(
        "4a still 503 during the 60 s stand-down window (fail-closed)",
        status == (503, "LOGIN_THROTTLE_UNAVAILABLE"),
        str(status),
    )
    log(f"waiting {standdown_wait:.0f}s for the stand-down window to expire ...")
    time.sleep(standdown_wait)
    statuses = [login(port_a, "recoveruser", "wrong-password") for _ in range(10)]
    check(
        "4b ten failed logins via A all 401 after recovery",
        all(s == (401, "INVALID_CREDENTIALS") for s in statuses),
        str(statuses[-1]),
    )
    status_b = login(port_b, "recoveruser", "wrong-password")
    check(
        "4c instance B resumes shared counting (429)",
        status_b == (429, "TOO_MANY_ATTEMPTS"),
        str(status_b),
    )


def test_5_concurrency(redis: redis_lib.Redis, port_a: int, port_b: int) -> None:
    log("--- test 5: concurrent attempts cannot bypass the limit ---")
    user = "wronguser"
    redis.flushdb()

    def one(port: int) -> tuple[int, str]:
        return login(port, user, "wrong-password")

    with ThreadPoolExecutor(max_workers=30) as pool:
        futures = [pool.submit(one, p) for p in (port_a, port_b) for _ in range(15)]
        results = [f.result() for f in futures]

    n_401 = sum(1 for s in results if s[0] == 401)
    n_429 = sum(1 for s in results if s[0] == 429)
    check("5a every concurrent attempt got 401 or 429", n_401 + n_429 == 30, f"401={n_401} 429={n_429}")
    check("5b threshold reached (>= 10 recorded failures)", n_401 >= 10, f"401={n_401}")

    pair_key = PAIR_KEY.format(ip=CLIENT_IP, username=user)
    shared_count = int(redis.get(pair_key) or 0)
    check(
        "5c atomic Lua: every recorded failure counted EXACTLY once",
        shared_count == n_401,
        f"redis={shared_count} recorded={n_401}",
    )
    ttl = redis.ttl(pair_key)
    check("5d counter TTL always set (no orphaned counters)", ttl > 0, f"ttl={ttl}")

    for name, port in (("A", port_a), ("B", port_b)):
        status = login(port, user, "wrong-password")
        check(
            f"5e instance {name} still blocked after the burst (429)",
            status == (429, "TOO_MANY_ATTEMPTS"),
            str(status),
        )


def test_6_success_clears_pair_only(
    redis: redis_lib.Redis, port_a: int, port_b: int, username: str, password: str
) -> None:
    log("--- test 6: correct login clears ONLY the pair bucket ---")
    redis.flushdb()
    pair_key = PAIR_KEY.format(ip=CLIENT_IP, username=username)
    ip_key = IP_KEY.format(ip=CLIENT_IP)

    for _ in range(3):
        login(port_a, username, "wrong-password")  # r32test failures
    for _ in range(2):
        login(port_a, "sprayuser", "wrong-password")  # same-IP spray
    before_ip = int(redis.get(ip_key) or 0)
    check("6a setup: ip spray bucket holds 5 failures", before_ip == 5, f"ip={before_ip}")

    status, _ = login(port_a, username, password)
    check("6b correct password login succeeds (200)", status == 200, f"status={status}")

    pair_value = redis.get(pair_key)
    check("6c pair bucket (ip, username) cleared", pair_value is None, str(pair_value))
    after_ip = int(redis.get(ip_key) or 0)
    check("6d ip spray bucket retained after success", after_ip == 5, f"ip={after_ip}")

    status, code = login(port_b, username, "wrong-password")
    check(
        "6e same pair not blocked anymore from instance B (401)",
        (status, code) == (401, "INVALID_CREDENTIALS"),
        f"({status}, {code})",
    )


# ---------------------------------------------------------------- main


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--redis-dir",
        default=os.environ.get(
            "R32_REDIS_DIR", str(Path(os.environ.get("TEMP", ".")) / "qp-r32-redis" / "pkg")
        ),
        help="folder holding redis-server.exe / redis-cli.exe",
    )
    parser.add_argument("--redis-port", type=int, default=6399)
    parser.add_argument("--port-a", type=int, default=8101)
    parser.add_argument("--port-b", type=int, default=8102)
    parser.add_argument("--user", default="r32test")
    parser.add_argument("--password", default="R32-Test-Pass!9")
    parser.add_argument("--log-dir", default=str(ROOT / "artifacts" / "r3.2"))
    parser.add_argument("--standdown-wait", type=float, default=65.0)
    args = parser.parse_args()

    redis_dir = Path(args.redis_dir)
    log_dir = Path(args.log_dir)
    log_dir.mkdir(parents=True, exist_ok=True)

    # protocol=2 (RESP2): required against the real Redis 5.x test server
    redis = redis_lib.Redis(host="127.0.0.1", port=args.redis_port, decode_responses=True, protocol=2)
    proc_redis = None
    proc_a: subprocess.Popen | None = None
    proc_b: subprocess.Popen | None = None

    try:
        # ---- preflight ------------------------------------------------
        missing = [
            v
            for v in ("QP_PG_DSN", "QP_API_KEY", "QP_JWT_SECRET")
            if not os.environ.get(v)
        ]
        if not check("preflight: QP_PG_DSN/QP_API_KEY/QP_JWT_SECRET present", not missing, str(missing)):
            return 2
        for exe in ("redis-server.exe", "redis-cli.exe"):
            if not check(f"preflight: {exe} present", (redis_dir / exe).exists(), str(redis_dir)):
                return 2
        try:
            with psycopg.connect(os.environ["QP_PG_DSN"], connect_timeout=5) as conn:
                conn.execute("SELECT 1")
            check("preflight: PostgreSQL reachable (logins write system_events)", True)
        except Exception as exc:
            check("preflight: PostgreSQL reachable (logins write system_events)", False, str(exc))
            return 2

        ensure_test_user(args.user, args.password)
        log(f"test user {args.user!r} ensured")

        # ---- boot: real redis + two real API processes -----------------
        redis_log = open(log_dir / "redis-server.log", "ab")
        api_a_log = open(log_dir / "uvicorn-a.log", "ab")
        api_b_log = open(log_dir / "uvicorn-b.log", "ab")

        log(f"starting redis-server on 127.0.0.1:{args.redis_port} (real binary)")
        proc_redis = redis_start(redis_dir, args.redis_port, redis_log)
        log(f"starting API instance A on 127.0.0.1:{args.port_a} (redis_required)")
        proc_a = api_start(args.port_a, args.redis_port, api_a_log)
        log(f"starting API instance B on 127.0.0.1:{args.port_b} (redis_required)")
        proc_b = api_start(args.port_b, args.redis_port, api_b_log)
        check("boot: real redis + two independent API processes up", True,
              f"redis:{args.redis_port} A:{args.port_a} B:{args.port_b}")

        # ---- 1: shared counting ----------------------------------------
        test_1_shared_counts(redis, args.port_a, args.port_b)

        # ---- 2: counts survive an instance-A restart -------------------
        log("--- test 2: counts survive an instance A restart ---")
        stop_proc(proc_a, "API instance A")
        proc_a = api_start(args.port_a, args.redis_port, api_a_log)
        status = login(args.port_a, "shareduser", "wrong-password")
        check(
            "2 restarted instance A still blocked by the shared counter (429)",
            status == (429, "TOO_MANY_ATTEMPTS"),
            str(status),
        )

        # ---- 3: outage -> both instances 503 ---------------------------
        log("stopping redis-server (shutdown nosave)")
        redis_stop(redis_dir, args.redis_port)
        test_3_outage_503(args.port_a, args.port_b)

        # ---- 4: recovery -> shared counting resumes --------------------
        log("restarting redis-server")
        proc_redis = redis_start(redis_dir, args.redis_port, redis_log)
        test_4_recovery(args.port_a, args.port_b, args.standdown_wait)

        # ---- 5: concurrency -------------------------------------------
        test_5_concurrency(redis, args.port_a, args.port_b)

        # ---- 6: success clears pair bucket only ------------------------
        test_6_success_clears_pair_only(
            redis, args.port_a, args.port_b, args.user, args.password
        )

        return 0 if all(ok for _, ok in RESULTS) else 1
    finally:
        stop_proc(proc_a, "API instance A")
        stop_proc(proc_b, "API instance B")
        if proc_redis is not None and proc_redis.poll() is None:
            try:
                redis_stop(redis_dir, args.redis_port)
            except Exception as exc:  # noqa: BLE001 - best effort cleanup
                log(f"redis stop failed during teardown: {exc}")
        passed = sum(1 for _, ok in RESULTS if ok)
        failed = len(RESULTS) - passed
        log(f"=== SUMMARY: {passed} passed, {failed} failed, {len(RESULTS)} checks ===")
        log_file = log_dir / "multiprocess-test.log"
        log_file.write_text("\n".join(LOG_LINES) + "\n", encoding="utf-8")
        print(f"full log -> {log_file}")


if __name__ == "__main__":
    raise SystemExit(main())
