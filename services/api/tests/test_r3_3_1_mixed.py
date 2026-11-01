"""R3.3.1 unit tests: the success-vs-failure concurrency boundary.

The R3.3 acceptance found that settle_success DELETED the whole pair
key: a request that was still verifying (a PENDING reservation) lost
its slot to another request's successful login, so the pending
request's later password failure was never recorded - the shared
budget under-counted, the attacker-favourable direction. R3.3.1 splits
pair members into "p:{id}" (pending, still verifying) and "f:{id}"
(committed failure):

  * reserve        -> one atomic slot, recorded as p:{id} in the pair
                      zset + plain id in the ip spray zset
  * wrong password -> commit_failure: one atomic Lua rename
                      p:{id} -> f:{id}, score preserved; RAISES
                      (fail-closed) when Redis cannot record it
  * right password -> settle_success: one atomic Lua script that
                      clears ONLY the f:* failure history plus the
                      attempt's OWN pending; OTHER requests' pendings
                      survive; own spray slot returned
  * abort          -> release: removes ONLY its own p:{id}

Mandated scenarios (R3.3.1 acceptance): mixed correct/wrong concurrent
logins, two instances sharing one account, success must not delete
other requests' pending reservations, failed requests must leave
failure records, release must never touch other attempt_ids,
redis_required stays fail-closed, and the accepted sliding-window +
pair/ip split semantics are preserved. Real-Redis dual-process proof
lives in scripts/r32_real_redis_multiprocess.py (test 7).
"""

import os
import threading
import time
import unittest
from unittest.mock import patch

import fakeredis
from fastapi.testclient import TestClient

from rbac_testkit import SECRET_ENV

PAIR_KEY = "qp:login:pair:{ip}|{username}"
IP_KEY = "qp:login:ip:{ip}"


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


def _make_throttle(server, *, pair_max=10, ip_max=200, window=900):
    from quantpilot_api.login_throttle import RedisLoginThrottle

    with _fake_redis_env(server):
        return RedisLoginThrottle("redis://shared/0", pair_max, ip_max, window)


def _client(server):
    return fakeredis.FakeStrictRedis(server=server, decode_responses=True)


def _pair(username: str, ip: str = "9.9.9.9") -> str:
    return PAIR_KEY.format(ip=ip, username=username)


def _ip(ip: str = "9.9.9.9") -> str:
    return IP_KEY.format(ip=ip)


class SharedSuccessKeepsOtherPendingsTests(unittest.TestCase):
    """THE R3.3.1 acceptance race, at the shared (Redis) layer.

    T1 request A reserves; T2 request B reserves; T3 A's password
    verifies correct; T4 A settles success; T5 B's password verifies
    WRONG; T6 B's failure must still be recorded. The R3.3 code lost
    B's record at T4 (whole-key DEL); the fix must keep it."""

    def test_success_keeps_other_pending_then_failure_recorded(self) -> None:
        server = fakeredis.FakeServer()
        th = _make_throttle(server)
        client = _client(server)
        self.assertTrue(th.reserve("vic", "9.9.9.9", "att-a"))
        self.assertTrue(th.reserve("vic", "9.9.9.9", "att-b"))
        # T4: A logs in successfully while B is still verifying
        th.settle_success("vic", "9.9.9.9", "att-a")
        # B's pending survived the success (the R3.3 code deleted it)
        self.assertEqual(client.zrange(_pair("vic"), 0, -1), ["p:att-b"])
        # T5+T6: B's password is wrong -> the failure is recorded
        th.commit_failure("vic", "9.9.9.9", "att-b")
        self.assertEqual(client.zrange(_pair("vic"), 0, -1), ["f:att-b"])

    def test_success_clears_committed_history_but_keeps_pendings(self) -> None:
        server = fakeredis.FakeServer()
        th = _make_throttle(server)
        client = _client(server)
        # one COMMITTED failure from the past ...
        self.assertTrue(th.reserve("vic", "9.9.9.9", "old-fail"))
        th.commit_failure("vic", "9.9.9.9", "old-fail")
        # ... two requests still verifying ...
        self.assertTrue(th.reserve("vic", "9.9.9.9", "pend-1"))
        self.assertTrue(th.reserve("vic", "9.9.9.9", "pend-2"))
        # ... and a successful login for pend-2
        th.settle_success("vic", "9.9.9.9", "pend-2")
        # accepted R3.1 semantics: the committed failure history is
        # cleared; R3.3.1: pend-1 (another request's IN-FLIGHT slot)
        # must survive, and only pend-2's own pending is gone
        self.assertEqual(client.zrange(_pair("vic"), 0, -1), ["p:pend-1"])

    def test_ip_spray_bucket_untouched_by_commit(self) -> None:
        server = fakeredis.FakeServer()
        th = _make_throttle(server)
        client = _client(server)
        self.assertTrue(th.reserve("vic", "9.9.9.9", "att-a"))
        th.commit_failure("vic", "9.9.9.9", "att-a")
        # the spray member keeps the plain attempt id and stays counted
        self.assertEqual(client.zrange(_ip(), 0, -1), ["att-a"])

    def test_commit_preserves_attempt_time_score(self) -> None:
        server = fakeredis.FakeServer()
        th = _make_throttle(server)
        client = _client(server)
        self.assertTrue(th.reserve("vic", "9.9.9.9", "att-a"))
        score_before = client.zscore(_pair("vic"), "p:att-a")
        time.sleep(0.01)  # the commit must NOT re-anchor at completion time
        th.commit_failure("vic", "9.9.9.9", "att-a")
        self.assertEqual(client.zscore(_pair("vic"), "f:att-a"), score_before)

    def test_commit_is_idempotent(self) -> None:
        server = fakeredis.FakeServer()
        th = _make_throttle(server)
        client = _client(server)
        self.assertTrue(th.reserve("vic", "9.9.9.9", "att-a"))
        th.commit_failure("vic", "9.9.9.9", "att-a")
        th.commit_failure("vic", "9.9.9.9", "att-a")  # double-commit: no dup
        self.assertEqual(client.zrange(_pair("vic"), 0, -1), ["f:att-a"])

    def test_release_removes_only_own_pending(self) -> None:
        server = fakeredis.FakeServer()
        th = _make_throttle(server)
        client = _client(server)
        self.assertTrue(th.reserve("vic", "9.9.9.9", "att-a"))
        th.commit_failure("vic", "9.9.9.9", "att-a")  # a committed failure
        self.assertTrue(th.reserve("vic", "9.9.9.9", "att-b"))
        # an abort can never remove another request's committed record
        th.release("vic", "9.9.9.9", "att-b")
        self.assertEqual(client.zrange(_pair("vic"), 0, -1), ["f:att-a"])
        # ... and returns only its own spray slot
        self.assertEqual(client.zrange(_ip(), 0, -1), ["att-a"])

    def test_committed_failure_ages_out_with_the_window(self) -> None:
        server = fakeredis.FakeServer()
        th = _make_throttle(server, pair_max=2)
        client = _client(server)
        self.assertTrue(th.reserve("vic", "9.9.9.9", "att-a"))
        th.commit_failure("vic", "9.9.9.9", "att-a")
        # slide the committed record out of the 900 s window ...
        aged = time.time() * 1000 - 900_000 - 1_000
        client.zadd(_pair("vic"), {"f:att-a": aged})
        client.zadd(_ip(), {"att-a": aged})
        # ... and the budget is reclaimable again (window semantics kept)
        self.assertTrue(th.reserve("vic", "9.9.9.9", "att-new"))
        self.assertEqual(client.zrange(_pair("vic"), 0, -1), ["p:att-new"])

    def test_success_reopens_budget_for_immediate_failures(self) -> None:
        # "successful login followed by an immediate batch of wrong
        # passwords" - the cleared history and the fresh reservations
        # must follow ONE consistent rule set
        server = fakeredis.FakeServer()
        th = _make_throttle(server, pair_max=3)
        for i in range(2):
            self.assertTrue(th.reserve("vic", "9.9.9.9", f"old-{i}"))
            th.commit_failure("vic", "9.9.9.9", f"old-{i}")
        self.assertTrue(th.reserve("vic", "9.9.9.9", "ok-att"))
        th.settle_success("vic", "9.9.9.9", "ok-att")
        # history cleared -> an immediate wrong batch fills the budget
        for i in range(3):
            self.assertTrue(th.reserve("vic", "9.9.9.9", f"new-{i}"))
        self.assertFalse(th.reserve("vic", "9.9.9.9", "new-over"))


class LocalMirrorTests(unittest.TestCase):
    """The same boundary in the always-on local (in-process) layer."""

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

    def test_local_success_keeps_other_pending_then_failure_recorded(self) -> None:
        with patch.dict(os.environ, _memory_only_env()):
            a = self.auth.reserve_login_attempt("vic", "9.9.9.9")
            b = self.auth.reserve_login_attempt("vic", "9.9.9.9")
            self.assertIsNotNone(a)
            self.assertIsNotNone(b)
            self.auth.settle_login_success(a)  # A succeeds while B verifies
            bucket = self.auth._login_bucket("vic", "9.9.9.9")
            # B's pending survived A's success ...
            entries = self.auth._login_failures[bucket]
            self.assertEqual(len(entries), 1)
            t_b, a_id, state = entries[0]
            self.assertEqual((a_id, state), (b.attempt_id, "p"))
            # ... and B's wrong password is committed with the SAME
            # timestamp (window anchors at attempt time, like the Redis
            # score-preserving commit)
            self.auth.settle_login_failure(b)
            self.assertEqual(
                self.auth._login_failures[bucket], [(t_b, b.attempt_id, "f")]
            )

    def test_local_success_clears_committed_failures_only(self) -> None:
        with patch.dict(os.environ, _memory_only_env()):
            f1 = self.auth.reserve_login_attempt("vic", "9.9.9.9")
            self.auth.settle_login_failure(f1)
            f2 = self.auth.reserve_login_attempt("vic", "9.9.9.9")
            self.auth.settle_login_failure(f2)
            p3 = self.auth.reserve_login_attempt("vic", "9.9.9.9")
            self.auth.settle_login_success(p3)
            bucket = self.auth._login_bucket("vic", "9.9.9.9")
            # both COMMITTED failures were cleared by the success; the
            # pending of p3 itself is gone too -> bucket empty
            self.assertEqual(self.auth._login_failures[bucket], [])

    def test_local_release_removes_only_own_entry(self) -> None:
        with patch.dict(os.environ, _memory_only_env()):
            a = self.auth.reserve_login_attempt("vic", "9.9.9.9")
            self.auth.settle_login_failure(a)  # committed failure of A
            b = self.auth.reserve_login_attempt("vic", "9.9.9.9")
            self.auth.release_login_reservation(b)  # B aborted
            bucket = self.auth._login_bucket("vic", "9.9.9.9")
            entries = self.auth._login_failures[bucket]
            self.assertEqual(len(entries), 1)
            self.assertEqual((entries[0][1], entries[0][2]), (a.attempt_id, "f"))


class TwoInstanceMixedTests(unittest.TestCase):
    """Two API instances, one shared budget, mixed outcomes - exact
    accounting (R3.3.1 acceptance: cross-instance mixed logins must not
    exceed the policy through success-triggered clears)."""

    def test_cross_instance_mixed_accounting_is_exact(self) -> None:
        server = fakeredis.FakeServer()
        ta = _make_throttle(server, pair_max=10)
        tb = _make_throttle(server, pair_max=10)
        client = _client(server)
        user, ip = "mixuser", "9.9.9.9"
        # instance A takes 5 slots, instance B takes 5 -> budget full
        for i in range(5):
            self.assertTrue(ta.reserve(user, ip, f"a{i}"))
            self.assertTrue(tb.reserve(user, ip, f"b{i}"))
        self.assertFalse(ta.reserve(user, ip, "a-x"))
        self.assertFalse(tb.reserve(user, ip, "b-x"))
        # b2 logs in SUCCESSFULLY from instance B: its own pending (and
        # any committed history - none yet) leaves; other pendings stay
        tb.settle_success(user, ip, "b2")
        # exactly ONE slot reopened: A grabs it and fails the password
        self.assertTrue(ta.reserve(user, ip, "a-new"))
        ta.commit_failure(user, ip, "a-new")
        # exact accounting: 5 A-pendings + 4 B-pendings + 1 committed
        # failure (f:a-new; a2 still pending; b2 settled and gone) -
        # nothing extra slipped in through the success
        expected = sorted(
            ["p:a0", "p:a1", "p:a2", "p:a3", "p:a4", "f:a-new"]
            + [f"p:b{i}" for i in (0, 1, 3, 4)]
        )
        self.assertEqual(sorted(client.zrange(_pair(user), 0, -1)), expected)
        # and the budget is full again for BOTH instances
        self.assertFalse(ta.reserve(user, ip, "a-over"))
        self.assertFalse(tb.reserve(user, ip, "b-over"))

    def test_mixed_concurrent_storm_never_invents_or_keeps_wrong_records(self):
        """120 concurrent mixed attempts across two instances. The
        final state must contain ONLY committed failures: released and
        settled attempt ids are gone entirely, refused ids were never
        written, no pending survives, and the zset never exceeds the
        pair budget."""
        server = fakeredis.FakeServer()
        ta = _make_throttle(server, pair_max=10, ip_max=500)
        tb = _make_throttle(server, pair_max=10, ip_max=500)
        client = _client(server)
        total = 120
        barrier = threading.Barrier(total)
        results: dict[int, tuple[str, str]] = {}
        lock = threading.Lock()

        def worker(idx: int) -> None:
            throttle = ta if idx % 2 == 0 else tb
            action = ("commit", "settle", "release")[idx % 3]
            attempt = f"storm-{idx}"
            barrier.wait(timeout=30)
            if not throttle.reserve("stormuser", "9.9.9.9", attempt):
                with lock:
                    results[idx] = ("refused", action)
                return
            if action == "commit":
                throttle.commit_failure("stormuser", "9.9.9.9", attempt)
            elif action == "settle":
                throttle.settle_success("stormuser", "9.9.9.9", attempt)
            else:
                throttle.release("stormuser", "9.9.9.9", attempt)
            with lock:
                results[idx] = ("accepted", action)

        threads = [
            threading.Thread(target=worker, args=(i,)) for i in range(total)
        ]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=60)

        self.assertEqual(len(results), total)
        accepted = {i for i, (v, _) in results.items() if v == "accepted"}
        refused = {i for i, (v, _) in results.items() if v == "refused"}
        self.assertEqual(len(accepted) + len(refused), total)
        committed = {i for i in accepted if results[i][1] == "commit"}
        settled = {i for i in accepted if results[i][1] == "settle"}
        released = {i for i in accepted if results[i][1] == "release"}

        members = client.zrange(_pair("stormuser"), 0, -1)
        # the budget is a hard ceiling
        self.assertLessEqual(len(members), 10)
        # every surviving member is a COMMITTED failure (nothing else)
        for member in members:
            self.assertTrue(member.startswith("f:"), member)
            self.assertIn(int(member[len("f:storm-"):]), committed)
        # released / settled / refused ids are nowhere in the zset
        gone = released | settled | refused
        for i in gone:
            self.assertNotIn(f"f:storm-{i}", members)
            self.assertNotIn(f"p:storm-{i}", members)
        # no pending survives (each accepted thread resolved its own)
        self.assertFalse([m for m in members if m.startswith("p:")])
        # and the failures that DID survive are a subset of the commits
        self.assertTrue(set(members) <= {f"f:storm-{i}" for i in committed})


class FailClosedCommitTests(unittest.TestCase):
    """R3.3.1: a failure that cannot be recorded in the shared budget
    fails CLOSED (503), never silently under-counts. Success/release
    bookkeeping stays best-effort: losing THOSE only over-counts."""

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

    def test_dead_redis_commit_raises_settle_release_do_not(self) -> None:
        from quantpilot_api.login_throttle import (
            LoginThrottleUnavailable,
            RedisLoginThrottle,
        )

        throttle = RedisLoginThrottle("redis://127.0.0.1:1/0", 10, 50, 900)
        self.assertIsNone(throttle.reserve("carol", "5.5.5.5", "a-1"))
        self.assertTrue(throttle.degraded)
        with self.assertRaises(LoginThrottleUnavailable):
            throttle.commit_failure("carol", "5.5.5.5", "a-1")
        # success/abort side may degrade silently (over-counting is safe)
        throttle.settle_success("carol", "5.5.5.5", "a-1")
        throttle.release("carol", "5.5.5.5", "a-1")

    def test_mid_verification_outage_raises_and_keeps_local_record(self) -> None:
        # Redis dies BETWEEN reserve and commit: the shared budget cannot
        # record the failure -> LoginThrottleUnavailable; the LOCAL record
        # was already committed and stays (this instance stays throttled)
        server = fakeredis.FakeServer()
        with (
            patch.dict(os.environ, _redis_required_env()),
            _fake_redis_env(server),
        ):
            reservation = self.auth.reserve_login_attempt("vic", "9.9.9.9")
            self.assertIsNotNone(reservation)
            self.assertTrue(reservation.shared)
            # ... and now Redis "goes down" mid-verification
            self.auth._shared_throttle._degraded_until = time.time() + 60
            from quantpilot_api.login_throttle import LoginThrottleUnavailable

            with self.assertRaises(LoginThrottleUnavailable):
                self.auth.settle_login_failure(reservation)
            bucket = self.auth._login_bucket("vic", "9.9.9.9")
            entries = self.auth._login_failures[bucket]
            self.assertEqual(len(entries), 1)
            self.assertEqual(
                (entries[0][1], entries[0][2]), (reservation.attempt_id, "f")
            )

    def test_memory_only_failure_never_raises(self) -> None:
        with patch.dict(os.environ, _memory_only_env()):
            reservation = self.auth.reserve_login_attempt("vic", "9.9.9.9")
            self.assertIsNotNone(reservation)
            self.assertFalse(reservation.shared)
            self.auth.settle_login_failure(reservation)  # quiet
            bucket = self.auth._login_bucket("vic", "9.9.9.9")
            entries = self.auth._login_failures[bucket]
            self.assertEqual(len(entries), 1)
            self.assertEqual(
                (entries[0][1], entries[0][2]), (reservation.attempt_id, "f")
            )


class LoginEndpointMixedTests(unittest.TestCase):
    """The app.py wiring: 401 = the failure was COMMITTED; a commit
    that cannot reach Redis answers 503, not a silent 401."""

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

    def test_wrong_password_401_leaves_committed_failure_record(self) -> None:
        import quantpilot_api.app as app_module

        server = fakeredis.FakeServer()
        with (
            patch.dict(os.environ, _redis_required_env()),
            _fake_redis_env(server),
            patch("quantpilot_api.app.writers"),
            patch("quantpilot_api.auth.verify_login", return_value=None),
        ):
            client = TestClient(app_module.app)
            resp = client.post(
                "/api/v1/auth/login",
                json={"username": "miduser", "password": "wrong"},
            )
        self.assertEqual(resp.status_code, 401, resp.text)
        # the shared layer holds exactly ONE COMMITTED failure for the pair
        pair_key = PAIR_KEY.format(ip="testclient", username="miduser")
        members = _client(server).zrange(pair_key, 0, -1)
        self.assertEqual(len(members), 1)
        self.assertTrue(members[0].startswith("f:"))
        # ... and the local mirror agrees (state "f")
        bucket = self.auth._login_bucket("miduser", "testclient")
        self.assertEqual(
            [s for (_t, _a, s) in self.auth._login_failures[bucket]], ["f"]
        )

    def test_mid_verify_outage_answers_503_not_silent_401(self) -> None:
        import quantpilot_api.app as app_module

        server = fakeredis.FakeServer()

        def verify_then_redis_dies(username, password):
            # the reservation succeeded, the bcrypt check ran, and Redis
            # became unreachable BEFORE the failure could be committed
            self.auth._shared_throttle._degraded_until = time.time() + 60
            return None

        with (
            patch.dict(os.environ, _redis_required_env()),
            _fake_redis_env(server),
            patch("quantpilot_api.app.writers"),
            patch(
                "quantpilot_api.auth.verify_login",
                side_effect=verify_then_redis_dies,
            ),
        ):
            client = TestClient(app_module.app)
            resp = client.post(
                "/api/v1/auth/login",
                json={"username": "miduser", "password": "wrong"},
            )
        self.assertEqual(resp.status_code, 503, resp.text)
        self.assertEqual(
            resp.json()["error"]["code"], "LOGIN_THROTTLE_UNAVAILABLE"
        )
        # the unrecordable failure still throttles THIS instance locally
        bucket = self.auth._login_bucket("miduser", "testclient")
        self.assertEqual(
            [s for (_t, _a, s) in self.auth._login_failures[bucket]], ["f"]
        )


if __name__ == "__main__":
    unittest.main()
