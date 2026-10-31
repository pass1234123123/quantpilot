"""Service-key gate: every /api/* route is gated, /healthz stays public.

Fail-closed (PRD 14): without QP_API_KEY configured the API refuses all API
traffic (503) instead of silently running open. The middleware reads the
environment on every request so the key can be rotated without a restart and
tests can patch it per-case.

R2 remediation (P1-01): the service key is a MACHINE lane. It carries agent/
EA traffic on the whitelisted machine paths only; business routes demand a
named user (403 USER_IDENTITY_REQUIRED).
"""

import os
import unittest
from unittest.mock import patch

from fastapi.testclient import TestClient


def _client() -> TestClient:
    import quantpilot_api.app as app_module

    return TestClient(app_module.app)


class ApiKeyAuthTests(unittest.TestCase):
    def setUp(self) -> None:
        self.client = _client()

    def test_request_without_key_is_rejected(self) -> None:
        with patch.dict(os.environ, {"QP_API_KEY": "secret"}):
            resp = self.client.get("/api/v1/nodes")
        self.assertEqual(resp.status_code, 401)
        self.assertEqual(resp.json()["error"]["code"], "UNAUTHORIZED")

    def test_wrong_key_is_rejected(self) -> None:
        with patch.dict(os.environ, {"QP_API_KEY": "secret"}):
            resp = self.client.get("/api/v1/nodes", headers={"X-API-Key": "wrong"})
        self.assertEqual(resp.status_code, 401)
        self.assertEqual(resp.json()["error"]["code"], "UNAUTHORIZED")

    def test_service_key_passes_on_machine_routes(self) -> None:
        # P1-01: the service key is the machine lane; POST /commands/pending
        # is agent traffic. An empty body reaches the ROUTER (pydantic 422),
        # proving the gate let machine traffic through.
        with patch.dict(os.environ, {"QP_API_KEY": "secret"}):
            resp = self.client.post(
                "/api/v1/commands/pending", json={}, headers={"X-API-Key": "secret"}
            )
        self.assertEqual(resp.status_code, 422)

    def test_service_key_rejected_on_business_routes(self) -> None:
        # P1-01: the same key on ANY business path is refused before the
        # router - even for a path that does not exist (403, not 404).
        with patch.dict(os.environ, {"QP_API_KEY": "secret"}):
            resp = self.client.get(
                "/api/v1/definitely-not-a-route", headers={"X-API-Key": "secret"}
            )
        self.assertEqual(resp.status_code, 403)
        self.assertEqual(resp.json()["error"]["code"], "USER_IDENTITY_REQUIRED")

    def test_server_without_configured_key_fails_closed(self) -> None:
        env = {k: v for k, v in os.environ.items() if k != "QP_API_KEY"}
        with patch.dict(os.environ, env, clear=True):
            resp = self.client.get("/api/v1/nodes", headers={"X-API-Key": "anything"})
        self.assertEqual(resp.status_code, 503)
        self.assertEqual(resp.json()["error"]["code"], "API_KEY_NOT_CONFIGURED")

    def test_healthz_stays_public_without_key(self) -> None:
        env = {k: v for k, v in os.environ.items() if k != "QP_API_KEY"}
        with patch.dict(os.environ, env, clear=True):
            resp = self.client.get("/healthz")
        # reaches the health route; db-unreachable 503 is expected without a pool
        self.assertIn(resp.status_code, (200, 503))
        if resp.status_code == 503:
            self.assertEqual(resp.json()["error"]["code"], "UNHEALTHY")


if __name__ == "__main__":
    unittest.main()
