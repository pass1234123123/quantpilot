"""R3.2 P1: login-throttle configuration must fail the boot loudly.

A typo in QP_LOGIN_THROTTLE_MODE (e.g. 'redis_require') or a missing URL
in redis_required mode is a STARTUP error - never a silent fallback to
memory_only that quietly drops a multi-instance deployment to
per-instance counting.

The eight mandated scenarios:
  1 mode unset             explicit memory_only default, boot OK
  2 memory_only            boot OK + one SECURITY warning
  3 redis_required + URL   boot OK (shared counting armed)
  4 unknown mode           LoginThrottleConfigError ('redis_require' typo)
  5 redis_required no URL  startup failure; runtime logins still rejected
  6 Redis down at boot     startup SUCCEEDS; logins answer 503
  7 Redis drops mid-run    logins keep answering 503 (never degrade)
  8 Redis recovers         shared counting resumes automatically

Plus wiring (9-10): the gate runs first inside the app lifespan, so a
bad config aborts the boot before the service accepts a single request,
and a source guard pins the no-silent-fallback rule.
"""

import os
import unittest
from unittest.mock import MagicMock, patch

import fakeredis
import redis as redis_lib
from fastapi.testclient import TestClient

from rbac_testkit import SECRET_ENV


def _env(**overrides) -> dict:
    """Host environment with every QP_LOGIN_THROTTLE_* variable removed,
    then the requested overrides applied (tests are hermetic regardless
    of the host's .env)."""
    env = {
        k: v
        for k, v in os.environ.items()
        if k
        not in (
            "QP_LOGIN_THROTTLE_MODE",
            "QP_LOGIN_THROTTLE_REDIS_URL",
            "QP_REDIS_URL",
        )
    }
    env.update(overrides)
    return env


def _fake_redis_env(server):
    """Point login_throttle's Redis.from_url at a shared fakeredis server."""
    from quantpilot_api import login_throttle

    def fake_from_url(url, **_kwargs):
        return fakeredis.FakeStrictRedis(server=server, decode_responses=True)

    return patch.object(login_throttle.redis.Redis, "from_url", fake_from_url)


class ThrottleConfigGateTests(unittest.TestCase):
    """Scenarios 1-6: validate_login_throttle_config verdicts."""

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

    def test_1_unset_mode_defaults_to_memory_only(self) -> None:
        # unset variable -> the explicit default, never an error
        with patch.dict(os.environ, _env(), clear=True):
            self.assertEqual(self.auth._throttle_mode(), "memory_only")
            self.auth.validate_login_throttle_config()  # must not raise

    def test_2_memory_only_boots_with_security_warning(self) -> None:
        # requirement #5: memory_only is explicitly flagged as weaker
        with (
            patch.dict(
                os.environ,
                _env(QP_LOGIN_THROTTLE_MODE="memory_only"),
                clear=True,
            ),
            self.assertLogs("quantpilot.auth", level="WARNING") as logs,
        ):
            self.auth.validate_login_throttle_config()
        self.assertTrue(
            any("SECURITY" in line for line in logs.output),
            logs.output,
        )

    def test_3_redis_required_with_url_boots(self) -> None:
        with (
            patch.dict(
                os.environ,
                _env(
                    QP_LOGIN_THROTTLE_MODE="redis_required",
                    QP_LOGIN_THROTTLE_REDIS_URL="redis://127.0.0.1:6399/0",
                ),
                clear=True,
            ),
            self.assertNoLogs("quantpilot.auth", level="WARNING"),
        ):
            self.auth.validate_login_throttle_config()  # must not raise

    def test_4_unknown_mode_is_a_config_error(self) -> None:
        # the typo from the acceptance report ('redis_require') and other
        # unknown values must fail - no silent memory_only fallback
        for bad in ("redis_require", "memory-only", "redis", "cluster"):
            with (
                patch.dict(
                    os.environ, _env(QP_LOGIN_THROTTLE_MODE=bad), clear=True
                ),
                self.assertRaises(
                    self.auth.LoginThrottleConfigError, msg=bad
                ),
            ):
                self.auth.validate_login_throttle_config()

    def test_5a_redis_required_without_url_fails_startup(self) -> None:
        with (
            patch.dict(
                os.environ,
                _env(QP_LOGIN_THROTTLE_MODE="redis_required"),
                clear=True,
            ),
            self.assertRaises(self.auth.LoginThrottleConfigError),
        ):
            self.auth.validate_login_throttle_config()

    def test_5b_required_without_url_still_rejects_logins(self) -> None:
        # runtime behavior: 503, never a memory_only fallback
        from quantpilot_api.auth import LoginThrottleUnavailable

        env = _env(
            QP_LOGIN_THROTTLE_MODE="redis_required",
            QP_LOGIN_THROTTLE_REDIS_URL="",
        )
        with patch.dict(os.environ, env, clear=True):
            with self.assertRaises(LoginThrottleUnavailable):
                self.auth.login_too_many_attempts("nina", "1.2.3.4")

    def test_6_dead_redis_boots_but_rejects_logins(self) -> None:
        # Redis being down is NOT a config error: the API starts, but
        # password logins answer 503 until it recovers
        from quantpilot_api.auth import LoginThrottleUnavailable

        env = _env(
            QP_LOGIN_THROTTLE_MODE="redis_required",
            QP_LOGIN_THROTTLE_REDIS_URL="redis://127.0.0.1:1/0",  # nothing listens
        )
        with patch.dict(os.environ, env, clear=True):
            self.auth.validate_login_throttle_config()  # boot OK
            with self.assertRaises(LoginThrottleUnavailable):
                self.auth.login_too_many_attempts("olga", "5.6.7.8")


class OutageRecoveryTests(unittest.TestCase):
    """Scenarios 7-8: a mid-run outage keeps rejecting logins; recovery
    resumes shared counting without a process restart (the 60 s
    stand-down window expires and the shared verdict returns)."""

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

    def _required_env(self) -> dict:
        return _env(
            QP_LOGIN_THROTTLE_MODE="redis_required",
            QP_LOGIN_THROTTLE_REDIS_URL="redis://shared/0",
        )

    def test_7_outage_mid_run_keeps_rejecting(self) -> None:
        from quantpilot_api.auth import LoginThrottleUnavailable

        server = fakeredis.FakeServer()
        with (
            patch.dict(os.environ, self._required_env(), clear=True),
            _fake_redis_env(server),
        ):
            self.assertFalse(self.auth.login_too_many_attempts("paul", "9.9.9.9"))
            # Redis "drops": every command now fails
            throttle = self.auth._throttle()
            with patch.object(
                throttle._client,
                "get",
                side_effect=redis_lib.exceptions.ConnectionError("down"),
            ):
                with self.assertRaises(LoginThrottleUnavailable):
                    self.auth.login_too_many_attempts("paul", "9.9.9.9")
            # stand-down window: still refusing, never degrading
            with self.assertRaises(LoginThrottleUnavailable):
                self.auth.login_too_many_attempts("paul", "9.9.9.9")

    def test_8_recovery_resumes_shared_counting(self) -> None:
        from quantpilot_api.auth import LoginThrottleUnavailable

        server = fakeredis.FakeServer()
        with (
            patch.dict(os.environ, self._required_env(), clear=True),
            patch.object(self.auth, "_LOGIN_MAX_FAILURES", 3),
            _fake_redis_env(server),
        ):
            for _ in range(3):
                self.auth.record_login_failure("tina", "3.3.3.3")
            # fresh in-process state: only the SHARED counter remains
            self.auth._login_failures.clear()
            self.auth._login_failures_ip.clear()
            throttle = self.auth._throttle()
            # outage -> 503 (twice: error + stand-down window)
            with patch.object(
                throttle._client,
                "get",
                side_effect=redis_lib.exceptions.ConnectionError("down"),
            ):
                with self.assertRaises(LoginThrottleUnavailable):
                    self.auth.login_too_many_attempts("tina", "3.3.3.3")
            self.assertIsNone(throttle.too_many_attempts("tina", "3.3.3.3"))
            # window over -> "recovered": the SHARED verdict returns
            throttle._degraded_until = 0.0
            self.assertTrue(self.auth.login_too_many_attempts("tina", "3.3.3.3"))


class LifespanGateWiringTests(unittest.TestCase):
    """Scenarios 9-11: the gate is wired into the app lifespan FIRST - a
    bad config aborts the boot (nothing else starts), a valid one boots
    cleanly, and a dead Redis at boot yields 503 logins over HTTP."""

    def test_9_lifespan_aborts_boot_on_bad_mode(self) -> None:
        import quantpilot_api.app as app_module
        import quantpilot_api.auth as auth

        env = _env(
            **SECRET_ENV,
            QP_LOGIN_THROTTLE_MODE="redis_require",  # the typo from the report
        )
        with (
            patch.dict(os.environ, env, clear=True),
            patch.object(app_module.db, "init_pool") as init_pool,
            patch.object(app_module, "NodeEventsConsumer"),
            patch.object(app_module, "CommandSweeper"),
        ):
            with self.assertRaises(auth.LoginThrottleConfigError):
                with TestClient(app_module.app):
                    pass
        # the gate runs before anything else starts
        init_pool.assert_not_called()

    def test_10_lifespan_aborts_boot_on_missing_url(self) -> None:
        import quantpilot_api.app as app_module
        import quantpilot_api.auth as auth

        env = _env(
            **SECRET_ENV,
            QP_LOGIN_THROTTLE_MODE="redis_required",
            QP_LOGIN_THROTTLE_REDIS_URL="",
        )
        with (
            patch.dict(os.environ, env, clear=True),
            patch.object(app_module.db, "init_pool") as init_pool,
            patch.object(app_module, "NodeEventsConsumer"),
            patch.object(app_module, "CommandSweeper"),
        ):
            with self.assertRaises(auth.LoginThrottleConfigError):
                with TestClient(app_module.app):
                    pass
        init_pool.assert_not_called()

    def test_11_http_boot_with_dead_redis_logins_answer_503(self) -> None:
        import quantpilot_api.app as app_module

        env = _env(
            **SECRET_ENV,
            QP_LOGIN_THROTTLE_MODE="redis_required",
            QP_LOGIN_THROTTLE_REDIS_URL="redis://127.0.0.1:1/0",
        )
        with (
            patch.dict(os.environ, env, clear=True),
            patch.object(app_module, "db", MagicMock()),
            patch.object(app_module, "NodeEventsConsumer"),
            patch.object(app_module, "CommandSweeper"),
            patch("quantpilot_api.app.writers"),
        ):
            with TestClient(app_module.app) as client:
                resp = client.post(
                    "/api/v1/auth/login",
                    json={"username": "quinn", "password": "whatever"},
                )
        self.assertEqual(resp.status_code, 503, resp.text)
        self.assertEqual(resp.json()["error"]["code"], "LOGIN_THROTTLE_UNAVAILABLE")


class NoSilentFallbackSourceGuard(unittest.TestCase):
    """The R3.1 flaw (unknown mode -> memory_only) must never come back."""

    def test_throttle_mode_parser_has_no_silent_fallback(self) -> None:
        import inspect

        import quantpilot_api.auth as auth

        source = inspect.getsource(auth._throttle_mode)
        # memory_only may only be returned as the explicit default for an
        # unset/empty variable - never as the fallback for an unknown one
        self.assertNotIn('else "memory_only"', source)
        self.assertNotIn("else 'memory_only'", source)
        # and an unknown value must raise
        self.assertIn("LoginThrottleConfigError", source)


if __name__ == "__main__":
    unittest.main()
