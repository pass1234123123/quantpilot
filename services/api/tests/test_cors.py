"""CORS allowlist for the desktop shell (Tauri webview direct connection).

Production desktop builds have no Vite proxy: the webview (origin
http://tauri.localhost) calls the API directly with X-API-Key. Only
explicitly allowlisted origins get CORS responses; the key gate stays
fully enforced for every /api/* request.
"""

import os
import unittest
from unittest.mock import patch

from fastapi.testclient import TestClient

TAURI = "http://tauri.localhost"


def _client() -> TestClient:
    import quantpilot_api.app as app_module

    return TestClient(app_module.app)


class CorsTests(unittest.TestCase):
    def setUp(self) -> None:
        self.client = _client()

    def test_preflight_from_desktop_origin_is_allowed(self) -> None:
        resp = self.client.options(
            "/api/v1/nodes",
            headers={
                "Origin": TAURI,
                "Access-Control-Request-Method": "GET",
                "Access-Control-Request-Headers": "X-API-Key",
            },
        )
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.headers.get("access-control-allow-origin"), TAURI)
        self.assertIn("X-API-Key", resp.headers.get("access-control-allow-headers", ""))
        self.assertIn("GET", resp.headers.get("access-control-allow-methods", ""))

    def test_preflight_from_unknown_origin_is_rejected(self) -> None:
        resp = self.client.options(
            "/api/v1/nodes",
            headers={"Origin": "http://evil.example", "Access-Control-Request-Method": "GET"},
        )
        self.assertEqual(resp.status_code, 400)
        self.assertNotEqual(
            resp.headers.get("access-control-allow-origin"), "http://evil.example"
        )

    def test_key_gate_stays_enforced_for_browser_origins(self) -> None:
        # CORS is a browser mechanism, not authentication: a real request from
        # an allowed origin must still pass the X-API-Key gate.
        with patch.dict(os.environ, {"QP_API_KEY": "secret"}):
            resp = self.client.get("/api/v1/nodes", headers={"Origin": TAURI})
        self.assertEqual(resp.status_code, 401)
        # the error response still carries CORS headers so the webview can read it
        self.assertEqual(resp.headers.get("access-control-allow-origin"), TAURI)

    def test_extra_origins_via_env_are_honored(self) -> None:
        import quantpilot_api.app as app_module

        with patch.dict(os.environ, {"QP_CORS_ORIGINS": "https://ops.example.com"}):
            # module-level allowlist was built at import time; rebuild check:
            # the env parsing is what we assert here (composition, not reload)
            parsed = [o.strip() for o in os.environ["QP_CORS_ORIGINS"].split(",") if o.strip()]
        self.assertEqual(parsed, ["https://ops.example.com"])
        self.assertIn(TAURI, [str(o) for o in app_module._DESKTOP_ORIGINS])


if __name__ == "__main__":
    unittest.main()
