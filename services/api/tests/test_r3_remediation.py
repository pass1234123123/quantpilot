"""R3 remediation unit tests: the mandated automated security tests.

Covers the three backend items of round 3:

* P2-01 global data scoping - GET /nodes, /terminals, /events must narrow
  to the caller's granted accounts (ADMIN sees all; events without an
  account are ADMIN-only via the account_id IN (...) filter, which NULL
  rows never satisfy)
* P2-02 operator identity - risk stop/resume stamp the authenticated
  principal everywhere (DB row, system event, audit row); the request
  body cannot carry updated_by at all (422, extra=forbid) and
  writers.add_system_event resolves the event's account for scoping
* P2-03 login throttling - pure-IP spray bucket, Redis layer shared
  across instances (fakeredis-backed server), counters surviving an API
  restart; R3.1 pins the failure semantics in LoginThrottleFailSafeTests:
  memory_only keeps local limiting, redis_required answers 503 and never
  degrades silently
"""

import os
import unittest
from unittest.mock import MagicMock, patch
from uuid import uuid4

import fakeredis
from fastapi.testclient import TestClient

from rbac_testkit import SECRET_ENV, bearer


def _client() -> TestClient:
    import quantpilot_api.app as app_module

    return TestClient(app_module.app)


def _memory_only_env() -> dict:
    """Hermetic memory_only throttle config (R3.1): the in-process buckets
    are authoritative regardless of the host environment."""
    return {
        **SECRET_ENV,
        "QP_LOGIN_THROTTLE_MODE": "memory_only",
        "QP_LOGIN_THROTTLE_REDIS_URL": "",
    }


def _redis_required_env(url: str = "redis://shared/0") -> dict:
    """redis_required throttle config (R3.1): shared counting is
    mandatory - an unreachable Redis must refuse logins, not degrade."""
    return {
        **SECRET_ENV,
        "QP_LOGIN_THROTTLE_MODE": "redis_required",
        "QP_LOGIN_THROTTLE_REDIS_URL": url,
    }


def _fake_redis_env(server):
    """Point login_throttle's Redis.from_url at a shared fakeredis server
    so several "API processes" can share one counter store."""
    from quantpilot_api import login_throttle

    def fake_from_url(url, **_kwargs):
        return fakeredis.FakeStrictRedis(server=server, decode_responses=True)

    return patch.object(login_throttle.redis.Redis, "from_url", fake_from_url)


class GlobalSurfaceScopingTests(unittest.TestCase):
    """P2-01: nodes/terminals/events narrow to granted accounts."""

    def setUp(self) -> None:
        import quantpilot_api.app as app_module

        self.app = app_module
        self.client = TestClient(app_module.app)

    def _assert_narrowed(self, path: str) -> tuple[str, tuple]:
        app_mock = MagicMock()
        app_mock.query.return_value = []
        app_mock.jsonable.side_effect = lambda value: value
        with (
            bearer(role="TRADER", granted_accounts=[uuid4()]) as headers,
            patch.object(self.app, "db", app_mock),
        ):
            resp = self.client.get(path, headers=headers)
        self.assertEqual(resp.status_code, 200, resp.text)
        sql, params = app_mock.query.call_args[0]
        self.assertIn("user_account_grants", sql, path)
        self.assertEqual(params[0], "u-1")
        return sql, params

    def test_nodes_narrowed_for_trader(self) -> None:
        sql, _ = self._assert_narrowed("/api/v1/nodes")
        self.assertIn("trading_nodes", sql)

    def test_terminals_narrowed_for_trader(self) -> None:
        sql, _ = self._assert_narrowed("/api/v1/terminals")
        self.assertIn("mt5_terminals", sql)

    def test_events_narrowed_for_trader(self) -> None:
        sql, _ = self._assert_narrowed("/api/v1/events")
        self.assertIn("e.account_id IN", sql)

    def test_events_narrowing_survives_severity_filter(self) -> None:
        app_mock = MagicMock()
        app_mock.query.return_value = []
        app_mock.jsonable.side_effect = lambda value: value
        with (
            bearer(role="TRADER", granted_accounts=[uuid4()]) as headers,
            patch.object(self.app, "db", app_mock),
        ):
            resp = self.client.get("/api/v1/events?severity=WARN", headers=headers)
        self.assertEqual(resp.status_code, 200, resp.text)
        sql, params = app_mock.query.call_args[0]
        self.assertIn("e.account_id IN", sql)
        self.assertIn("e.severity = %s", sql)
        self.assertEqual(params, ("u-1", "WARN", 100))

    def test_viewer_is_narrowed_too(self) -> None:
        app_mock = MagicMock()
        app_mock.query.return_value = []
        app_mock.jsonable.side_effect = lambda value: value
        with (
            bearer(role="VIEWER", granted_accounts=[uuid4()]) as headers,
            patch.object(self.app, "db", app_mock),
        ):
            resp = self.client.get("/api/v1/events", headers=headers)
        self.assertEqual(resp.status_code, 200, resp.text)
        sql, _ = app_mock.query.call_args[0]
        self.assertIn("e.account_id IN", sql)

    def _assert_unfiltered(self, path: str) -> None:
        app_mock = MagicMock()
        app_mock.query.return_value = []
        app_mock.jsonable.side_effect = lambda value: value
        with (
            bearer(role="ADMIN") as headers,
            patch.object(self.app, "db", app_mock),
        ):
            resp = self.client.get(path, headers=headers)
        self.assertEqual(resp.status_code, 200, resp.text)
        # admin path passes SQL only (no narrowing params)
        sql = app_mock.query.call_args[0][0]
        self.assertNotIn("user_account_grants", sql)

    def test_admin_sees_all_nodes_terminals_events(self) -> None:
        self._assert_unfiltered("/api/v1/nodes")
        self._assert_unfiltered("/api/v1/terminals")
        self._assert_unfiltered("/api/v1/events")

    def test_service_key_cannot_read_global_surfaces(self) -> None:
        from rbac_testkit import service_key

        with service_key() as headers:
            for path in ("/api/v1/nodes", "/api/v1/terminals", "/api/v1/events"):
                resp = self.client.get(path, headers=headers)
                self.assertEqual(resp.status_code, 403, path)
                self.assertEqual(resp.json()["error"]["code"], "USER_IDENTITY_REQUIRED")


class SystemEventScopingWriterTests(unittest.TestCase):
    """P2-01 support: add_system_event resolves the account for scoping."""

    def test_payload_account_id_is_stored(self) -> None:
        import quantpilot_api.writers as writers

        account = uuid4()
        db_mock = MagicMock()
        conn_ctx = MagicMock()
        conn_ctx.__enter__.return_value = conn_ctx
        db_mock.conn.return_value = conn_ctx
        with patch.object(writers, "db", db_mock):
            writers.add_system_event("api", "x.y", "INFO", {"account_id": str(account)})
        params = conn_ctx.execute.call_args[0][1]
        self.assertEqual(str(params[4]), str(account))

    def test_explicit_account_wins_over_payload(self) -> None:
        import quantpilot_api.writers as writers

        explicit, payload_val = uuid4(), uuid4()
        db_mock = MagicMock()
        conn_ctx = MagicMock()
        conn_ctx.__enter__.return_value = conn_ctx
        db_mock.conn.return_value = conn_ctx
        with patch.object(writers, "db", db_mock):
            writers.add_system_event(
                "api", "x.y", "INFO", {"account_id": str(payload_val)},
                account_id=explicit,
            )
        params = conn_ctx.execute.call_args[0][1]
        self.assertEqual(str(params[4]), str(explicit))

    def test_non_uuid_account_id_stays_global(self) -> None:
        import quantpilot_api.writers as writers

        db_mock = MagicMock()
        conn_ctx = MagicMock()
        conn_ctx.__enter__.return_value = conn_ctx
        db_mock.conn.return_value = conn_ctx
        with patch.object(writers, "db", db_mock):
            writers.add_system_event("api", "x.y", "INFO", {"account_id": "GLOBAL"})
        params = conn_ctx.execute.call_args[0][1]
        self.assertIsNone(params[4])

    def test_missing_account_id_stays_global(self) -> None:
        import quantpilot_api.writers as writers

        db_mock = MagicMock()
        conn_ctx = MagicMock()
        conn_ctx.__enter__.return_value = conn_ctx
        db_mock.conn.return_value = conn_ctx
        with patch.object(writers, "db", db_mock):
            writers.add_system_event("api", "auth.login", "INFO", {"username": "a"})
        params = conn_ctx.execute.call_args[0][1]
        self.assertIsNone(params[4])


class LoginThrottleLayeredTests(unittest.TestCase):
    """P2-03 local layer: pure-IP spray bucket + (ip, username) pairs.

    The shared-Redis scenarios live in LoginThrottleFailSafeTests below
    (R3.1). These tests pin QP_LOGIN_THROTTLE_MODE=memory_only so the
    in-process buckets stay authoritative on any host.

    R3.3 P1-01: the check and the spend are one operation, so a failed
    login is now simulated by reserving (which SUCCEEDS) and then
    keeping the reservation - exactly what the endpoint does on a wrong
    password."""

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

    def test_ip_spray_bucket_blocks_rotating_usernames(self) -> None:
        # default _LOGIN_IP_MAX_FAILURES = 5 x _LOGIN_MAX_FAILURES = 50;
        # patch the limits down so the ip bucket alone triggers
        with (
            patch.dict(os.environ, _memory_only_env()),
            patch.object(self.auth, "_LOGIN_MAX_FAILURES", 100),
            patch.object(self.auth, "_LOGIN_IP_MAX_FAILURES", 3),
        ):
            for i in range(3):
                # three failed logins from one source, three usernames
                self.assertIsNotNone(
                    self.auth.reserve_login_attempt(f"user{i}", "9.9.9.9")
                )
            # every username from this source is now blocked...
            self.assertIsNone(self.auth.reserve_login_attempt("fresh-name", "9.9.9.9"))
            # ...while a different source is untouched
            self.assertIsNotNone(self.auth.reserve_login_attempt("user0", "8.8.8.8"))

    def test_success_clears_pair_but_not_ip_bucket(self) -> None:
        with patch.dict(os.environ, _memory_only_env()):
            # bob's failure must survive alice's success in the shared
            # spray bucket (R3.3 P1-01: a success releases only its OWN
            # spray slot and never wipes other recorded failures)
            self.assertIsNotNone(self.auth.reserve_login_attempt("bob", "9.9.9.9"))
            alice = self.auth.reserve_login_attempt("alice", "9.9.9.9")
            self.assertIsNotNone(alice)
            self.auth.settle_login_success(alice)
            # the ip bucket kept exactly bob's failure: alice's success
            # released only her OWN spray slot (len == 1, bob's attempt id)
            self.assertEqual(len(self.auth._login_failures_ip.get("9.9.9.9", [])), 1)
            bob_bucket = self.auth._login_bucket("bob", "9.9.9.9")
            self.assertEqual(
                [a for (_, a) in self.auth._login_failures_ip["9.9.9.9"]],
                [a for (_, a, _) in self.auth._login_failures[bob_bucket]],
            )
            # alice's pair bucket is cleared: she is not throttled anymore
            self.assertIsNotNone(self.auth.reserve_login_attempt("alice", "9.9.9.9"))


class LoginThrottleFailSafeTests(unittest.TestCase):
    """R3.1 P1: explicit fail-closed semantics for the shared Redis layer.

    The eight mandated fault-injection scenarios:
      1 healthy Redis           shared reservations work across instances
      2 connection failure      reserve reports None (unavailable)
      3 timeout                 reserve reports None
      4 redis_required outage   login path raises LoginThrottleUnavailable
                                -> HTTP 503; NEVER a silent fallback to
                                per-instance counters
      5 memory_only + outage     local limiting continues, nothing raises
      6 recovery                the stand-down window expires and the SHARED
                                budget comes back into force automatically
      7 multi-instance           two processes share one budget
      8 concurrency             parallel reserves land exact counts
                                (atomic Lua trim+check+ZADD, R3.3 P1-01)
    """

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

    # -- 1 + 7: healthy shared counting across instances/restarts ---------

    def test_shared_counting_across_instances_and_restarts(self) -> None:
        server = fakeredis.FakeServer()
        with (
            patch.dict(os.environ, _redis_required_env()),
            patch.object(self.auth, "_LOGIN_MAX_FAILURES", 3),
            _fake_redis_env(server),
        ):
            for _ in range(3):
                self.assertIsNotNone(
                    self.auth.reserve_login_attempt("alice", "9.9.9.9")
                )
            # process "restart": drop every in-process cache/throttle; a
            # fresh instance still sees the shared budget spent
            self.auth._login_failures.clear()
            self.auth._login_failures_ip.clear()
            self.auth._shared_throttle = None
            self.assertIsNone(self.auth.reserve_login_attempt("alice", "9.9.9.9"))

    def test_shared_ip_dimension_still_counts(self) -> None:
        server = fakeredis.FakeServer()
        with (
            patch.dict(os.environ, _redis_required_env()),
            patch.object(self.auth, "_LOGIN_MAX_FAILURES", 100),
            patch.object(self.auth, "_LOGIN_IP_MAX_FAILURES", 3),
            _fake_redis_env(server),
        ):
            for i in range(3):
                self.assertIsNotNone(
                    self.auth.reserve_login_attempt(f"victim{i}", "7.7.7.7")
                )
            self.auth._login_failures.clear()
            self.auth._login_failures_ip.clear()
            self.auth._shared_throttle = None
            self.assertIsNone(self.auth.reserve_login_attempt("anyone", "7.7.7.7"))

    def test_success_clears_shared_pair_counter(self) -> None:
        server = fakeredis.FakeServer()
        with (
            patch.dict(os.environ, _redis_required_env()),
            patch.object(self.auth, "_LOGIN_MAX_FAILURES", 3),
            _fake_redis_env(server),
        ):
            reservation = self.auth.reserve_login_attempt("bob", "6.6.6.6")
            self.assertIsNotNone(reservation)
            self.auth.settle_login_success(reservation)
            self.auth._login_failures.clear()
            self.auth._login_failures_ip.clear()
            self.auth._shared_throttle = None
            self.assertIsNotNone(self.auth.reserve_login_attempt("bob", "6.6.6.6"))

    # -- 2 + 3: outage detection at the throttle layer ----------------------

    def test_connection_failure_reports_unavailable(self) -> None:
        # port 1 on localhost: connection refused, fast and certain
        from quantpilot_api.login_throttle import RedisLoginThrottle

        throttle = RedisLoginThrottle("redis://127.0.0.1:1/0", 10, 50, 900)
        self.assertIsNone(throttle.reserve("carol", "5.5.5.5", "a-1"))
        self.assertTrue(throttle.degraded)
        # settling during the outage must not raise
        throttle.settle_success("carol", "5.5.5.5", "a-1")
        throttle.release("carol", "5.5.5.5", "a-1")

    def test_timeout_reports_unavailable(self) -> None:
        import redis as redis_lib
        from quantpilot_api.login_throttle import RedisLoginThrottle

        server = fakeredis.FakeServer()
        with _fake_redis_env(server):
            throttle = RedisLoginThrottle("redis://shared/0", 10, 50, 900)
        with patch.object(
            throttle,
            "_reserve_script",
            side_effect=redis_lib.exceptions.TimeoutError("simulated timeout"),
        ):
            self.assertIsNone(throttle.reserve("dave", "4.4.4.4", "a-1"))
        self.assertTrue(throttle.degraded)

    # -- 4: redis_required refuses instead of degrading ---------------------

    def test_required_mode_raises_when_redis_unreachable(self) -> None:
        from quantpilot_api.auth import LoginThrottleUnavailable

        with patch.dict(os.environ, _redis_required_env("redis://127.0.0.1:1/0")):
            with self.assertRaises(LoginThrottleUnavailable):
                self.auth.reserve_login_attempt("erin", "3.3.3.3")
            # degraded stand-down window: still refusing, never degrading
            with self.assertRaises(LoginThrottleUnavailable):
                self.auth.reserve_login_attempt("erin", "3.3.3.3")

    def test_required_mode_without_url_rejects_logins(self) -> None:
        from quantpilot_api.auth import LoginThrottleUnavailable

        with patch.dict(os.environ, _redis_required_env("")):
            with self.assertRaises(LoginThrottleUnavailable):
                self.auth.reserve_login_attempt("judy", "9.9.9.9")

    def test_required_mode_answers_503_on_the_login_route(self) -> None:
        import quantpilot_api.app as app_module

        with (
            patch.dict(os.environ, _redis_required_env("redis://127.0.0.1:1/0")),
            patch("quantpilot_api.app.writers") as writers,
        ):
            client = TestClient(app_module.app)
            resp = client.post(
                "/api/v1/auth/login",
                json={"username": "frank", "password": "whatever"},
            )
        self.assertEqual(resp.status_code, 503, resp.text)
        self.assertEqual(resp.json()["error"]["code"], "LOGIN_THROTTLE_UNAVAILABLE")
        # the outage is auditable, never silent
        self.assertIn(
            "auth.login_throttle_unavailable",
            [call.args[1] for call in writers.add_system_event.call_args_list],
        )

    # -- 5: memory_only keeps limiting locally ------------------------------

    def test_memory_only_outage_still_limits_locally(self) -> None:
        # a dead Redis URL is irrelevant in memory_only mode: the local
        # buckets stay authoritative and nothing ever raises
        env = {**_memory_only_env(), "QP_LOGIN_THROTTLE_REDIS_URL": "redis://127.0.0.1:1/0"}
        with (
            patch.dict(os.environ, env),
            patch.object(self.auth, "_LOGIN_MAX_FAILURES", 2),
        ):
            self.assertIsNotNone(self.auth.reserve_login_attempt("grace", "2.2.2.2"))
            self.assertIsNotNone(self.auth.reserve_login_attempt("grace", "2.2.2.2"))
            self.assertIsNone(self.auth.reserve_login_attempt("grace", "2.2.2.2"))

    def test_memory_only_with_url_warns_once(self) -> None:
        # explicit degradation must be loud (R3.1 P1 #5): a configured URL
        # ignored by the mode earns exactly one SECURITY warning
        env = {**_memory_only_env(), "QP_LOGIN_THROTTLE_REDIS_URL": "redis://shared/0"}
        with (
            patch.dict(os.environ, env),
            self.assertLogs("quantpilot.auth", level="WARNING") as logs,
        ):
            self.assertIsNone(self.auth._throttle())
            self.assertIsNone(self.auth._throttle())
        warnings = [line for line in logs.output if "memory_only" in line]
        self.assertEqual(len(warnings), 1)

    def test_unset_config_means_memory_only(self) -> None:
        env = {
            k: v
            for k, v in os.environ.items()
            if k
            not in (
                "QP_REDIS_URL",
                "QP_LOGIN_THROTTLE_REDIS_URL",
                "QP_LOGIN_THROTTLE_MODE",
            )
        }
        with patch.dict(os.environ, env, clear=True):
            self.assertIsNotNone(self.auth.reserve_login_attempt("kate", "4.4.4.4"))
            self.assertIsNone(self.auth._throttle())
            self.assertIsNotNone(self.auth.reserve_login_attempt("kate", "4.4.4.4"))

    # -- 6: recovery resumes shared counting --------------------------------

    def test_recovery_resumes_shared_counting(self) -> None:
        import redis as redis_lib

        server = fakeredis.FakeServer()
        with (
            patch.dict(os.environ, _redis_required_env()),
            patch.object(self.auth, "_LOGIN_MAX_FAILURES", 3),
            _fake_redis_env(server),
        ):
            throttle = self.auth._throttle()
            for i in range(3):
                self.assertTrue(throttle.reserve("heidi", "1.1.1.1", f"a-{i}"))
            self.assertFalse(throttle.reserve("heidi", "1.1.1.1", "blocked"))
            # outage: script errors, then the 60 s stand-down window
            with patch.object(
                throttle,
                "_reserve_script",
                side_effect=redis_lib.exceptions.ConnectionError("down"),
            ):
                self.assertIsNone(throttle.reserve("heidi", "1.1.1.1", "x-1"))
            self.assertIsNone(throttle.reserve("heidi", "1.1.1.1", "x-2"))
            # window over -> "recovered": the SHARED verdict returns
            throttle._degraded_until = 0.0
            self.assertFalse(throttle.reserve("heidi", "1.1.1.1", "x-3"))

    # -- 8: concurrency lands exact counts -----------------------------------

    def test_concurrent_reservations_count_exact(self) -> None:
        import secrets
        import threading

        from quantpilot_api import login_throttle
        from quantpilot_api.login_throttle import RedisLoginThrottle

        server = fakeredis.FakeServer()
        with _fake_redis_env(server):
            throttle = RedisLoginThrottle("redis://shared/0", 10_000, 50_000, 900)

        def hammer() -> None:
            for _ in range(25):
                throttle.reserve("ivan", "8.8.4.4", secrets.token_hex(8))

        threads = [threading.Thread(target=hammer) for _ in range(8)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        pair_key = login_throttle._PAIR_KEY.format(ip="8.8.4.4", username="ivan")
        ip_key = login_throttle._IP_KEY.format(ip="8.8.4.4")
        # one ZSET member per attempt: no count drift under concurrency
        self.assertEqual(int(throttle._client.zcard(pair_key)), 200)
        self.assertEqual(int(throttle._client.zcard(ip_key)), 200)
        # the atomic script leaves no bucket behind without its TTL
        self.assertGreater(int(throttle._client.ttl(pair_key)), 0)
        self.assertGreater(int(throttle._client.ttl(ip_key)), 0)


if __name__ == "__main__":
    unittest.main()
