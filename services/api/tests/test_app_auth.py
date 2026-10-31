"""X-API-Key service auth: every /api/* route is gated, /healthz stays public.

Fail-closed (PRD 14): without QP_API_KEY configured the API refuses all API
traffic (503) instead of silently running open. The middleware reads the
environment on every request so the key can be rotated without a restart and
tests can patch it per-case.
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

    def test_correct_key_reaches_the_router(self) -> None:
        with patch.dict(os.environ, {"QP_API_KEY": "secret"}):
            resp = self.client.get(
                "/api/v1/definitely-not-a-route", headers={"X-API-Key": "secret"}
            )
        # passes the gate, then FastAPI 404s the unknown route (NOT 401)
        self.assertEqual(resp.status_code, 404)

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
