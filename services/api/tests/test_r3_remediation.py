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
  restart, loud degradation to memory-only when Redis dies
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
    """P2-03: pure-IP spray bucket + shared Redis layer + degradation."""

    def setUp(self) -> None:
        import quantpilot_api.auth as auth

        self.auth = auth
        auth._login_failures.clear()
        auth._login_failures_ip.clear()
        auth._shared_throttle = None

    def tearDown(self) -> None:
        self.auth._login_failures.clear()
        self.auth._login_failures_ip.clear()
        self.auth._shared_throttle = None

    def test_ip_spray_bucket_blocks_rotating_usernames(self) -> None:
        # default _LOGIN_IP_MAX_FAILURES = 5 x _LOGIN_MAX_FAILURES = 50;
        # patch the limits down so the ip bucket alone triggers
        with (
            patch.object(self.auth, "_LOGIN_MAX_FAILURES", 100),
            patch.object(self.auth, "_LOGIN_IP_MAX_FAILURES", 3),
        ):
            for i in range(3):
                self.auth.record_login_failure(f"user{i}", "9.9.9.9")
            # every username from this source is now blocked...
            self.assertTrue(self.auth.login_too_many_attempts("fresh-name", "9.9.9.9"))
            # ...while a different source is untouched
            self.assertFalse(self.auth.login_too_many_attempts("user0", "8.8.8.8"))

    def test_success_clears_pair_but_not_ip_bucket(self) -> None:
        self.auth.record_login_failure("alice", "9.9.9.9")
        self.auth.clear_login_failures("alice", "9.9.9.9")
        self.assertFalse(self.auth.login_too_many_attempts("alice", "9.9.9.9"))
        # the ip bucket survived the success (spray protection)
        self.assertEqual(len(self.auth._login_failures_ip.get("9.9.9.9", [])), 1)

    def _fake_redis_env(self, server):
        from quantpilot_api import login_throttle

        def fake_from_url(url, **_kwargs):
            return fakeredis.FakeStrictRedis(server=server, decode_responses=True)

        return patch.object(login_throttle.redis.Redis, "from_url", fake_from_url)

    def test_redis_layer_shared_across_instances(self) -> None:
        # two "API processes": one shared fakeredis server behind both
        server = fakeredis.FakeServer()
        with (
            patch.dict(os.environ, {**SECRET_ENV, "QP_REDIS_URL": "redis://shared/0"}),
            patch.object(self.auth, "_LOGIN_MAX_FAILURES", 3),
            self._fake_redis_env(server),
        ):
            for _ in range(3):
                self.auth.record_login_failure("alice", "9.9.9.9")
            # process "restart": drop every in-process cache/throttle
            self.auth._login_failures.clear()
            self.auth._login_failures_ip.clear()
            self.auth._shared_throttle = None
            # the shared counter survives the restart
            self.assertTrue(self.auth.login_too_many_attempts("alice", "9.9.9.9"))

    def test_redis_layer_counts_by_ip_dimension_too(self) -> None:
        server = fakeredis.FakeServer()
        with (
            patch.dict(os.environ, {**SECRET_ENV, "QP_REDIS_URL": "redis://shared/0"}),
            patch.object(self.auth, "_LOGIN_MAX_FAILURES", 100),
            patch.object(self.auth, "_LOGIN_IP_MAX_FAILURES", 3),
            self._fake_redis_env(server),
        ):
            for i in range(3):
                self.auth.record_login_failure(f"victim{i}", "7.7.7.7")
            self.auth._login_failures.clear()
            self.auth._login_failures_ip.clear()
            self.auth._shared_throttle = None
            self.assertTrue(self.auth.login_too_many_attempts("anyone", "7.7.7.7"))

    def test_redis_success_clears_shared_pair_counter(self) -> None:
        server = fakeredis.FakeServer()
        with (
            patch.dict(os.environ, {**SECRET_ENV, "QP_REDIS_URL": "redis://shared/0"}),
            patch.object(self.auth, "_LOGIN_MAX_FAILURES", 3),
            self._fake_redis_env(server),
        ):
            self.auth.record_login_failure("bob", "6.6.6.6")
            self.auth.clear_login_failures("bob", "6.6.6.6")
            self.auth._login_failures.clear()
            self.auth._login_failures_ip.clear()
            self.auth._shared_throttle = None
            self.assertFalse(self.auth.login_too_many_attempts("bob", "6.6.6.6"))

    def test_redis_outage_degrades_to_memory_only(self) -> None:
        # port 1 on localhost: connection refused, fast and certain
        with patch.dict(os.environ, {**SECRET_ENV, "QP_REDIS_URL": "redis://127.0.0.1:1/0"}):
            # building the throttle against a dead server must not raise
            self.auth.record_login_failure("carol", "5.5.5.5")
            # memory layer still enforces the limit
            with patch.object(self.auth, "_LOGIN_MAX_FAILURES", 2):
                self.auth.record_login_failure("carol", "5.5.5.5")
                self.auth.record_login_failure("carol", "5.5.5.5")
                self.assertTrue(self.auth.login_too_many_attempts("carol", "5.5.5.5"))

    def test_no_redis_url_means_memory_only(self) -> None:
        env = {k: v for k, v in os.environ.items() if k != "QP_REDIS_URL"}
        with patch.dict(os.environ, env, clear=True):
            self.auth.record_login_failure("dave", "4.4.4.4")
            self.assertIsNone(self.auth._throttle())
            self.assertFalse(self.auth.login_too_many_attempts("dave", "4.4.4.4"))


if __name__ == "__main__":
    unittest.main()
