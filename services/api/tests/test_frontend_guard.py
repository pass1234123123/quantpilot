"""R2 acceptance test #15: frontend source-level guards.

The frontend slice has no unit-test runner wired up (no vitest), so the
mandated check "Tauri JWT never lands in localStorage" - plus the P1-01
no-service-key-fallback rule and the T6 login gate - is enforced as a
source scan over frontend/src. If a refactor regresses any of these
invariants this suite fails the same way a backend test would.
"""

from __future__ import annotations

import unittest
from pathlib import Path

FRONTEND = Path(__file__).resolve().parents[3] / "frontend"
FRONTEND_SRC = FRONTEND / "src"


def _read(rel: str) -> str:
    path = FRONTEND_SRC / rel
    assert path.exists(), f"missing frontend source: {rel}"
    return path.read_text(encoding="utf-8")


class FrontendTokenStorageTests(unittest.TestCase):
    """#15: the session JWT must never live in localStorage."""

    def test_no_token_persistence_in_localstorage(self):
        offenders: list[str] = []
        for p in sorted(FRONTEND_SRC.rglob("*")):
            if p.suffix not in (".ts", ".vue"):
                continue
            text = p.read_text(encoding="utf-8", errors="replace")
            for needle in (
                "localStorage.getItem('qp.token'",
                "localStorage.setItem('qp.token'",
                "localStorage.getItem('qp.user'",
                "localStorage.setItem('qp.user'",
            ):
                if needle in text:
                    offenders.append(f"{p.name}: {needle}")
        self.assertEqual(offenders, [], "JWT / user snapshot persisted in localStorage")

    def test_secure_store_routes_token_to_credential_manager(self):
        store_ts = _read("secure-store.ts")
        # desktop: OS credential store via the Rust bridge
        self.assertIn("TOKEN_STORE_KEY = 'auth_token'", store_ts)
        self.assertIn("'credential_set', { key: TOKEN_STORE_KEY", store_ts)
        self.assertIn("'credential_delete', { key: TOKEN_STORE_KEY", store_ts)
        # browser/dev fallback: sessionStorage (dies with the tab), not localStorage
        self.assertIn("sessionStorage.setItem(SESSION_TOKEN_KEY", store_ts)
        # one-time migration wipes the legacy localStorage copy
        self.assertIn("localStorage.removeItem(LEGACY_TOKEN_KEY)", store_ts)


class FrontendNoServiceKeyFallbackTests(unittest.TestCase):
    """#12/#15: desktop users never degrade into the shared service key."""

    def test_api_client_sends_personal_credentials_only(self):
        api_ts = _read("api.ts")
        # personal qpk_ keys pass; any other key (the shared service key)
        # is simply not sent, so the backend answers 401 -> login gate.
        self.assertIn("key.startsWith('qpk_')", api_ts)
        self.assertNotIn("localStorage.getItem('qp.token')", api_ts)
        self.assertIn("setAuthRequiredListener", api_ts)
        # expired/invalid session re-opens the gate instead of falling back
        self.assertIn("authRequiredListener?.()", api_ts)

    def test_vite_proxy_no_longer_injects_service_key(self):
        config = (FRONTEND / "vite.config.ts").read_text(encoding="utf-8")
        self.assertNotIn("'X-API-Key'", config)

    def test_settings_panel_has_no_login_form(self):
        panel = _read("components/panels/SettingsPanel.vue")
        self.assertNotIn("doLogin", panel)
        self.assertNotIn("api.login", panel)


class FrontendLoginGateTests(unittest.TestCase):
    """T6: the app boots into a login gate, not into business views."""

    def test_app_shell_gates_on_verified_session(self):
        app_vue = _read("App.vue")
        self.assertIn("LoginView", app_vue)
        self.assertIn("verifySession", app_vue)
        self.assertIn('v-else-if="!authUser"', app_vue)

    def test_login_view_exists_and_emits_identity(self):
        login = _read("components/LoginView.vue")
        self.assertIn("emit('success', user)", login)
        self.assertIn("api.login", login)

    def test_role_gated_buttons_cover_every_write_panel(self):
        # strategy/config writes need ADMIN/TRADER; research writes
        # (backtests, AI analyze) additionally accept ANALYST.
        for rel, fn in (
            ("components/panels/StrategiesPanel.vue", "canWrite"),
            ("components/panels/FilteringPanel.vue", "canWrite"),
            ("components/panels/ExpertPanel.vue", "canWrite"),
            ("components/panels/BacktestsPanel.vue", "canResearch"),
            ("components/panels/AiPanel.vue", "canResearch"),
        ):
            text = _read(rel)
            self.assertIn(fn, text, f"{rel} lost its role gate")
            self.assertIn(":disabled", text, f"{rel} lost its disabled gate")


if __name__ == "__main__":
    unittest.main()
