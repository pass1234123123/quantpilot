"""R3.3 unit tests: atomic reservation + true sliding windows.

P1-01 (concurrency race): the limit check and the slot spend must be
ONE atomic operation in BOTH layers. The R3.2 design checked first and
only incremented after the (slow) password verification, so N concurrent
requests all read "under the limit" and all proceeded - the acceptance
measured 17 password verifications past a limit of 10. These tests hammer
both layers with concurrent reserves and demand the accepted count equal
the configured limit EXACTLY.

P2-01 (window semantics): failures age out one by one (a true sliding
window) in BOTH layers, instead of the whole bucket's TTL resetting on
every new failure (the R3.2 INCR+EXPIRE behavior).

P1-01 lifecycle: a refused attempt (429) reserves nothing; a successful
login releases its OWN spray slot without wiping other users' recorded
failures; an aborted verification returns its slot; the optimistic local
slot is refunded whenever the shared layer refuses.
"""

import os
import threading
import time
import unittest
from unittest.mock import patch

import fakeredis

from rbac_testkit import SECRET_ENV


def _memory_only_env() -> dict:
    return {
        **SECRET_ENV,
        "QP_LOGIN_THROTTLE_MODE": "memory_only",
        "QP_LOGIN_THROTTLE_REDIS_URL": "",
    }


def _redis_required_env(url: str = "redis://shared/0") -> dict:
    return {
        **SECRET_ENV,
        "QP_LOGIN_THROTTLE_MODE": "redis_required",
        "QP_LOGIN_THROTTLE_REDIS_URL": url,
    }


def _fake_redis_env(server):
    from quantpilot_api import login_throttle

    def fake_from_url(url, **_kwargs):
        return fakeredis.FakeStrictRedis(server=server, decode_responses=True)

    return patch.object(login_throttle.redis.Redis, "from_url", fake_from_url)


def _make_throttle(server, *, pair_max=5, ip_max=50, window=900):
    from quantpilot_api.login_throttle import RedisLoginThrottle

    with _fake_redis_env(server):
        return RedisLoginThrottle("redis://shared/0", pair_max, ip_max, window)


def _client(server):
    return fakeredis.FakeStrictRedis(server=server, decode_responses=True)


class LocalConcurrencyTests(unittest.TestCase):
    """P1-01/P2-01 against the always-on local layer (memory_only)."""

    def setUp(self) -> None:
        import quantpilot_api.auth as auth

        self.auth = auth
        auth._login_failures.clear()
        auth._login_failures_ip.clear()
        auth._shared_throttle = None
        auth._warned_memory_only = False

    def tearDown(self) -> None:
        self.auth._login_failures.clear()
        self.auth._login_failures_ip.clear()
        self.auth._shared_throttle = None
        self.auth._warned_memory_only = False

    def test_concurrent_reserves_never_exceed_local_limit(self) -> None:
        # 100 threads race for a limit of 5: exactly 5 must win. The
        # reserve is check-then-append under _login_lock, so the local
        # layer is as atomic as the shared Lua script.
        barrier = threading.Barrier(100)
        accepted: list = []
        lock = threading.Lock()

        def worker() -> None:
            barrier.wait(timeout=15)
            reservation = self.auth.reserve_login_attempt("racer", "9.9.9.9")
            if reservation is not None:
                with lock:
                    accepted.append(reservation)

        with (
            patch.dict(os.environ, _memory_only_env()),
            patch.object(self.auth, "_LOGIN_MAX_FAILURES", 5),
            patch.object(self.auth, "_LOGIN_IP_MAX_FAILURES", 50),
        ):
            threads = [threading.Thread(target=worker) for _ in range(100)]
            for t in threads:
                t.start()
            for t in threads:
                t.join(timeout=60)

        self.assertEqual(len(accepted), 5)
        bucket = self.auth._login_bucket("racer", "9.9.9.9")
        self.assertEqual(len(self.auth._login_failures[bucket]), 5)
        self.assertEqual(len(self.auth._login_failures_ip["9.9.9.9"]), 5)

    def test_pair_failures_age_out_one_by_one(self) -> None:
        # P2-01: fill the pair bucket, then age the OLDEST failure out of
        # the window - the budget must free up immediately. The R3.2
        # TTL-refresh semantics would keep the bucket full until 15
        # minutes pass with NO failure at all.
        with (
            patch.dict(os.environ, _memory_only_env()),
            patch.object(self.auth, "_LOGIN_MAX_FAILURES", 3),
        ):
            for _ in range(3):
                self.assertIsNotNone(self.auth.reserve_login_attempt("bob", "9.9.9.9"))
            self.assertIsNone(self.auth.reserve_login_attempt("bob", "9.9.9.9"))
            bucket = self.auth._login_bucket("bob", "9.9.9.9")
            with self.auth._login_lock:
                t_old, a_old = self.auth._login_failures[bucket][0]
                self.auth._login_failures[bucket][0] = (
                    t_old - self.auth._LOGIN_WINDOW_SECONDS - 1,
                    a_old,
                )
            self.assertIsNotNone(self.auth.reserve_login_attempt("bob", "9.9.9.9"))

    def test_ip_failures_age_out_one_by_one(self) -> None:
        # same sliding-window guarantee for the pure-IP spray bucket
        with (
            patch.dict(os.environ, _memory_only_env()),
            patch.object(self.auth, "_LOGIN_MAX_FAILURES", 100),
            patch.object(self.auth, "_LOGIN_IP_MAX_FAILURES", 3),
        ):
            for i in range(3):
                self.assertIsNotNone(
                    self.auth.reserve_login_attempt(f"user{i}", "9.9.9.9")
                )
            self.assertIsNone(self.auth.reserve_login_attempt("user3", "9.9.9.9"))
            with self.auth._login_lock:
                t_old, a_old = self.auth._login_failures_ip["9.9.9.9"][0]
                self.auth._login_failures_ip["9.9.9.9"][0] = (
                    t_old - self.auth._LOGIN_WINDOW_SECONDS - 1,
                    a_old,
                )
            self.assertIsNotNone(self.auth.reserve_login_attempt("user3", "9.9.9.9"))

    def test_window_boundary_is_exclusive(self) -> None:
        # local semantics keep t while now - t < window; a failure
        # exactly window_seconds old is gone. This must match the Redis
        # ZREMRANGEBYSCORE '-inf', cutoff trim (see SharedWindowSemantics).
        with (
            patch.dict(os.environ, _memory_only_env()),
            patch.object(self.auth, "_LOGIN_MAX_FAILURES", 1),
        ):
            self.assertIsNotNone(self.auth.reserve_login_attempt("carol", "9.9.9.9"))
            self.assertIsNone(self.auth.reserve_login_attempt("carol", "9.9.9.9"))
            bucket = self.auth._login_bucket("carol", "9.9.9.9")
            with self.auth._login_lock:
                t, a = self.auth._login_failures[bucket][0]
                self.auth._login_failures[bucket][0] = (
                    time.time() - self.auth._LOGIN_WINDOW_SECONDS,
                    a,
                )
            self.assertIsNotNone(self.auth.reserve_login_attempt("carol", "9.9.9.9"))


class SharedConcurrencyTests(unittest.TestCase):
    """P1-01 against the shared Redis layer (fakeredis-backed server:
    scripts execute, so the Lua atomicity is really exercised)."""

    def test_concurrent_reserves_never_exceed_shared_limit(self) -> None:
        server = fakeredis.FakeServer()
        throttle = _make_throttle(server, pair_max=5, ip_max=50, window=900)
        barrier = threading.Barrier(100)
        verdicts: list = []
        lock = threading.Lock()

        def worker(i: int) -> None:
            barrier.wait(timeout=15)
            verdict = throttle.reserve("racer", "9.9.9.9", f"att{i}")
            with lock:
                verdicts.append(verdict)

        threads = [threading.Thread(target=worker, args=(i,)) for i in range(100)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=60)

        self.assertEqual(sum(1 for v in verdicts if v is True), 5)
        pair_key, ip_key = throttle._keys("racer", "9.9.9.9")
        client = _client(server)
        self.assertEqual(client.zcard(pair_key), 5)
        self.assertEqual(client.zcard(ip_key), 5)
        # crash-leak backstop: the accepted attempts keep a positive TTL
        self.assertGreater(client.pttl(pair_key), 0)

    def test_two_throttle_instances_share_one_budget(self) -> None:
        server = fakeredis.FakeServer()
        instance_a = _make_throttle(server, pair_max=3, ip_max=50, window=900)
        instance_b = _make_throttle(server, pair_max=3, ip_max=50, window=900)
        for i in range(3):
            self.assertTrue(instance_a.reserve("bob", "9.9.9.9", f"att-a{i}"))
        # instance B sees the budget instance A already spent
        self.assertFalse(instance_b.reserve("bob", "9.9.9.9", "att-b0"))

    def test_refused_attempts_reserve_nothing(self) -> None:
        # a 429 refuses WITHOUT writing anything, so refusals can never
        # extend the lockout (no TTL refresh on refusal either)
        server = fakeredis.FakeServer()
        throttle = _make_throttle(server, pair_max=3, ip_max=50, window=900)
        for i in range(3):
            self.assertTrue(throttle.reserve("eve", "9.9.9.9", f"att{i}"))
        for _ in range(5):
            self.assertFalse(throttle.reserve("eve", "9.9.9.9", "refused"))
        pair_key, ip_key = throttle._keys("eve", "9.9.9.9")
        client = _client(server)
        self.assertEqual(client.zcard(pair_key), 3)
        self.assertEqual(client.zcard(ip_key), 3)

    def test_release_returns_quota(self) -> None:
        # an aborted verification is NOT a failure: its slot comes back
        # in both dimensions, so a retry is possible immediately
        server = fakeredis.FakeServer()
        throttle = _make_throttle(server, pair_max=3, ip_max=50, window=900)
        attempt_ids = []
        for i in range(3):
            attempt_id = f"att{i}"
            self.assertTrue(throttle.reserve("bob", "9.9.9.9", attempt_id))
            attempt_ids.append(attempt_id)
        self.assertFalse(throttle.reserve("bob", "9.9.9.9", "overflow"))
        throttle.release("bob", "9.9.9.9", attempt_ids[0])
        self.assertTrue(throttle.reserve("bob", "9.9.9.9", "retry"))
        pair_key, _ = throttle._keys("bob", "9.9.9.9")
        self.assertEqual(_client(server).zcard(pair_key), 3)

    def test_settle_success_clears_pair_keeps_other_ip_failures(self) -> None:
        # a correct password clears the (ip, username) pair bucket whole
        # and releases ONLY its own spray slot: bob's recorded failure
        # must survive alice's success
        server = fakeredis.FakeServer()
        throttle = _make_throttle(server, pair_max=10, ip_max=50, window=900)
        self.assertTrue(throttle.reserve("bob", "9.9.9.9", "bob-1"))
        self.assertTrue(throttle.reserve("alice", "9.9.9.9", "alice-1"))
        throttle.settle_success("alice", "9.9.9.9", "alice-1")
        client = _client(server)
        alice_pair, alice_ip = throttle._keys("alice", "9.9.9.9")
        bob_pair, _ = throttle._keys("bob", "9.9.9.9")
        # alice's pair bucket is gone: she is not throttled anymore
        self.assertEqual(client.zcard(alice_pair), 0)
        # bob's failure survives alice's success
        self.assertEqual(client.zcard(bob_pair), 1)
        # the shared spray bucket kept exactly bob's attempt id
        self.assertEqual(client.zrange(alice_ip, 0, -1), ["bob-1"])


class SharedWindowSemanticsTests(unittest.TestCase):
    """P2-01: the shared layer is a true sliding window, matching the
    local layer's one-by-one aging (not a TTL that refreshes on every
    failure)."""

    def test_aged_failures_slide_out(self) -> None:
        server = fakeredis.FakeServer()
        throttle = _make_throttle(server, pair_max=10, ip_max=50, window=900)
        now_ms = int(time.time() * 1000)
        pair_key, _ = throttle._keys("oscar", "9.9.9.9")
        client = _client(server)
        # ten failures from BEFORE the window: all aged out
        client.zadd(pair_key, {f"old{i}": now_ms - 900_000 - 1 for i in range(10)})
        # the R3.2 TTL-refresh design would keep this bucket full
        # forever; the sliding window frees the whole budget
        self.assertTrue(throttle.reserve("oscar", "9.9.9.9", "fresh"))
        self.assertEqual(client.zcard(pair_key), 1)

    def test_failures_slide_out_one_by_one(self) -> None:
        server = fakeredis.FakeServer()
        throttle = _make_throttle(server, pair_max=10, ip_max=50, window=900)
        now_ms = int(time.time() * 1000)
        pair_key, _ = throttle._keys("oscar", "9.9.9.9")
        client = _client(server)
        aged_out = {f"old{i}": now_ms - 900_000 - 1 for i in range(5)}
        recent = {f"new{i}": now_ms for i in range(5)}
        client.zadd(pair_key, {**aged_out, **recent})
        # exactly the five aged-out failures leave the window; the five
        # recent ones stay counted, so five slots remain
        for i in range(5):
            self.assertTrue(throttle.reserve("oscar", "9.9.9.9", f"fresh{i}"))
        self.assertEqual(client.zcard(pair_key), 10)
        self.assertFalse(throttle.reserve("oscar", "9.9.9.9", "overflow"))

    def test_window_boundary_matches_local_semantics(self) -> None:
        # ZREMRANGEBYSCORE '-inf', cutoff evicts score <= cutoff while
        # the local layer keeps t while now - t < window: a failure
        # scored exactly at the cutoff is gone in BOTH layers
        server = fakeredis.FakeServer()
        throttle = _make_throttle(server, pair_max=1, ip_max=50, window=900)
        now_ms = int(time.time() * 1000)
        pair_key, _ = throttle._keys("edge", "9.9.9.9")
        client = _client(server)
        client.zadd(pair_key, {"boundary": now_ms - 900_000})
        self.assertTrue(throttle.reserve("edge", "9.9.9.9", "fresh"))
        self.assertEqual(client.zcard(pair_key), 1)


class LayeredReservationTests(unittest.TestCase):
    """P1-01 lifecycle through auth.reserve_login_attempt with a live
    (fakeredis) shared layer in redis_required mode."""

    def setUp(self) -> None:
        import quantpilot_api.auth as auth

        self.auth = auth
        auth._login_failures.clear()
        auth._login_failures_ip.clear()
        auth._shared_throttle = None
        auth._warned_memory_only = False

    def tearDown(self) -> None:
        self.auth._login_failures.clear()
        self.auth._login_failures_ip.clear()
        self.auth._shared_throttle = None
        self.auth._warned_memory_only = False

    def test_shared_budget_full_refunds_local_slot(self) -> None:
        # another instance already spent the shared budget: the local
        # optimistic slot must be refunded before returning the 429
        server = fakeredis.FakeServer()
        with (
            patch.dict(os.environ, _redis_required_env(), clear=True),
            _fake_redis_env(server),
        ):
            throttle = self.auth._throttle()
            pair_key, _ = throttle._keys("nina", "9.9.9.9")
            now_ms = int(time.time() * 1000)
            throttle._client.zadd(
                pair_key, {f"att{i}": now_ms for i in range(10)}
            )
            self.assertIsNone(self.auth.reserve_login_attempt("nina", "9.9.9.9"))
            bucket = self.auth._login_bucket("nina", "9.9.9.9")
            self.assertEqual(self.auth._login_failures.get(bucket, []), [])
            self.assertEqual(self.auth._login_failures_ip.get("9.9.9.9", []), [])

    def test_success_does_not_count_as_failure(self) -> None:
        server = fakeredis.FakeServer()
        with (
            patch.dict(os.environ, _redis_required_env(), clear=True),
            _fake_redis_env(server),
        ):
            reservation = self.auth.reserve_login_attempt("paul", "9.9.9.9")
            self.assertIsNotNone(reservation)
            self.assertTrue(reservation.shared)
            self.auth.settle_login_success(reservation)
            throttle = self.auth._throttle()
            pair_key, ip_key = throttle._keys("paul", "9.9.9.9")
            client = throttle._client
            self.assertEqual(client.zcard(pair_key), 0)
            self.assertEqual(client.zcard(ip_key), 0)

    def test_aborted_attempt_releases_both_dimensions(self) -> None:
        server = fakeredis.FakeServer()
        with (
            patch.dict(os.environ, _redis_required_env(), clear=True),
            _fake_redis_env(server),
        ):
            reservation = self.auth.reserve_login_attempt("olga", "9.9.9.9")
            self.assertIsNotNone(reservation)
            self.auth.release_login_reservation(reservation)
            throttle = self.auth._throttle()
            pair_key, ip_key = throttle._keys("olga", "9.9.9.9")
            client = throttle._client
            self.assertEqual(client.zcard(pair_key), 0)
            self.assertEqual(client.zcard(ip_key), 0)
            bucket = self.auth._login_bucket("olga", "9.9.9.9")
            self.assertEqual(self.auth._login_failures.get(bucket, []), [])
            self.assertEqual(self.auth._login_failures_ip.get("9.9.9.9", []), [])

    def test_refusals_never_extend_the_lockout(self) -> None:
        server = fakeredis.FakeServer()
        with (
            patch.dict(os.environ, _redis_required_env(), clear=True),
            patch.object(self.auth, "_LOGIN_MAX_FAILURES", 3),
            _fake_redis_env(server),
        ):
            for _ in range(3):
                self.assertIsNotNone(
                    self.auth.reserve_login_attempt("tina", "9.9.9.9")
                )
            for _ in range(5):
                self.assertIsNone(self.auth.reserve_login_attempt("tina", "9.9.9.9"))
            throttle = self.auth._throttle()
            pair_key, _ = throttle._keys("tina", "9.9.9.9")
            self.assertEqual(throttle._client.zcard(pair_key), 3)


if __name__ == "__main__":
    unittest.main()
