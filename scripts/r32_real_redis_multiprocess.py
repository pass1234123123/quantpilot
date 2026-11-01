"""R3.3 P1-02: real-Redis multi-process login-throttle verification.

Spawns ONE real redis-server plus TWO independent API processes
(uvicorn, different ports) in redis_required mode and verifies the eight
mandated behaviors over real HTTP:

  1 instance A's failure counts are visible to instance B (shared Redis)
  2 after instance A restarts the counts are still there (Redis-backed,
    not per-process memory)
  3 with Redis stopped BOTH instances answer 503 LOGIN_THROTTLE_UNAVAILABLE
    (fail-closed; no silent degradation to per-instance counting)
  4 after Redis recovers both instances resume shared counting
    automatically (the 60 s stand-down window expires, no restart needed)
  5 concurrent login attempts cannot bypass the limit (R3.3 P1-01:
    120 concurrent wrong-password requests -> the password check runs
    EXACTLY max times; the R3.2 check-then-increment race let 17 run
    past a limit of 10) plus crashed-reservation reclaim via the
    sliding-window trim
  6 a correct login clears ONLY the (ip, username) pair bucket; the
    pure-IP spray bucket keeps every recorded failure
  7 R3.3.1: eight REAL in-flight pendings (spent by the very Lua script
    the API uses, via RedisLoginThrottle straight into the shared
    Redis) all SURVIVE a successful HTTP login; two subsequent wrong
    passwords commit REAL f: records next to them; the pair budget
    then refuses the next reservation exactly (429)
  8 R3.3.1: 120 CONCURRENT mixed correct/wrong-password logins for ONE
    account from ONE IP across BOTH instances - every response is
    200/401/429, no pending is ever stranded (the final pair zset holds
    only committed f: members), and the ip spray zset ends up holding
    EXACTLY as many members as wrong-password 401s

Data-safety hardening (R3.3 P1-02) - the R3.2 version of this script
could destroy data of a Redis it did not own (a foreign server already
listening on the test port: the script connected to it, FLUSHDB'd it
and SHUTDOWN'd it). Now:

  * port preflight: if the target port is already serving ANY listener
    the script aborts BEFORE sending a single command
  * exclusive ephemeral port by default (--redis-port 0 picks a free
    port), so the default path cannot collide at all
  * ownership proof: the server must accept OUR generated password AND
    refuse an unauthenticated PING (NOAUTH). A foreign unprotected
    server fails the second check
  * the child process must still be alive after boot (a port conflict
    makes redis-server exit immediately; the R3.2 version missed that
    and kept talking to whoever owned the port)
  * the freshly started server must be EMPTY (dbsize == 0)
  * no FLUSHDB / FLUSHALL anywhere: only the qp:login:* namespace this
    suite creates is deleted (SCAN + DEL)
  * teardown only terminates the process WE spawned - never a
    redis-cli SHUTDOWN that could hit someone else's server

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
import secrets
import socket
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
NAMESPACE = "qp:login:*"
# pinned to the API's defaults so the assertions below are exact; the
# API processes get these values forced into their environment
PAIR_MAX = 10
IP_MAX = 50
WINDOW_SECONDS = 900


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


# ------------------------------------------------------- port safety (P1-02)


def port_in_use(port: int) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.settimeout(0.5)
        return s.connect_ex(("127.0.0.1", port)) == 0


def assert_port_free(port: int, what: str) -> None:
    """Abort BEFORE sending a single command if the port is taken. The
    R3.2 version skipped this: a foreign Redis on the port meant the
    script happily FLUSHDB'd and SHUTDOWN'd someone else's server."""
    if port_in_use(port):
        raise RuntimeError(
            f"port {port} is already serving another listener - refusing to "
            f"run the {what} test against a service this script did not "
            f"create (R3.3 P1-02: no connection, no cleanup, no shutdown)"
        )


def pick_free_port() -> int:
    """An exclusive ephemeral port the OS guarantees free right now."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


def wait_port_down(port: int, timeout: float = 10.0) -> bool:
    deadline = time.time() + timeout
    while time.time() < deadline:
        if not port_in_use(port):
            return True
        time.sleep(0.3)
    return False


# ---------------------------------------------------------------- process mgmt


def redis_start(redis_dir: Path, port: int, password: str, log_handle) -> subprocess.Popen:
    """Start OUR Redis and PROVE the port answers to it (R3.3 P1-02):

    1. the port must be free before we spawn anything
    2. the child must still be alive after boot (a port conflict makes
       redis-server exit immediately)
    3. our generated password must authenticate
    4. an unauthenticated PING must be refused (NOAUTH) - a foreign
       unprotected server would answer PING instead
    5. the fresh instance must be EMPTY (dbsize == 0)
    """
    assert_port_free(port, "redis")
    proc = subprocess.Popen(
        [
            str(redis_dir / "redis-server.exe"),
            "--port", str(port),
            "--bind", "127.0.0.1",
            "--requirepass", password,
            "--save", "",
            "--appendonly", "no",
        ],
        stdout=log_handle,
        stderr=subprocess.STDOUT,
        cwd=str(redis_dir),
    )
    # ownership check 1: alive (a lost port race makes it exit at once)
    time.sleep(1.0)
    if proc.poll() is not None:
        raise RuntimeError(
            f"redis-server exited immediately with code {proc.returncode} "
            f"(port {port} conflict?) - the test server never came up"
        )
    # ownership checks 2+3: our password works, a stranger's PING doesn't
    authed = redis_lib.Redis(
        host="127.0.0.1", port=port, password=password,
        socket_connect_timeout=0.5, protocol=2,
    )
    deadline = time.time() + 15.0
    while time.time() < deadline:
        if proc.poll() is not None:
            raise RuntimeError("redis-server died during startup")
        try:
            if authed.ping():
                break
        except Exception:
            pass
        time.sleep(0.3)
    else:
        raise RuntimeError(f"redis-server did not answer authenticated PING on :{port}")
    stranger = redis_lib.Redis(
        host="127.0.0.1", port=port,
        socket_connect_timeout=0.5, protocol=2,
    )
    try:
        stranger.ping()
        raise RuntimeError(
            "server answered an UNAUTHENTICATED ping - this is not the "
            "password-protected instance this script spawned; aborting"
        )
    except redis_lib.exceptions.AuthenticationError:
        pass  # expected: NOAUTH proves requirepass is armed
    if authed.dbsize() != 0:
        raise RuntimeError(
            "freshly started redis-server is not empty - refusing to run "
            "against a database holding foreign keys"
        )
    return proc


def redis_stop(proc: subprocess.Popen, port: int) -> None:
    """Only the process WE spawned is terminated (R3.3 P1-02). The R3.2
    version sent `redis-cli shutdown nosave` to whatever owned the port -
    if that was a foreign server it got killed."""
    if proc.poll() is None:
        proc.terminate()
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait(timeout=10)
    if not wait_port_down(port):
        raise RuntimeError(f"redis-server on :{port} did not stop")


def clear_login_namespace(redis: redis_lib.Redis) -> int:
    """Delete ONLY the qp:login:* keys this suite creates (R3.3 P1-02).
    The R3.2 version called FLUSHDB, which wipes every key of whatever
    database the connection points at - including keys this test never
    owned."""
    keys = list(redis.scan_iter(match=NAMESPACE, count=100))
    if keys:
        redis.delete(*keys)
    return len(keys)


def api_start(port: int, redis_port: int, redis_password: str, log_handle) -> subprocess.Popen:
    env = dict(os.environ)
    env.update(
        {
            "QP_LOGIN_THROTTLE_MODE": "redis_required",
            "QP_LOGIN_THROTTLE_REDIS_URL": (
                f"redis://:{redis_password}@127.0.0.1:{redis_port}/0"
            ),
            # pin the limits so the exact-count assertions below hold
            "QP_LOGIN_MAX_FAILURES": str(PAIR_MAX),
            "QP_LOGIN_IP_MAX_FAILURES": str(IP_MAX),
            "QP_LOGIN_WINDOW_SECONDS": str(WINDOW_SECONDS),
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
    statuses = [login(port_a, user, "wrong-password") for _ in range(PAIR_MAX)]
    ok_401 = all(s == (401, "INVALID_CREDENTIALS") for s in statuses)
    check("1a ten failed logins via A all 401", ok_401, str(statuses[-1]))

    status_a = login(port_a, user, "wrong-password")
    check(
        "1b 11th attempt via A blocked (429)",
        status_a == (429, "TOO_MANY_ATTEMPTS"),
        str(status_a),
    )
    # B's in-process buckets are EMPTY: only the shared Redis budget can block
    status_b = login(port_b, user, "wrong-password")
    check(
        "1c instance B sees A's counts (429)",
        status_b == (429, "TOO_MANY_ATTEMPTS"),
        str(status_b),
    )
    pair_key = PAIR_KEY.format(ip=CLIENT_IP, username=user)
    zcard = redis.zcard(pair_key)
    check("1d shared pair zset == 10 (atomic reservations)", zcard == PAIR_MAX, f"zcard={zcard}")
    ttl = redis.ttl(pair_key)
    check("1e pair zset has TTL (crash-leak backstop)", ttl > 0, f"ttl={ttl}")


def test_3_outage_503(port_a: int, port_b: int) -> None:
    log("--- test 3: Redis stopped -> both instances answer 503 ---")
    for name, port in (("A", port_a), ("B", port_b)):
        status = login(port, "outageuser", "wrong-password")
        check(
            f"3 instance {name} rejects login with 503 (fail-closed)",
            status == (503, "LOGIN_THROTTLE_UNAVAILABLE"),
            str(status),
        )


def test_4_recovery(redis: redis_lib.Redis, port_a: int, port_b: int, standdown_wait: float) -> None:
    log("--- test 4: Redis recovers -> shared counting resumes automatically ---")
    status = login(port_a, "recoveruser", "wrong-password")
    check(
        "4a still 503 during the 60 s stand-down window (fail-closed)",
        status == (503, "LOGIN_THROTTLE_UNAVAILABLE"),
        str(status),
    )
    log(f"waiting {standdown_wait:.0f}s for the stand-down window to expire ...")
    time.sleep(standdown_wait)
    statuses = [login(port_a, "recoveruser", "wrong-password") for _ in range(PAIR_MAX)]
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
    zcard = redis.zcard(PAIR_KEY.format(ip=CLIENT_IP, username="recoveruser"))
    check("4d shared pair zset == 10 after recovery", zcard == PAIR_MAX, f"zcard={zcard}")


def test_5_concurrency(redis: redis_lib.Redis, port_a: int, port_b: int) -> None:
    log("--- test 5: 120 concurrent attempts cannot bypass the limit ---")
    user = "wronguser"
    cleared = clear_login_namespace(redis)
    log(f"cleared {cleared} qp:login:* keys from earlier tests")

    def one(port: int) -> tuple[int, str]:
        return login(port, user, "wrong-password")

    # R3.3 P1-01 acceptance: two independent API processes, MORE than
    # 100 concurrent wrong-password requests
    total = 120
    with ThreadPoolExecutor(max_workers=40) as pool:
        futures = [pool.submit(one, p) for p in (port_a, port_b) for _ in range(total // 2)]
        results = [f.result() for f in futures]

    n_401 = sum(1 for s in results if s[0] == 401)
    n_429 = sum(1 for s in results if s[0] == 429)
    others = [s for s in results if s[0] not in (401, 429)]
    check(
        "5a every concurrent attempt got 401 or 429",
        n_401 + n_429 == total and not others,
        f"401={n_401} 429={n_429} other={others[:2]}",
    )
    # the R3.2 race let 17 password verifications run past a limit of
    # 10; the atomic reservation must run the check EXACTLY max times
    check(
        "5b password verifications EXACTLY at the limit (no race)",
        n_401 == PAIR_MAX,
        f"401={n_401} (limit={PAIR_MAX})",
    )
    check(
        "5c everything else refused 429",
        n_429 == total - PAIR_MAX,
        f"429={n_429}",
    )
    pair_key = PAIR_KEY.format(ip=CLIENT_IP, username=user)
    ip_key = IP_KEY.format(ip=CLIENT_IP)
    zc_pair = redis.zcard(pair_key)
    check(
        "5d shared pair zset == limit (atomic reservation)",
        zc_pair == PAIR_MAX,
        f"zcard={zc_pair}",
    )
    zc_ip = redis.zcard(ip_key)
    check(
        "5e shared ip zset == accepted attempts",
        zc_ip == PAIR_MAX,
        f"zcard={zc_ip}",
    )
    # P1-01 leak handling, on an ISOLATED user: burst user's local-layer
    # buckets inside instance A are also full (A's requests reached the
    # shared layer first and won the whole budget), so the leak test uses
    # a fresh username whose LOCAL buckets are empty in both processes -
    # only the shared layer decides. Ten in-window reservations are
    # planted (as if ten attempts just happened; one of them "leaked"
    # when a process crashed mid-verification - the zset cannot tell
    # them apart, which is exactly the point):
    leak_user = "leakuser"
    leak_pair = PAIR_KEY.format(ip=CLIENT_IP, username=leak_user)
    now_ms = int(time.time() * 1000)
    redis.zadd(leak_pair, {f"p:leak-att-{i}": now_ms for i in range(PAIR_MAX)})
    status = login(port_a, leak_user, "wrong-password")
    check(
        "5f in-window reservations (incl. a leaked one) block login (429)",
        status == (429, "TOO_MANY_ATTEMPTS"),
        str(status),
    )
    # simulate 15 minutes passing: every reservation - leaked ones
    # included - slides out of the window together, so leaked quota is
    # reclaimed by the window trim and never permanently lost
    aged = now_ms - WINDOW_SECONDS * 1000 - 1000
    for member in redis.zrange(leak_pair, 0, -1):
        redis.zadd(leak_pair, {member: aged})
    status = login(port_a, leak_user, "wrong-password")
    check(
        "5g aged-out reservations reclaimed by window trim (401)",
        status == (401, "INVALID_CREDENTIALS"),
        str(status),
    )
    zc_after = redis.zcard(leak_pair)
    check(
        "5h zset rebuilt from one fresh attempt (no permanent loss)",
        zc_after == 1,
        f"zcard={zc_after}",
    )


def test_6_success_clears_pair_only(
    redis: redis_lib.Redis, port_a: int, port_b: int, username: str, password: str
) -> None:
    log("--- test 6: correct login clears ONLY the pair bucket ---")
    cleared = clear_login_namespace(redis)
    log(f"cleared {cleared} qp:login:* keys from earlier tests")
    pair_key = PAIR_KEY.format(ip=CLIENT_IP, username=username)
    ip_key = IP_KEY.format(ip=CLIENT_IP)

    for _ in range(3):
        login(port_a, username, "wrong-password")  # r32test failures
    for _ in range(2):
        login(port_a, "sprayuser", "wrong-password")  # same-IP spray
    before_ip = redis.zcard(ip_key)
    check("6a setup: ip spray zset holds 5 failures", before_ip == 5, f"ip={before_ip}")

    status, _ = login(port_a, username, password)
    check("6b correct password login succeeds (200)", status == 200, f"status={status}")

    zc_pair = redis.zcard(pair_key)
    check("6c pair bucket (ip, username) cleared", zc_pair == 0, f"zcard={zc_pair}")
    after_ip = redis.zcard(ip_key)
    # the successful attempt released ONLY its own spray slot; the five
    # recorded failures (3 + 2) stay counted
    check("6d ip spray zset retained after success", after_ip == 5, f"ip={after_ip}")

    status, code = login(port_b, username, "wrong-password")
    check(
        "6e same pair not blocked anymore from instance B (401)",
        (status, code) == (401, "INVALID_CREDENTIALS"),
        f"({status}, {code})",
    )


def test_7_pendings_survive_success(
    redis: redis_lib.Redis,
    port_a: int,
    port_b: int,
    redis_url: str,
    username: str,
    password: str,
) -> None:
    """R3.3.1 acceptance race (the T1-T6 scenario), deterministic, over
    real HTTP + real Redis: a successful login must keep OTHER requests'
    in-flight pendings, and a failure that lands later must still be
    recorded next to them."""
    log("--- test 7 (R3.3.1): a success keeps other requests' pendings ---")
    cleared = clear_login_namespace(redis)
    log(f"cleared {cleared} qp:login:* keys from earlier tests")
    pair_key = PAIR_KEY.format(ip=CLIENT_IP, username=username)
    ip_key = IP_KEY.format(ip=CLIENT_IP)

    # eight REAL in-flight reservations from "other API processes":
    # spent by the very Lua script the API uses, straight into the
    # shared Redis both instances talk to (two slots stay free)
    from quantpilot_api.login_throttle import RedisLoginThrottle

    preset = RedisLoginThrottle(redis_url, PAIR_MAX, IP_MAX, WINDOW_SECONDS)
    for i in range(PAIR_MAX - 2):
        ok = preset.reserve(username, CLIENT_IP, f"preset-{i}")
        if not check(f"7a preset pending {i} reserved", ok is True, str(ok)):
            return
    score_before = redis.zscore(pair_key, "p:preset-0")

    # a correct HTTP login lands while those eight are mid-verification
    status, _code = login(port_a, username, password)
    check(
        "7b correct login 200 amid eight in-flight pendings",
        status == 200,
        f"status={status}",
    )

    members = redis.zrange(pair_key, 0, -1)
    survived = sorted(m for m in members if m.startswith("p:preset-"))
    check(
        "7c ALL eight foreign pendings SURVIVED the success",
        survived == sorted(f"p:preset-{i}" for i in range(PAIR_MAX - 2)),
        f"members={members}",
    )
    check(
        "7d the survivors' window scores are untouched",
        redis.zscore(pair_key, "p:preset-0") == score_before,
        f"before={score_before} after={redis.zscore(pair_key, 'p:preset-0')}",
    )

    # the two open slots: two wrong passwords commit REAL f: records
    status, code = login(port_b, username, "definitely-wrong")
    check(
        "7e wrong password #1 verified (401)",
        (status, code) == (401, "INVALID_CREDENTIALS"),
        f"({status}, {code})",
    )
    members = redis.zrange(pair_key, 0, -1)
    check(
        "7f failure #1 committed next to the pendings (f: member)",
        len(members) == PAIR_MAX - 1
        and sum(1 for m in members if m.startswith("f:")) == 1,
        f"members={members}",
    )
    status, code = login(port_a, username, "definitely-wrong")
    check(
        "7g wrong password #2 verified (401)",
        (status, code) == (401, "INVALID_CREDENTIALS"),
        f"({status}, {code})",
    )
    # the pair budget is now exactly full: the NEXT reservation - before
    # any password verification - must be refused
    status, code = login(port_a, username, "definitely-wrong")
    check(
        "7h next reservation refused exactly at the limit (429)",
        (status, code) == (429, "TOO_MANY_ATTEMPTS"),
        f"({status}, {code})",
    )
    members = redis.zrange(pair_key, 0, -1)
    check(
        "7i exact accounting: 8 pendings + 2 committed failures",
        len(members) == PAIR_MAX
        and sum(1 for m in members if m.startswith("p:preset-")) == PAIR_MAX - 2
        and sum(1 for m in members if m.startswith("f:")) == 2,
        f"members={members}",
    )
    zc_ip = redis.zcard(ip_key)
    check(
        "7j ip spray zset == 10 (the success returned only its own slot)",
        zc_ip == PAIR_MAX,
        f"ip={zc_ip}",
    )


def test_8_mixed_storm(
    redis: redis_lib.Redis, port_a: int, port_b: int, username: str, password: str
) -> None:
    """R3.3.1: >=100 CONCURRENT mixed correct/wrong-password logins for
    ONE account from ONE IP across BOTH instances (same account, same
    IP). Successes legitimately clear committed history and re-open the
    budget, so the deterministic verification-count bound lives in test
    5; what a mixed storm can and must prove are the structural
    invariants: every response is 200/401/429, no pending is ever
    stranded, no record is invented, and the spray accounting is
    EXACT under full concurrency."""
    log("--- test 8 (R3.3.1): 120 mixed correct/wrong concurrent logins ---")
    cleared = clear_login_namespace(redis)
    log(f"cleared {cleared} qp:login:* keys from earlier tests")
    pair_key = PAIR_KEY.format(ip=CLIENT_IP, username=username)
    ip_key = IP_KEY.format(ip=CLIENT_IP)

    total = 120

    def one(i: int) -> int:
        port = port_a if i % 2 == 0 else port_b
        pwd = password if i % 4 in (0, 1) else "definitely-wrong"
        status, _code = login(port, username, pwd)
        return status

    with ThreadPoolExecutor(max_workers=total) as pool:
        futures = [pool.submit(one, i) for i in range(total)]
        statuses = [f.result() for f in futures]

    n_200 = statuses.count(200)
    n_401 = statuses.count(401)
    n_429 = statuses.count(429)
    others = [s for s in statuses if s not in (200, 401, 429)]
    check(
        "8a every mixed attempt answered 200/401/429 (no 5xx)",
        n_200 + n_401 + n_429 == total and not others,
        f"200={n_200} 401={n_401} 429={n_429} other={others[:3]}",
    )
    check(
        "8b the storm was really mixed: both verdicts occurred",
        n_200 >= 1 and n_401 >= 1,
        f"200={n_200} 401={n_401}",
    )
    members = redis.zrange(pair_key, 0, -1)
    check(
        "8c no pending stranded: every survivor is a committed f: record",
        all(m.startswith("f:") for m in members),
        f"members={members}",
    )
    check(
        "8d pair zset stayed within the budget ceiling",
        len(members) <= PAIR_MAX,
        f"zcard={len(members)}",
    )
    check(
        "8e no invented records: pair holds at most the 401 commits",
        len(members) <= n_401,
        f"zcard={len(members)} 401={n_401}",
    )
    zc_ip = redis.zcard(ip_key)
    # every 401 kept its spray member; every 200 returned its own slot -
    # so the spray zset holds EXACTLY as many members as verified
    # wrong-password attempts, under full concurrency
    check(
        "8f ip spray accounting exact: zcard == verified 401s",
        zc_ip == n_401,
        f"ip={zc_ip} 401={n_401}",
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
    parser.add_argument(
        "--redis-port",
        type=int,
        default=0,
        help="exclusive port for the test redis-server (default 0 = pick "
        "a free ephemeral port; a busy port aborts the run)",
    )
    parser.add_argument("--port-a", type=int, default=8101)
    parser.add_argument("--port-b", type=int, default=8102)
    parser.add_argument("--user", default="r32test")
    parser.add_argument("--password", default="R32-Test-Pass!9")
    parser.add_argument("--log-dir", default=str(ROOT / "artifacts" / "r3.3"))
    parser.add_argument("--standdown-wait", type=float, default=65.0)
    args = parser.parse_args()

    redis_dir = Path(args.redis_dir)
    log_dir = Path(args.log_dir)
    log_dir.mkdir(parents=True, exist_ok=True)

    # R3.3 P1-02: a random per-run password proves the server on the
    # port is OURS (and arms the NOAUTH ownership check)
    redis_password = secrets.token_hex(16)
    redis_port = args.redis_port or pick_free_port()

    # protocol=2 (RESP2): required against the real Redis 5.x test server
    redis = redis_lib.Redis(
        host="127.0.0.1", port=redis_port, password=redis_password,
        decode_responses=True, protocol=2,
    )
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
        # port safety FIRST (R3.3 P1-02): it needs no external services,
        # and a busy port must abort the run before ANYTHING is started
        # or connected - the port-conflict acceptance test depends on
        # this running ahead of the PostgreSQL preflight
        if args.redis_port:
            # explicit port: must be free RIGHT NOW, or abort without
            # sending a single command to whoever owns it
            try:
                assert_port_free(args.redis_port, "redis")
            except RuntimeError as exc:
                check("preflight: requested redis port free", False, str(exc))
                return 2
            check("preflight: requested redis port free", True, f"port={args.redis_port}")
        else:
            check(
                "preflight: exclusive ephemeral redis port picked",
                True,
                f"port={redis_port}",
            )
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

        log(
            f"starting redis-server on 127.0.0.1:{redis_port} "
            f"(real binary, password-protected, ownership-checked)"
        )
        proc_redis = redis_start(redis_dir, redis_port, redis_password, redis_log)
        log(f"starting API instance A on 127.0.0.1:{args.port_a} (redis_required)")
        proc_a = api_start(args.port_a, redis_port, redis_password, api_a_log)
        log(f"starting API instance B on 127.0.0.1:{args.port_b} (redis_required)")
        proc_b = api_start(args.port_b, redis_port, redis_password, api_b_log)
        check(
            "boot: real redis + two independent API processes up",
            True,
            f"redis:{redis_port} A:{args.port_a} B:{args.port_b}",
        )

        # ---- 1: shared counting ----------------------------------------
        test_1_shared_counts(redis, args.port_a, args.port_b)

        # ---- 2: counts survive an instance-A restart -------------------
        log("--- test 2: counts survive an instance A restart ---")
        stop_proc(proc_a, "API instance A")
        proc_a = api_start(args.port_a, redis_port, redis_password, api_a_log)
        status = login(args.port_a, "shareduser", "wrong-password")
        check(
            "2 restarted instance A still blocked by the shared counter (429)",
            status == (429, "TOO_MANY_ATTEMPTS"),
            str(status),
        )

        # ---- 3: outage -> both instances 503 ---------------------------
        log("stopping redis-server (terminate - only the process WE spawned)")
        redis_stop(proc_redis, redis_port)
        test_3_outage_503(args.port_a, args.port_b)

        # ---- 4: recovery -> shared counting resumes --------------------
        log("restarting redis-server (fresh password-verified instance)")
        proc_redis = redis_start(redis_dir, redis_port, redis_password, redis_log)
        test_4_recovery(redis, args.port_a, args.port_b, args.standdown_wait)

        # ---- 5: concurrency -------------------------------------------
        test_5_concurrency(redis, args.port_a, args.port_b)

        # ---- 6: success clears pair bucket only ------------------------
        test_6_success_clears_pair_only(
            redis, args.port_a, args.port_b, args.user, args.password
        )

        # ---- 7 (R3.3.1): pendings survive a success (deterministic) ----
        test_7_pendings_survive_success(
            redis,
            args.port_a,
            args.port_b,
            f"redis://:{redis_password}@127.0.0.1:{redis_port}/0",
            args.user,
            args.password,
        )

        # ---- 8 (R3.3.1): 120 mixed concurrent logins, both instances ---
        test_8_mixed_storm(
            redis, args.port_a, args.port_b, args.user, args.password
        )

        return 0 if all(ok for _, ok in RESULTS) else 1
    finally:
        stop_proc(proc_a, "API instance A")
        stop_proc(proc_b, "API instance B")
        if proc_redis is not None and proc_redis.poll() is None:
            try:
                # terminate OUR child only - never a broadcast shutdown
                redis_stop(proc_redis, redis_port)
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
