"""User auth + RBAC (phase-3 R2): JWT login, four roles, per-user API keys
and per-account grants layered ON TOP of the unchanged service-key gate.

Compatibility invariant: the X-API-Key service path behaves exactly as
before (see test_app_auth.py). Humans authenticate via Bearer JWT (login
with username + bcrypt password) or a personal qpk_... key. Users are
confined to granted accounts and to what their role allows; machine routes
(nodes/register, commands/pending ...) stay service-key-only; the
fail-closed order of checks (503 API_KEY_NOT_CONFIGURED -> 401 -> 403)
never regresses - even for login.
"""

import base64
import json
import os
import unittest
from unittest.mock import MagicMock, patch

from fastapi.testclient import TestClient

SECRET_ENV = {"QP_API_KEY": "svc-key", "QP_JWT_SECRET": "jwt-secret"}
UID = "11111111-2222-3333-4444-555555555555"


def _client() -> TestClient:
    import quantpilot_api.app as app_module

    return TestClient(app_module.app)


def _principal(role: str):
    from quantpilot_api import auth

    return auth.Principal(kind="user", user_id="u-1", username="alice", role=role)


def _env_without(*keys: str) -> dict:
    return {k: v for k, v in os.environ.items() if k not in keys}


class PasswordTests(unittest.TestCase):
    def test_roundtrip_and_bcrypt_prefix(self) -> None:
        from quantpilot_api import auth

        hashed = auth.hash_password("pass-word-123", rounds=4)
        self.assertTrue(hashed.startswith(b"$2"))
        self.assertTrue(auth.verify_password("pass-word-123", hashed))
        self.assertFalse(auth.verify_password("wrong-password", hashed))

    def test_corrupt_hash_is_false_not_a_crash(self) -> None:
        from quantpilot_api import auth

        self.assertFalse(auth.verify_password("x", "not-a-bcrypt-hash"))
        self.assertFalse(auth.verify_password("x", 12345))


class JwtTests(unittest.TestCase):
    def test_roundtrip(self) -> None:
        from quantpilot_api import auth

        with patch.dict(os.environ, SECRET_ENV):
            token = auth.issue_token("u-1", "alice", "TRADER")
            payload = auth.verify_token(token)
        self.assertEqual(payload["sub"], "u-1")
        self.assertEqual(payload["username"], "alice")
        self.assertEqual(payload["role"], "TRADER")
        self.assertEqual(token.count("."), 2)

    def test_expired_token_rejected(self) -> None:
        from quantpilot_api import auth

        with patch.dict(os.environ, SECRET_ENV):
            token = auth.issue_token("u-1", "alice", "TRADER", ttl=-60)
            self.assertIsNone(auth.verify_token(token))

    def test_tampered_payload_rejected(self) -> None:
        from quantpilot_api import auth

        with patch.dict(os.environ, SECRET_ENV):
            head, body, sig = auth.issue_token("u-1", "alice", "VIEWER").split(".")
        raw = json.loads(base64.urlsafe_b64decode(body + "=" * (-len(body) % 4)))
        raw["role"] = "ADMIN"  # privilege escalation attempt
        forged = (
            base64.urlsafe_b64encode(json.dumps(raw, separators=(",", ":")).encode())
            .rstrip(b"=")
            .decode()
        )
        with patch.dict(os.environ, SECRET_ENV):
            self.assertIsNone(auth.verify_token(f"{head}.{forged}.{sig}"))

    def test_wrong_secret_rejected(self) -> None:
        from quantpilot_api import auth

        with patch.dict(os.environ, SECRET_ENV):
            token = auth.issue_token("u-1", "alice", "TRADER")
        with patch.dict(os.environ, {"QP_JWT_SECRET": "other-secret"}):
            self.assertIsNone(auth.verify_token(token))

    def test_alg_confusion_rejected(self) -> None:
        from quantpilot_api import auth

        with patch.dict(os.environ, SECRET_ENV):
            head, body, sig = auth.issue_token("u-1", "alice", "TRADER").split(".")
        forged = (
            base64.urlsafe_b64encode(
                json.dumps({"alg": "none", "typ": "JWT"}, separators=(",", ":")).encode()
            )
            .rstrip(b"=")
            .decode()
        )
        with patch.dict(os.environ, SECRET_ENV):
            self.assertIsNone(auth.verify_token(f"{forged}.{body}.{sig}"))

    def test_no_secret_fails_closed(self) -> None:
        from quantpilot_api import auth

        env = _env_without("QP_JWT_SECRET")
        with patch.dict(os.environ, env, clear=True):
            self.assertIsNone(auth.verify_token("a.b.c"))
            with self.assertRaises(RuntimeError):
                auth.issue_token("u-1", "alice", "ADMIN")


class UserKeyAuthTests(unittest.TestCase):
    @staticmethod
    def _row(**over):
        row = {
            "key_id": "k-1",
            "user_id": "u-1",
            "revoked_at": None,
            "username": "alice",
            "role": "TRADER",
            "is_active": True,
        }
        row.update(over)
        return row

    def test_valid_key_resolves_and_stamps_last_used(self) -> None:
        import quantpilot_api.auth as auth

        mock = MagicMock()
        mock.query_one.return_value = self._row()
        with patch.object(auth, "db", mock):
            principal = auth.authenticate_user_key("qpk_" + "a" * 48)
        self.assertIsNotNone(principal)
        self.assertEqual(principal.user_id, "u-1")
        self.assertEqual(principal.role, "TRADER")
        self.assertFalse(principal.is_service)
        self.assertIn("user_api_keys", mock.query_one.call_args[0][0])
        sql = mock.execute.call_args[0][0]
        self.assertIn("UPDATE user_api_keys", sql)
        self.assertIn("last_used_at", sql)

    def test_revoked_key_rejected_without_stamp(self) -> None:
        import quantpilot_api.auth as auth

        mock = MagicMock()
        mock.query_one.return_value = self._row(revoked_at="2026-01-01T00:00:00+00:00")
        with patch.object(auth, "db", mock):
            self.assertIsNone(auth.authenticate_user_key("qpk_" + "a" * 48))
        mock.execute.assert_not_called()

    def test_inactive_user_key_rejected(self) -> None:
        import quantpilot_api.auth as auth

        mock = MagicMock()
        mock.query_one.return_value = self._row(is_active=False)
        with patch.object(auth, "db", mock):
            self.assertIsNone(auth.authenticate_user_key("qpk_" + "a" * 48))
        mock.execute.assert_not_called()

    def test_unknown_key_rejected(self) -> None:
        import quantpilot_api.auth as auth

        mock = MagicMock()
        mock.query_one.return_value = None
        with patch.object(auth, "db", mock):
            self.assertIsNone(auth.authenticate_user_key("qpk_" + "a" * 48))

    def test_non_user_key_short_circuits_without_db(self) -> None:
        import quantpilot_api.auth as auth

        mock = MagicMock()
        with patch.object(auth, "db", mock):
            self.assertIsNone(auth.authenticate_user_key("wrong-service-key"))
        mock.query_one.assert_not_called()


class AuthorizeTests(unittest.TestCase):
    def setUp(self) -> None:
        from quantpilot_api import auth

        self.auth = auth
        self.service = auth.Principal(kind="service")
        self.users = {r: _principal(r) for r in auth.ROLES}

    def test_service_passes_everything(self) -> None:
        cases = [
            ("POST", "/api/v1/nodes/register"),
            ("POST", "/api/v1/commands"),
            ("GET", "/api/v1/auth/users"),
            ("PATCH", f"/api/v1/auth/users/{UID}"),
            ("DELETE", f"/api/v1/accounts/{UID}/orders"),
        ]
        for method, path in cases:
            self.assertIsNone(self.auth.authorize(self.service, method, path))

    def test_machine_routes_are_service_key_only(self) -> None:
        for principal in self.users.values():
            for path in (
                "/api/v1/nodes/register",
                "/api/v1/nodes/heartbeat",
                "/api/v1/config/lookup",
                "/api/v1/commands/pending",
            ):
                self.assertEqual(
                    self.auth.authorize(principal, "POST", path), "SERVICE_KEY_ONLY"
                )

    def test_reads_open_to_all_roles(self) -> None:
        for principal in self.users.values():
            for path in (
                f"/api/v1/accounts/{UID}/positions",
                "/api/v1/commands",
                "/api/v1/audit-logs",
            ):
                self.assertIsNone(self.auth.authorize(principal, "GET", path))

    def test_command_write_matrix(self) -> None:
        for role in ("VIEWER", "ANALYST"):
            self.assertEqual(
                self.auth.authorize(self.users[role], "POST", "/api/v1/commands"),
                "ROLE_FORBIDDEN",
            )
        for role in ("TRADER", "ADMIN"):
            self.assertIsNone(self.auth.authorize(self.users[role], "POST", "/api/v1/commands"))

    def test_analyst_allowances(self) -> None:
        analyst = self.users["ANALYST"]
        self.assertIsNone(self.auth.authorize(analyst, "POST", "/api/v1/backtests"))
        self.assertIsNone(
            self.auth.authorize(analyst, "POST", f"/api/v1/accounts/{UID}/ai/analyze")
        )
        self.assertEqual(
            self.auth.authorize(self.users["VIEWER"], "POST", "/api/v1/backtests"),
            "ROLE_FORBIDDEN",
        )
        # analyst-lifted routes do NOT lift account config writes
        self.assertEqual(
            self.auth.authorize(analyst, "PUT", f"/api/v1/accounts/{UID}/config"),
            "ROLE_FORBIDDEN",
        )

    def test_user_management_is_admin_only_incl_read(self) -> None:
        for principal in (self.users["TRADER"], self.users["ANALYST"], self.users["VIEWER"]):
            self.assertEqual(
                self.auth.authorize(principal, "GET", "/api/v1/auth/users"), "ADMIN_ONLY"
            )
            self.assertEqual(
                self.auth.authorize(principal, "POST", "/api/v1/auth/users"), "ADMIN_ONLY"
            )
            self.assertEqual(
                self.auth.authorize(principal, "PATCH", f"/api/v1/auth/users/{UID}"),
                "ADMIN_ONLY",
            )
        self.assertIsNone(self.auth.authorize(self.users["ADMIN"], "GET", "/api/v1/auth/users"))

    def test_self_service_open_to_all_roles(self) -> None:
        for principal in self.users.values():
            self.assertIsNone(self.auth.authorize(principal, "GET", "/api/v1/auth/me"))
            self.assertIsNone(
                self.auth.authorize(principal, "POST", "/api/v1/auth/api-keys")
            )
            self.assertIsNone(
                self.auth.authorize(principal, "DELETE", "/api/v1/auth/api-keys/k-1")
            )


class AccountIsolationTests(unittest.TestCase):
    def test_path_extraction(self) -> None:
        from quantpilot_api import auth

        self.assertEqual(auth.account_id_from_path(f"/api/v1/accounts/{UID}/positions"), UID)
        self.assertEqual(auth.account_id_from_path(f"/api/v1/accounts/{UID}"), UID)
        self.assertIsNone(auth.account_id_from_path("/api/v1/accounts"))
        self.assertIsNone(auth.account_id_from_path("/api/v1/commands"))
        self.assertIsNone(auth.account_id_from_path("/api/v1/accounts/not-a-uuid/positions"))

    def test_service_and_admin_bypass_without_db(self) -> None:
        from quantpilot_api import auth

        mock = MagicMock()
        with patch.object(auth, "db", mock):
            self.assertTrue(auth.account_granted(auth.Principal(kind="service"), UID))
            self.assertTrue(auth.account_granted(_principal("ADMIN"), UID))
        mock.query_one.assert_not_called()

    def test_user_needs_a_grant_row(self) -> None:
        from quantpilot_api import auth

        granted = MagicMock()
        granted.query_one.return_value = {"one": 1}
        denied = MagicMock()
        denied.query_one.return_value = None
        with patch.object(auth, "db", granted):
            self.assertTrue(auth.account_granted(_principal("TRADER"), UID))
        with patch.object(auth, "db", denied):
            self.assertFalse(auth.account_granted(_principal("VIEWER"), UID))
        sql, params = denied.query_one.call_args[0]
        self.assertIn("user_account_grants", sql)
        self.assertEqual(params, ("u-1", UID))


class LoginFlowTests(unittest.TestCase):
    def setUp(self) -> None:
        self.client = _client()

    @staticmethod
    def _user_row(role="TRADER", is_active=True):
        from quantpilot_api import auth

        return {
            "id": "u-1",
            "username": "alice",
            "role": role,
            "is_active": is_active,
            "password_hash": auth.hash_password("pass-word-123", rounds=4).decode(),
        }

    def _post_login(self, row, password="pass-word-123", env=SECRET_ENV):
        import quantpilot_api.auth as auth

        mock = MagicMock()
        mock.query_one.return_value = row
        with (
            patch.dict(os.environ, env),
            patch.object(auth, "db", mock),
            patch("quantpilot_api.app.writers") as writers,
        ):
            resp = self.client.post(
                "/api/v1/auth/login",
                json={"username": "alice", "password": password},
            )
        return resp, writers

    def test_success_returns_token_and_audits(self) -> None:
        resp, writers = self._post_login(self._user_row())
        self.assertEqual(resp.status_code, 200)
        data = resp.json()["data"]
        self.assertEqual(data["token"].count("."), 2)
        self.assertEqual(data["token_type"], "Bearer")
        self.assertEqual(data["user"]["username"], "alice")
        self.assertEqual(data["user"]["role"], "TRADER")
        writers.add_system_event.assert_called_once()
        self.assertEqual(writers.add_system_event.call_args[0][2], "INFO")

    def test_wrong_password_401(self) -> None:
        resp, writers = self._post_login(self._user_row(), password="nope")
        self.assertEqual(resp.status_code, 401)
        self.assertEqual(resp.json()["error"]["code"], "INVALID_CREDENTIALS")
        self.assertEqual(writers.add_system_event.call_args[0][2], "WARN")

    def test_unknown_user_401(self) -> None:
        resp, writers = self._post_login(None)
        self.assertEqual(resp.status_code, 401)
        self.assertEqual(resp.json()["error"]["code"], "INVALID_CREDENTIALS")
        self.assertEqual(writers.add_system_event.call_args[0][2], "WARN")

    def test_inactive_user_401(self) -> None:
        resp, _ = self._post_login(self._user_row(is_active=False))
        self.assertEqual(resp.status_code, 401)
        self.assertEqual(resp.json()["error"]["code"], "INVALID_CREDENTIALS")

    def test_missing_jwt_secret_503(self) -> None:
        # clear=True: the repo .env (loaded at import by config) may carry
        # QP_JWT_SECRET into os.environ; only a hard clear isolates this test
        env = _env_without("QP_JWT_SECRET")
        env["QP_API_KEY"] = "svc-key"
        with patch.dict(os.environ, env, clear=True):
            resp = self.client.post(
                "/api/v1/auth/login",
                json={"username": "alice", "password": "pass-word-123"},
            )
        self.assertEqual(resp.status_code, 503)
        self.assertEqual(resp.json()["error"]["code"], "JWT_NOT_CONFIGURED")

    def test_fail_closed_without_service_key(self) -> None:
        # login is exempt from the credential gate but NEVER from the
        # fail-closed key check (test_app_auth compatibility)
        env = _env_without("QP_API_KEY")
        with patch.dict(os.environ, env, clear=True):
            resp = self.client.post(
                "/api/v1/auth/login",
                json={"username": "alice", "password": "pass-word-123"},
            )
        self.assertEqual(resp.status_code, 503)
        self.assertEqual(resp.json()["error"]["code"], "API_KEY_NOT_CONFIGURED")


class MiddlewareTests(unittest.TestCase):
    def setUp(self) -> None:
        self.client = _client()

    @staticmethod
    def _token(role: str) -> str:
        from quantpilot_api import auth

        return auth.issue_token("u-1", "alice", role)

    def test_bearer_jwt_passes_gate(self) -> None:
        with patch.dict(os.environ, SECRET_ENV):
            resp = self.client.get(
                "/api/v1/not-a-route",
                headers={"Authorization": f"Bearer {self._token('VIEWER')}"},
            )
        self.assertEqual(resp.status_code, 404)

    def test_invalid_bearer_401(self) -> None:
        with patch.dict(os.environ, SECRET_ENV):
            resp = self.client.get(
                "/api/v1/nodes", headers={"Authorization": "Bearer garbage"}
            )
        self.assertEqual(resp.status_code, 401)
        self.assertEqual(resp.json()["error"]["code"], "UNAUTHORIZED")

    def test_expired_bearer_401(self) -> None:
        from quantpilot_api import auth

        with patch.dict(os.environ, SECRET_ENV):
            token = auth.issue_token("u-1", "alice", "TRADER", ttl=-60)
            resp = self.client.get(
                "/api/v1/nodes", headers={"Authorization": f"Bearer {token}"}
            )
        self.assertEqual(resp.status_code, 401)

    def test_viewer_cannot_issue_commands(self) -> None:
        with patch.dict(os.environ, SECRET_ENV):
            resp = self.client.post(
                "/api/v1/commands",
                json={},
                headers={"Authorization": f"Bearer {self._token('VIEWER')}"},
            )
        self.assertEqual(resp.status_code, 403)
        self.assertEqual(resp.json()["error"]["code"], "ROLE_FORBIDDEN")

    def test_trader_passes_command_gate(self) -> None:
        # reaching pydantic validation (422) proves the middleware let it through
        with patch.dict(os.environ, SECRET_ENV):
            resp = self.client.post(
                "/api/v1/commands",
                json={},
                headers={"Authorization": f"Bearer {self._token('TRADER')}"},
            )
        self.assertEqual(resp.status_code, 422)

    def test_user_key_passes_gate(self) -> None:
        import quantpilot_api.auth as auth

        mock = MagicMock()
        mock.query_one.return_value = {
            "key_id": "k-1",
            "user_id": "u-1",
            "revoked_at": None,
            "username": "alice",
            "role": "ANALYST",
            "is_active": True,
        }
        with patch.dict(os.environ, {"QP_API_KEY": "svc-key"}), patch.object(auth, "db", mock):
            resp = self.client.get(
                "/api/v1/not-a-route", headers={"X-API-Key": "qpk_" + "a" * 48}
            )
        self.assertEqual(resp.status_code, 404)
        self.assertIn("user_api_keys", mock.query_one.call_args[0][0])

    def test_machine_route_rejects_user_key(self) -> None:
        import quantpilot_api.auth as auth

        mock = MagicMock()
        mock.query_one.return_value = {
            "key_id": "k-1",
            "user_id": "u-1",
            "revoked_at": None,
            "username": "alice",
            "role": "ADMIN",
            "is_active": True,
        }
        with patch.dict(os.environ, {"QP_API_KEY": "svc-key"}), patch.object(auth, "db", mock):
            resp = self.client.post(
                "/api/v1/commands/pending", json={}, headers={"X-API-Key": "qpk_" + "a" * 48}
            )
        self.assertEqual(resp.status_code, 403)
        self.assertEqual(resp.json()["error"]["code"], "SERVICE_KEY_ONLY")

    def test_ungranted_account_403(self) -> None:
        import quantpilot_api.auth as auth

        mock = MagicMock()
        mock.query_one.return_value = None
        with patch.dict(os.environ, SECRET_ENV), patch.object(auth, "db", mock):
            resp = self.client.get(
                f"/api/v1/accounts/{UID}/positions",
                headers={"Authorization": f"Bearer {self._token('TRADER')}"},
            )
        self.assertEqual(resp.status_code, 403)
        self.assertEqual(resp.json()["error"]["code"], "ACCOUNT_NOT_GRANTED")

    def test_granted_account_reaches_route(self) -> None:
        import quantpilot_api.app as app_module
        import quantpilot_api.auth as auth

        auth_mock = MagicMock()
        auth_mock.query_one.return_value = {"one": 1}
        app_mock = MagicMock()
        app_mock.query.return_value = []
        app_mock.jsonable.side_effect = lambda value: value
        with (
            patch.dict(os.environ, SECRET_ENV),
            patch.object(auth, "db", auth_mock),
            patch.object(app_module, "db", app_mock),
        ):
            resp = self.client.get(
                f"/api/v1/accounts/{UID}/positions",
                headers={"Authorization": f"Bearer {self._token('TRADER')}"},
            )
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.json()["data"], [])

    def test_user_list_accounts_filters_to_grants(self) -> None:
        import quantpilot_api.app as app_module

        app_mock = MagicMock()
        app_mock.query.return_value = []
        app_mock.jsonable.side_effect = lambda value: value
        with patch.dict(os.environ, SECRET_ENV), patch.object(app_module, "db", app_mock):
            resp = self.client.get(
                "/api/v1/accounts",
                headers={"Authorization": f"Bearer {self._token('VIEWER')}"},
            )
        self.assertEqual(resp.status_code, 200)
        sql, params = app_mock.query.call_args[0]
        self.assertIn("user_account_grants", sql)
        self.assertEqual(params, ("u-1",))

    def test_service_list_accounts_sees_all(self) -> None:
        import quantpilot_api.app as app_module

        app_mock = MagicMock()
        app_mock.query.return_value = []
        app_mock.jsonable.side_effect = lambda value: value
        with patch.dict(os.environ, {"QP_API_KEY": "svc-key"}), patch.object(
            app_module, "db", app_mock
        ):
            resp = self.client.get("/api/v1/accounts", headers={"X-API-Key": "svc-key"})
        self.assertEqual(resp.status_code, 200)
        self.assertNotIn("user_account_grants", app_mock.query.call_args[0][0])


class AdminUserRoutesTests(unittest.TestCase):
    def setUp(self) -> None:
        self.client = _client()

    @staticmethod
    def _token(role: str) -> str:
        from quantpilot_api import auth

        return auth.issue_token("u-9", "root", role)

    def test_viewer_cannot_list_users(self) -> None:
        with patch.dict(os.environ, SECRET_ENV):
            resp = self.client.get(
                "/api/v1/auth/users",
                headers={"Authorization": f"Bearer {self._token('VIEWER')}"},
            )
        self.assertEqual(resp.status_code, 403)
        self.assertEqual(resp.json()["error"]["code"], "ADMIN_ONLY")

    def test_admin_lists_users_with_grants(self) -> None:
        import quantpilot_api.app as app_module

        app_mock = MagicMock()
        app_mock.query.side_effect = [
            [
                {
                    "user_id": "u-1",
                    "username": "alice",
                    "role": "TRADER",
                    "is_active": True,
                    "created_at": "2026-10-31T00:00:00+00:00",
                }
            ],
            [{"user_id": "u-1", "account_id": UID}],
        ]
        app_mock.jsonable.side_effect = lambda value: value
        with patch.dict(os.environ, SECRET_ENV), patch.object(app_module, "db", app_mock):
            resp = self.client.get(
                "/api/v1/auth/users",
                headers={"Authorization": f"Bearer {self._token('ADMIN')}"},
            )
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.json()["data"][0]["account_ids"], [UID])

    def test_admin_creates_user_and_grants(self) -> None:
        import quantpilot_api.app as app_module

        app_mock = MagicMock()
        app_mock.jsonable.side_effect = lambda value: value
        app_mock.query_one.side_effect = [
            None,  # username-uniqueness pre-check: not taken
            {
                "user_id": "u-2",
                "username": "bob",
                "role": "TRADER",
                "is_active": True,
                "created_at": "2026-10-31T00:00:00+00:00",
            },
        ]
        with (
            patch.dict(os.environ, SECRET_ENV),
            patch.object(app_module, "db", app_mock),
            patch("quantpilot_api.app.writers") as writers,
        ):
            resp = self.client.post(
                "/api/v1/auth/users",
                json={
                    "username": "bob",
                    "password": "long-enough-1",
                    "role": "TRADER",
                    "account_ids": [UID],
                },
                headers={"Authorization": f"Bearer {self._token('ADMIN')}"},
            )
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.json()["data"]["username"], "bob")
        self.assertEqual(resp.json()["data"]["account_ids"], [UID])
        # no plaintext password in any SQL parameter
        for call in app_mock.query_one.call_args_list + app_mock.execute.call_args_list:
            for param in call[0][1] if len(call[0]) > 1 else ():
                self.assertNotEqual(param, "long-enough-1")
                if isinstance(param, str):
                    self.assertFalse(param.startswith("$2") is True and param == "long-enough-1")
        grant_calls = [c for c in app_mock.execute.call_args_list if "user_account_grants" in c[0][0]]
        self.assertEqual(len(grant_calls), 1)
        writers.add_system_event.assert_called_once()


class ApiKeyRoutesTests(unittest.TestCase):
    """Self-service key minting: plaintext exactly once, metadata-only lists."""

    def setUp(self) -> None:
        self.client = _client()

    @staticmethod
    def _token(role: str) -> str:
        from quantpilot_api import auth

        return auth.issue_token("u-1", "alice", role)

    def test_service_key_cannot_mint_user_keys(self) -> None:
        with patch.dict(os.environ, {"QP_API_KEY": "svc-key"}):
            resp = self.client.post(
                "/api/v1/auth/api-keys", json={"label": "l"}, headers={"X-API-Key": "svc-key"}
            )
        self.assertEqual(resp.status_code, 403)
        self.assertEqual(resp.json()["error"]["code"], "USER_ONLY")

    def test_create_passes_row_through_jsonable(self) -> None:
        # regression: the INSERT row carries a datetime (created_at); the
        # route MUST serialize through db.jsonable or the response 500s.
        from datetime import datetime

        import quantpilot_api.app as app_module

        app_mock = MagicMock()
        seen: list[dict] = []
        # jsonable must intercept the row BEFORE it reaches JSONResponse; a
        # passthrough mock would let the datetime through and 500 again
        app_mock.jsonable.side_effect = lambda value: seen.append(value) or "serialized"
        app_mock.query_one.return_value = {
            "key_id": "k-1",
            "key_prefix": "qpk_abcd",
            "label": "l",
            "created_at": datetime(2026, 10, 31, 12, 0, 0),
        }
        with (
            patch.dict(os.environ, SECRET_ENV),
            patch.object(app_module, "db", app_mock),
            patch("quantpilot_api.app.writers"),
        ):
            resp = self.client.post(
                "/api/v1/auth/api-keys",
                json={"label": "l"},
                headers={"Authorization": f"Bearer {self._token('VIEWER')}"},
            )
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(len(seen), 1)
        self.assertIsInstance(seen[0]["created_at"], datetime)
        self.assertTrue(seen[0]["key"].startswith("qpk_"))


if __name__ == "__main__":
    unittest.main()
