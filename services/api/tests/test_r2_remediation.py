"""R2 acceptance remediation: the mandated automated security tests.

Each test maps to the acceptance checklist item(s) it covers:

#1  unauthenticated callers never reach business APIs (401)
#2  the shared service key must not bypass personal RBAC on business
    routes (P1-01); it stays valid for machine traffic only
#3  VIEWER cannot create trade commands
#4  ANALYST cannot execute trade commands
#5/#6  TRADER confined to granted accounts - body account_id on create
    (object-level authz, P1-02)
#7  TRADER cannot read other users' commands (query param + object lookup)
#8  TRADER cannot reach other users' backtests (instance lookup + job read)
#9  TRADER cannot touch other users' risk states (ACCOUNT + GLOBAL scope)
#10 disabled user's old JWT dies immediately (per-request user re-read)
#11 role change strips the old token's privileges immediately
#12 an expired Bearer never falls back to the service key
#13 revoked personal API keys stop working immediately
#14 personal API key list/revoke is owner-scoped (no existence oracle)
#15 frontend source-scan guard ships with the frontend remediation
#16 clean-environment install is delivered as a deployment artifact

Global surfaces (unresolved commands, GLOBAL breaker) are ADMIN-only by
design (P1-02): they cross every tenant.
"""

import json
import os
import unittest
from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock, patch
from uuid import uuid4

from fastapi.testclient import TestClient

from rbac_testkit import SECRET_ENV, bearer, service_key


def _client() -> TestClient:
    import quantpilot_api.app as app_module

    return TestClient(app_module.app)


def _command_body(account_id) -> dict:
    return {
        "command_id": str(uuid4()),
        "account_id": str(account_id),
        "command_type": "RECONCILE",
        "expires_at": (datetime.now(timezone.utc) + timedelta(seconds=60)).isoformat(),
        "created_by": "forged-attacker",
        "payload": {},
    }


class ServiceLaneTests(unittest.TestCase):
    """#1 / #2 / #12: authentication lanes and no service-key bypass."""

    def setUp(self) -> None:
        self.client = _client()

    def test_01_unauthenticated_business_api_is_401(self) -> None:
        with patch.dict(os.environ, {"QP_API_KEY": "svc-key"}):
            resp = self.client.get("/api/v1/commands")
        self.assertEqual(resp.status_code, 401)
        self.assertEqual(resp.json()["error"]["code"], "UNAUTHORIZED")

    def test_02_service_key_cannot_bypass_rbac(self) -> None:
        # the SAME key that carries agent traffic is refused on business
        # routes - it would otherwise impersonate a user with every account
        with service_key() as headers:
            business = self.client.get("/api/v1/commands", headers=headers)
            machine = self.client.post(
                "/api/v1/commands/pending", json={}, headers=headers
            )
        self.assertEqual(business.status_code, 403)
        self.assertEqual(business.json()["error"]["code"], "USER_IDENTITY_REQUIRED")
        # the machine lane itself stays open for the agent (pydantic 422 =
        # the request reached the router)
        self.assertEqual(machine.status_code, 422)

    def test_12_expired_bearer_never_falls_back_to_service_key(self) -> None:
        import quantpilot_api.auth as auth

        with patch.dict(os.environ, SECRET_ENV):
            token = auth.issue_token("u-1", "root", "ADMIN", ttl=-60)
        # BOTH credentials on one request: the dead Bearer must decide the
        # outcome (401), never silently degrade to the service-key identity
        with patch.dict(os.environ, SECRET_ENV):
            resp = self.client.get(
                "/api/v1/commands",
                headers={"Authorization": f"Bearer {token}", "X-API-Key": "svc-key"},
            )
        self.assertEqual(resp.status_code, 401)
        self.assertEqual(resp.json()["error"]["code"], "UNAUTHORIZED")


class CommandIsolationTests(unittest.TestCase):
    """#3-#7 + global-surface ADMIN rules (P1-02 object-level authz)."""

    def setUp(self) -> None:
        import quantpilot_api.app as app_module

        self.app = app_module
        self.client = TestClient(app_module.app)

    def test_03_viewer_cannot_create_commands(self) -> None:
        with bearer(role="VIEWER") as headers:
            resp = self.client.post(
                "/api/v1/commands", json=_command_body(uuid4()), headers=headers
            )
        self.assertEqual(resp.status_code, 403)
        self.assertEqual(resp.json()["error"]["code"], "ROLE_FORBIDDEN")

    def test_04_analyst_cannot_execute_commands(self) -> None:
        with bearer(role="ANALYST") as headers:
            resp = self.client.post(
                "/api/v1/commands", json=_command_body(uuid4()), headers=headers
            )
        self.assertEqual(resp.status_code, 403)
        self.assertEqual(resp.json()["error"]["code"], "ROLE_FORBIDDEN")

    def test_05_trader_cannot_command_ungranted_account(self) -> None:
        mine, other = uuid4(), uuid4()
        with (
            bearer(role="TRADER", granted_accounts=[mine]) as headers,
            patch.object(self.app.command_service, "create_command") as create,
        ):
            resp = self.client.post(
                "/api/v1/commands", json=_command_body(other), headers=headers
            )
        self.assertEqual(resp.status_code, 403, resp.text)
        # object-level: the command must never reach the durable core
        create.assert_not_called()

    def test_05b_trader_create_is_allowed_and_stamps_identity(self) -> None:
        mine = uuid4()
        with (
            bearer(role="TRADER", granted_accounts=[mine]) as headers,
            patch.object(
                self.app.command_service, "create_command",
                return_value={"created": True},
            ) as create,
        ):
            resp = self.client.post(
                "/api/v1/commands", json=_command_body(mine), headers=headers
            )
        self.assertEqual(resp.status_code, 200, resp.text)
        create.assert_called_once()
        # created_by is stamped server-side: a forged body value loses
        self.assertEqual(create.call_args.args[0].created_by, "root")

    def test_06_list_commands_cross_account_403_and_narrowing(self) -> None:
        mine, other = uuid4(), uuid4()
        with bearer(role="TRADER", granted_accounts=[mine]) as headers:
            explicit = self.client.get(
                f"/api/v1/commands?account_id={other}", headers=headers
            )
        self.assertEqual(explicit.status_code, 403)
        self.assertEqual(explicit.json()["error"]["code"], "FORBIDDEN")

        app_mock = MagicMock()
        app_mock.query.return_value = []
        app_mock.jsonable.side_effect = lambda value: value
        with (
            bearer(role="TRADER", granted_accounts=[mine]) as headers,
            patch.object(self.app, "db", app_mock),
        ):
            narrowed = self.client.get("/api/v1/commands", headers=headers)
        self.assertEqual(narrowed.status_code, 200)
        sql, params = app_mock.query.call_args[0]
        self.assertIn("user_account_grants", sql)  # narrowed, not leaky
        self.assertEqual(params[0], "u-1")

    def test_07_trader_cannot_read_foreign_command(self) -> None:
        mine, other = uuid4(), uuid4()
        row = {"command_id": str(uuid4()), "account_id": str(other), "status": "QUEUED"}
        with (
            bearer(role="TRADER", granted_accounts=[mine]) as headers,
            patch.object(self.app, "_command_by_id", return_value=row),
        ):
            resp = self.client.get(f"/api/v1/commands/{row['command_id']}", headers=headers)
        self.assertEqual(resp.status_code, 403)
        self.assertEqual(resp.json()["error"]["code"], "FORBIDDEN")

    def test_unresolved_commands_are_admin_only(self) -> None:
        with bearer(role="TRADER") as headers:
            denied = self.client.get("/api/v1/commands/unresolved", headers=headers)
        self.assertEqual(denied.status_code, 403)
        self.assertEqual(denied.json()["error"]["code"], "FORBIDDEN")

        with (
            bearer(role="ADMIN") as headers,
            patch.object(self.app.reconcile, "unresolved_commands", return_value=[]),
        ):
            allowed = self.client.get("/api/v1/commands/unresolved", headers=headers)
        self.assertEqual(allowed.status_code, 200)


class RiskIsolationTests(unittest.TestCase):
    """#9: risk states stay per-account; GLOBAL switches are ADMIN-only."""

    def setUp(self) -> None:
        import quantpilot_api.app as app_module

        self.app = app_module
        self.client = TestClient(app_module.app)

    def test_09_trader_cannot_stop_foreign_account(self) -> None:
        mine, other = uuid4(), uuid4()
        with (
            bearer(role="TRADER", granted_accounts=[mine]) as headers,
            patch.object(self.app, "_account_or_404", return_value=other),
        ):
            resp = self.client.post(
                "/api/v1/risk/stop",
                json={
                    "scope": "ACCOUNT", "account_id": str(other),
                    "reason": "unit", "updated_by": "unit-test",
                },
                headers=headers,
            )
        self.assertEqual(resp.status_code, 403)
        self.assertEqual(resp.json()["error"]["code"], "FORBIDDEN")

    def test_global_risk_controls_are_admin_only(self) -> None:
        for path, body in (
            ("/api/v1/risk/stop", {"scope": "GLOBAL", "reason": "x", "updated_by": "t"}),
            ("/api/v1/risk/resume", {"scope": "GLOBAL", "reason": "x", "updated_by": "t"}),
        ):
            with bearer(role="TRADER") as headers:
                resp = self.client.post(path, json=body, headers=headers)
            self.assertEqual(resp.status_code, 403, path)
            self.assertEqual(resp.json()["error"]["code"], "FORBIDDEN")


class BacktestIsolationTests(unittest.TestCase):
    """#8: the account arrives indirectly via strategy_instance_id / job_id."""

    def setUp(self) -> None:
        import quantpilot_api.app as app_module

        self.app = app_module
        self.client = TestClient(app_module.app)

    def _create_body(self, instance_id) -> dict:
        return {
            "strategy_instance_id": str(instance_id),
            "range_start": "2026-01-05T00:00:00+00:00",
            "range_end": "2026-01-05T02:00:00+00:00",
            "initial_balance": "10000",
            "volume": "1",
        }

    def test_08_trader_cannot_backtest_foreign_instance(self) -> None:
        mine, other = uuid4(), uuid4()
        app_mock = MagicMock()
        app_mock.query_one.return_value = {"account_id": str(other)}
        with (
            bearer(role="TRADER", granted_accounts=[mine]) as headers,
            patch.object(self.app, "db", app_mock),
            patch.object(self.app.backtests, "create_backtest") as create,
        ):
            resp = self.client.post(
                "/api/v1/backtests",
                json=self._create_body(uuid4()),
                headers={"Idempotency-Key": str(uuid4()), **headers},
            )
        self.assertEqual(resp.status_code, 403, resp.text)
        create.assert_not_called()

    def test_08b_trader_cannot_read_foreign_backtest(self) -> None:
        mine, other = uuid4(), uuid4()
        stored = {"job_id": str(uuid4()), "account_id": str(other), "status": "COMPLETED"}
        with (
            bearer(role="TRADER", granted_accounts=[mine]) as headers,
            patch.object(self.app.backtests, "get_backtest", return_value=stored),
        ):
            resp = self.client.get(f"/api/v1/backtests/{stored['job_id']}", headers=headers)
        self.assertEqual(resp.status_code, 403)


class AuditIsolationTests(unittest.TestCase):
    """P1-02: the audit trail follows the same grant discipline."""

    def setUp(self) -> None:
        import quantpilot_api.app as app_module

        self.app = app_module
        self.client = TestClient(app_module.app)

    def test_audit_logs_narrowed_to_grants(self) -> None:
        with (
            bearer(role="TRADER") as headers,
            patch.object(self.app.audit, "list_audit_logs", return_value=[]) as listed,
        ):
            resp = self.client.get("/api/v1/audit-logs", headers=headers)
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(listed.call_args.kwargs["granted_user_id"], "u-1")

    def test_audit_logs_cross_account_403(self) -> None:
        mine, other = uuid4(), uuid4()
        with bearer(role="TRADER", granted_accounts=[mine]) as headers:
            resp = self.client.get(f"/api/v1/audit-logs?account_id={other}", headers=headers)
        self.assertEqual(resp.status_code, 403)


class JwtLifecycleTests(unittest.TestCase):
    """#10 / #11: live tokens follow the users row, not their mint-time snapshot."""

    def setUp(self) -> None:
        import quantpilot_api.app as app_module

        self.app = app_module
        self.client = TestClient(app_module.app)

    def test_10_disabled_user_token_rejected(self) -> None:
        with bearer(role="TRADER", is_active=False) as headers:
            resp = self.client.get("/api/v1/auth/me", headers=headers)
        self.assertEqual(resp.status_code, 401)

    def test_10b_token_version_bump_rejects_old_token(self) -> None:
        # token minted at version 0, users row now at 1 (password reset /
        # disable / role change bumped it): the old JWT is dead on arrival
        with bearer(role="TRADER", token_version=0, db_token_version=1) as headers:
            resp = self.client.get("/api/v1/auth/me", headers=headers)
        self.assertEqual(resp.status_code, 401)

    def test_11_role_change_strips_privileges_immediately(self) -> None:
        # token mints ADMIN, the users row now says TRADER: the admin
        # surface must 403 - the old token keeps none of its minted role
        with bearer(role="ADMIN", db_role="TRADER") as headers:
            resp = self.client.get("/api/v1/auth/users", headers=headers)
        self.assertEqual(resp.status_code, 403)
        self.assertEqual(resp.json()["error"]["code"], "ADMIN_ONLY")

    def test_update_user_bumps_token_version(self) -> None:
        app_mock = MagicMock()
        app_mock.query_one.return_value = {
            "user_id": "u-2", "username": "bob", "role": "TRADER", "is_active": True,
        }
        app_mock.query.return_value = []
        app_mock.jsonable.side_effect = lambda value: value
        with (
            bearer(role="ADMIN") as headers,
            patch.object(self.app, "db", app_mock),
            patch("quantpilot_api.app.writers"),
        ):
            resp = self.client.patch(
                f"/api/v1/auth/users/{uuid4()}",
                json={"role": "TRADER"},
                headers=headers,
            )
        self.assertEqual(resp.status_code, 200, resp.text)
        sql = app_mock.query_one.call_args[0][0]
        self.assertIn("token_version = token_version + 1", sql)


class ApiKeyLifecycleTests(unittest.TestCase):
    """#13 / #14: per-user keys are live-checked and owner-scoped."""

    def setUp(self) -> None:
        import quantpilot_api.app as app_module

        self.app = app_module
        self.client = TestClient(app_module.app)

    def test_13_revoked_key_rejected_immediately(self) -> None:
        import quantpilot_api.auth as auth

        mock = MagicMock()
        mock.query_one.return_value = {
            "key_id": "k-1", "user_id": "u-1",
            "revoked_at": "2026-10-31T00:00:00+00:00",  # revoked - live check
            "username": "alice", "role": "TRADER", "is_active": True,
        }
        with (
            patch.dict(os.environ, {"QP_API_KEY": "svc-key"}),
            patch.object(auth, "db", mock),
        ):
            resp = self.client.get(
                "/api/v1/auth/me", headers={"X-API-Key": "qpk_" + "a" * 48}
            )
        self.assertEqual(resp.status_code, 401)

    def test_14_key_listing_scoped_to_owner(self) -> None:
        app_mock = MagicMock()
        app_mock.query.return_value = []
        app_mock.jsonable.side_effect = lambda value: value
        with (
            bearer(role="TRADER") as headers,
            patch.object(self.app, "db", app_mock),
        ):
            resp = self.client.get("/api/v1/auth/api-keys", headers=headers)
        self.assertEqual(resp.status_code, 200)
        sql, params = app_mock.query.call_args[0]
        self.assertIn("user_id = %s", sql)
        self.assertEqual(params, ("u-1",))

    def test_14b_revocation_is_owner_scoped_no_existence_oracle(self) -> None:
        app_mock = MagicMock()
        app_mock.query_one.return_value = None  # UPDATE ... RETURNING: no row
        with (
            bearer(role="TRADER") as headers,
            patch.object(self.app, "db", app_mock),
            patch("quantpilot_api.app.writers"),
        ):
            resp = self.client.delete(
                f"/api/v1/auth/api-keys/{uuid4()}", headers=headers
            )
        self.assertEqual(resp.status_code, 404)  # foreign key == missing key
        sql, params = app_mock.query_one.call_args[0]
        self.assertIn("user_id = %s", sql)


class LoginHardeningTests(unittest.TestCase):
    """P2-03/P2-04: rate limiting, timing parity, secret-free audit."""

    def setUp(self) -> None:
        import quantpilot_api.auth as auth

        self.auth = auth
        self.client = _client()
        # the failure buckets are process-global; isolate every test
        self.addCleanup(auth._login_failures.clear)

    def test_rate_limit_locks_after_max_failures(self) -> None:
        with (
            patch.dict(os.environ, SECRET_ENV),
            patch.object(self.auth, "_LOGIN_MAX_FAILURES", 3),
            patch.object(self.auth, "verify_login", return_value=None),
            patch("quantpilot_api.app.writers"),
        ):
            for _ in range(3):
                resp = self.client.post(
                    "/api/v1/auth/login",
                    json={"username": "bruteforce", "password": "x"},
                )
                self.assertEqual(resp.status_code, 401)
            fourth = self.client.post(
                "/api/v1/auth/login", json={"username": "bruteforce", "password": "x"}
            )
        self.assertEqual(fourth.status_code, 429)
        self.assertEqual(fourth.json()["error"]["code"], "TOO_MANY_ATTEMPTS")

    def test_successful_login_clears_the_failure_bucket(self) -> None:
        user = {"id": "u-1", "username": "lockout", "role": "TRADER", "token_version": 0}
        with (
            patch.dict(os.environ, SECRET_ENV),
            patch.object(self.auth, "_LOGIN_MAX_FAILURES", 3),
            patch.object(self.auth, "verify_login",
                         side_effect=[None, None, user, None]),
            patch("quantpilot_api.app.writers"),
        ):
            for _ in range(2):
                self.client.post(
                    "/api/v1/auth/login", json={"username": "lockout", "password": "x"}
                )
            ok_login = self.client.post(
                "/api/v1/auth/login", json={"username": "lockout", "password": "right"}
            )
            after = self.client.post(
                "/api/v1/auth/login", json={"username": "lockout", "password": "x"}
            )
        self.assertEqual(ok_login.status_code, 200)
        # bucket was cleared on success -> this is failure #1, not #3
        self.assertEqual(after.status_code, 401)

    def test_dummy_hash_burns_the_same_bcrypt_cost(self) -> None:
        # P2-04: unknown usernames burn a bcrypt check at cost 12, the same
        # as real password hashes - timing cannot enumerate usernames
        self.auth._dummy_verify("some-password")
        self.assertTrue(self.auth._dummy_hash.startswith(b"$2b$12$"))

    def test_login_events_never_carry_secrets(self) -> None:
        with (
            patch.dict(os.environ, SECRET_ENV),
            patch.object(self.auth, "verify_login", return_value=None),
            patch("quantpilot_api.app.writers") as writers,
        ):
            self.client.post(
                "/api/v1/auth/login",
                json={"username": "alice", "password": "super-secret-pw"},
            )
        self.assertTrue(writers.add_system_event.called)
        for call in writers.add_system_event.call_args_list:
            payload = json.dumps(call.args[3] if len(call.args) > 3 else {})
            self.assertNotIn("super-secret-pw", payload)


if __name__ == "__main__":
    unittest.main()
