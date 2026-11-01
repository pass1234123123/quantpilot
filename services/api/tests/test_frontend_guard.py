"""R2 acceptance test #15: frontend source-level guards.

The frontend slice has no unit-test runner wired up (no vitest), so the
mandated check "Tauri JWT never lands in localStorage" - plus the P1-01
no-service-key-fallback rule, the T6 login gate and the R3.1 HTTPS /
Redis-config hardening - is enforced as a source scan over frontend/src.
If a refactor regresses any of these invariants this suite fails the
same way a backend test would.
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
        # R3 P1-02: the prefix check runs on the trimmed key inside
        # loginWithApiKey and rejects shared service keys outright.
        self.assertIn("trimmed.startsWith('qpk_')", api_ts)
        self.assertNotIn("localStorage.getItem('qp.token')", api_ts)
        self.assertIn("setAuthRequiredListener", api_ts)
        # expired/invalid session re-opens the gate instead of falling back
        self.assertIn("authRequiredListener?.()", api_ts)
        # R3: login-key rides its own credential channel; the retired R2
        # settings-slot accessors must stay out of the API client.
        self.assertIn("X-API-Key", api_ts)
        self.assertNotIn("getApiKey", api_ts)
        self.assertNotIn("setApiKey", api_ts)

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
        # R3 P1-01: four-state gate (boot -> setup -> auth -> app); the
        # first-run connection page gates the login gate itself so a
        # fresh desktop install can never hit a relative /api request.
        self.assertIn("ConnectionSetup", app_vue)
        self.assertIn("gate === 'setup'", app_vue)
        self.assertIn("gate === 'auth'", app_vue)
        self.assertIn("gate === 'app'", app_vue)
        self.assertIn("LoginView", app_vue)
        self.assertIn("verifySession", app_vue)

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


class R31SecurityHardeningTests(unittest.TestCase):
    """R3.1: remote-HTTP rejection, shared validator, unified config name,
    atomic Redis failure recording."""

    def test_remote_http_is_rejected(self):
        api_ts = _read("api.ts")
        # loopback whitelist: localhost / 127.0.0.1 / ::1 (bracketed or not)
        self.assertIn(
            "LOCAL_HOSTS = new Set(['localhost', '127.0.0.1', '::1', '[::1]'])",
            api_ts,
        )
        # every other host on http:// is refused before any network attempt
        self.assertIn(
            "url.protocol === 'http:' && !LOCAL_HOSTS.has(url.hostname)",
            api_ts,
        )
        self.assertIn("远程地址必须使用 https://", api_ts)

    def test_tls_verification_has_no_escape_hatch(self):
        offenders: list[str] = []
        for p in sorted(FRONTEND_SRC.rglob("*")):
            if p.suffix not in (".ts", ".vue"):
                continue
            text = p.read_text(encoding="utf-8", errors="replace")
            for needle in (
                "rejectUnauthorized",
                "NODE_TLS_REJECT_UNAUTHORIZED",
                "reject-unauthorized",
            ):
                if needle in text:
                    offenders.append(f"{p.name}: {needle}")
        # the Tauri shell must not bypass certificate checks either
        for p in sorted((FRONTEND / "src-tauri").rglob("*.rs")):
            text = p.read_text(encoding="utf-8", errors="replace")
            for needle in ("danger_accept_invalid_certs", "accept_invalid_certs"):
                if needle in text:
                    offenders.append(f"{p.name}: {needle}")
        self.assertEqual(offenders, [], "TLS verification was relaxed somewhere")

    def test_both_pages_share_one_validator(self):
        for rel in (
            "components/ConnectionSetup.vue",
            "components/panels/SettingsPanel.vue",
        ):
            text = _read(rel)
            self.assertIn("validateApiBase", text, f"{rel} must use the shared validator")
            self.assertIn("testApiBase", text, f"{rel} must probe before saving")
            self.assertNotIn("new URL(", text, f"{rel} must not roll its own URL parsing")

    def test_one_redis_config_name_everywhere(self):
        api_src = Path(__file__).resolve().parents[1] / "src"
        offenders: list[str] = []
        for p in sorted(api_src.rglob("*.py")):
            text = p.read_text(encoding="utf-8", errors="replace")
            if "QP_REDIS_URL" in text:
                offenders.append(p.name)
        self.assertEqual(offenders, [], "the retired QP_REDIS_URL key is still referenced")
        auth_py = (api_src / "quantpilot_api" / "auth.py").read_text(encoding="utf-8")
        self.assertIn("QP_LOGIN_THROTTLE_MODE", auth_py)
        self.assertIn("QP_LOGIN_THROTTLE_REDIS_URL", auth_py)
        env_example = (Path(__file__).resolve().parents[3] / ".env.example").read_text(
            encoding="utf-8"
        )
        self.assertIn("QP_LOGIN_THROTTLE_MODE", env_example)
        self.assertIn("QP_LOGIN_THROTTLE_REDIS_URL", env_example)

    def test_attempt_reservation_is_one_atomic_script(self):
        throttle = (
            Path(__file__).resolve().parents[1]
            / "src"
            / "quantpilot_api"
            / "login_throttle.py"
        ).read_text(encoding="utf-8")
        # R3.3 P1-01: the limit check and the slot spend are ONE Lua
        # call (trim + ZCARD check + ZADD). The R3.2 acceptance caught
        # the old design checking first and incrementing only after the
        # slow bcrypt check - 17 concurrent verifications slipped past a
        # limit of 10.
        self.assertIn("_RESERVE_LUA", throttle)
        self.assertIn("ZREMRANGEBYSCORE", throttle)
        # R3.3.1: a reservation starts PENDING - the pair member carries
        # the "p:" state prefix so a later success can tell in-flight
        # reservations apart from committed failure records
        self.assertIn(
            "redis.call('ZADD', KEYS[1], now, 'p:' .. ARGV[3])", throttle
        )
        # a blocked verdict must add nothing (no partial spend)
        self.assertIn("return 0", throttle)
        # R3.3 P2-01: true sliding window - each attempt ages out via its
        # own score, never a whole-bucket TTL refresh per failure
        self.assertNotIn("redis.call('INCR'", throttle)
        self.assertNotIn("redis.call('EXPIRE'", throttle)
        # R3.3.1: a wrong password COMMITS its own reservation with one
        # atomic rename p:{id} -> f:{id}, score preserved (attempt-time
        # window anchor), idempotent
        self.assertIn("_COMMIT_LUA", throttle)
        self.assertIn("local score = redis.call('ZSCORE', KEYS[1], pending)", throttle)
        self.assertIn("redis.call('ZADD', KEYS[1], score, failed)", throttle)
        # R3.3.1: a success must NEVER delete whole keys - the old
        # pipe.delete(pair_key) destroyed other concurrently verifying
        # requests' reservations, so their later failures vanished
        # (the R3.3 acceptance race). The settle is one atomic script
        # that removes ONLY committed "f:" members plus the attempt's
        # OWN pending member and its own spray slot.
        self.assertIn("_SETTLE_SUCCESS_LUA", throttle)
        self.assertNotIn("pipe.delete(pair_key)", throttle)
        self.assertNotIn("redis.call('DEL'", throttle)
        self.assertIn("string.sub(members[i], 1, 2) == 'f:'", throttle)
        # R3.3 P1-01: a success/release gives back its OWN spray slot,
        # never the whole ip bucket
        self.assertIn("pipe.zrem(ip_key, attempt_id)", throttle)
        # R3.3.1: an abort returns only its OWN pending member
        self.assertIn('pipe.zrem(pair_key, f"p:{attempt_id}")', throttle)
        # R3.1 P1 #9: hostile oversized inputs cannot bloat Redis keys
        self.assertIn("_KEY_PART_MAX", throttle)
        # R3.1 P1 #4: redis_required mode refuses instead of degrading
        self.assertIn("class LoginThrottleUnavailable", throttle)
        # R3.3.1: a failure that cannot be recorded in the shared budget
        # fails CLOSED (raises) - a dropped record under-counts
        self.assertIn("def commit_failure", throttle)


if __name__ == "__main__":
    unittest.main()
