"""PRD 9.5 six-state circuit breaker.

NORMAL -> WARNING -> STOP_NEW_RISK -> CANCEL_PENDING -> MANAGE_EXISTING
      -> LOCKED

Semantics under test:
- WARNING is an early warning only: it must NOT block new risk at the gate;
  every state past it must.
- The gate auto-escalates the ACCOUNT state from live loss metrics
  (>=50% of the daily loss limit -> WARNING, >=100% -> STOP_NEW_RISK),
  never de-escalates and never touches manually set states.
- POST /risk/stop accepts all five non-NORMAL states; moving to a WEAKER
  state through /stop is refused (409) - de-escalation must go through
  POST /risk/resume (LOCKED additionally requires confirm_locked=true).
"""

import os
import unittest
from datetime import datetime, timezone
from decimal import Decimal
from unittest.mock import MagicMock, patch
from uuid import UUID

from fastapi.testclient import TestClient

from quantpilot_api.account_config import AccountConfig

ACCOUNT = UUID("aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa")
BLOCKING = ("STOP_NEW_RISK", "CANCEL_PENDING", "MANAGE_EXISTING", "LOCKED")
ESCALATED = ("WARNING", "STOP_NEW_RISK", "CANCEL_PENDING", "MANAGE_EXISTING", "LOCKED")


def stopped_states(global_status: str = "NORMAL", account_status: str = "NORMAL") -> dict:
    return {
        "GLOBAL": global_status,
        "ACCOUNT": account_status,
        "global_reason": None if global_status == "NORMAL" else "test",
        "account_reason": None if account_status == "NORMAL" else "test",
    }


class RiskStateConstantTests(unittest.TestCase):
    def test_prd_9_5_state_chain_and_blocking_set(self) -> None:
        from quantpilot_api import risk

        self.assertEqual(
            risk.RISK_STATES,
            ("NORMAL", "WARNING", "STOP_NEW_RISK", "CANCEL_PENDING",
             "MANAGE_EXISTING", "LOCKED"),
        )
        self.assertEqual(
            risk.RISK_STATE_RANK,
            {"NORMAL": 0, "WARNING": 1, "STOP_NEW_RISK": 2,
             "CANCEL_PENDING": 3, "MANAGE_EXISTING": 4, "LOCKED": 5},
        )
        self.assertEqual(risk.BLOCKING_RISK_STATES, frozenset(BLOCKING))
        self.assertNotIn("WARNING", risk.BLOCKING_RISK_STATES)


class GateStateSemanticsTests(unittest.TestCase):
    def _evaluate(self, states: dict) -> object:
        from quantpilot_api import risk

        with (
            patch.object(risk, "_stop_states", return_value=states),
            patch.object(risk.db, "query_one", return_value=None),
        ):
            return risk.evaluate_command(
                ACCOUNT, "OPEN_MARKET",
                {"symbol": "EURUSD", "side": "BUY", "volume": "0.02"},
                AccountConfig(), 1,
            )

    def test_warning_state_does_not_block_new_risk(self) -> None:
        decision = self._evaluate(stopped_states("WARNING", "WARNING"))
        # WARNING passed the stop level; the gate moved on to the snapshot
        # check, which rejects because no snapshot exists in this test
        self.assertEqual(decision.reason_codes, ["SNAPSHOT_STALE"])
        self.assertEqual(decision.snapshot["stop_states"],
                         {"global": "WARNING", "account": "WARNING"})

    def test_every_escalated_state_blocks_new_risk(self) -> None:
        for state in BLOCKING:
            with self.subTest(account_state=state):
                decision = self._evaluate(stopped_states(account_status=state))
                self.assertEqual(decision.decision, "REJECT")
                self.assertEqual(decision.reason_codes, ["ACCOUNT_STOP_NEW_RISK"])
            with self.subTest(global_state=state):
                decision = self._evaluate(stopped_states(global_status=state))
                self.assertEqual(decision.decision, "REJECT")
                self.assertEqual(decision.reason_codes, ["GLOBAL_STOP_NEW_RISK"])


class AutoEscalationTests(unittest.TestCase):
    def _run(self, current_status: str | None, *, loss: str, limit: str) -> object:
        """Patch the account state row + write connection, run escalation."""
        from quantpilot_api import risk

        row = None if current_status is None else {"status": current_status}
        connection = MagicMock()
        connection_context = MagicMock()
        connection_context.__enter__.return_value = connection
        with (
            patch.object(risk.db, "query_one", return_value=row),
            patch.object(risk.db, "conn", return_value=connection_context) as connect,
            patch("quantpilot_api.writers.add_system_event") as emit,
        ):
            result = risk.escalate_auto_state(
                ACCOUNT, daily_loss=Decimal(loss), daily_limit=Decimal(limit)
            )
        return result, connection, emit, connect

    def test_half_limit_escalates_normal_to_warning(self) -> None:
        result, connection, emit, _ = self._run(None, loss="50", limit="100")
        self.assertEqual(result, {"from": "NORMAL", "to": "WARNING"})
        self.assertEqual(connection.execute.call_count, 1)
        params = connection.execute.call_args.args[1]
        self.assertEqual(params[1], "WARNING")
        event = emit.call_args.kwargs
        self.assertEqual(event["event_type"], "risk.auto_escalated")
        self.assertEqual(event["severity"], "INFO")
        self.assertEqual(event["payload"]["from"], "NORMAL")
        self.assertEqual(event["payload"]["to"], "WARNING")

    def test_full_limit_escalates_to_stop_new_risk(self) -> None:
        result, connection, emit, _ = self._run(
            "WARNING", loss="100.01", limit="100"
        )
        self.assertEqual(result, {"from": "WARNING", "to": "STOP_NEW_RISK"})
        params = connection.execute.call_args.args[1]
        self.assertEqual(params[1], "STOP_NEW_RISK")
        event = emit.call_args.kwargs
        self.assertEqual(event["severity"], "WARN")

    def test_below_half_limit_is_a_noop(self) -> None:
        result, connection, emit, _ = self._run(None, loss="49.99", limit="100")
        self.assertIsNone(result)
        connection.execute.assert_not_called()
        emit.assert_not_called()

    def test_disabled_limit_is_a_noop(self) -> None:
        result, connection, emit, _ = self._run(None, loss="500", limit="0")
        self.assertIsNone(result)
        connection.execute.assert_not_called()
        emit.assert_not_called()

    def test_manual_states_are_never_touched(self) -> None:
        # CANCEL_PENDING / MANAGE_EXISTING / LOCKED rank above STOP_NEW_RISK:
        # auto escalation must not move them, even at a full-limit breach
        for status in ("CANCEL_PENDING", "MANAGE_EXISTING", "LOCKED"):
            with self.subTest(current=status):
                result, connection, emit, _ = self._run(
                    status, loss="120", limit="100"
                )
                self.assertIsNone(result)
                connection.execute.assert_not_called()
                emit.assert_not_called()

    def test_no_sidegrade_when_state_already_matches(self) -> None:
        # already WARNING with the same target -> no rewrite, no event
        result, connection, emit, _ = self._run("WARNING", loss="60", limit="100")
        self.assertIsNone(result)
        connection.execute.assert_not_called()
        emit.assert_not_called()
        # already STOP_NEW_RISK at a full breach -> no rewrite either
        result, connection, emit, _ = self._run("STOP_NEW_RISK", loss="99", limit="100")
        self.assertIsNone(result)
        connection.execute.assert_not_called()
        emit.assert_not_called()

    def test_gate_feeds_daily_loss_metrics_into_auto_escalation(self) -> None:
        from quantpilot_api import risk

        now = datetime.now(timezone.utc)
        snapshot = {
            "balance": "10000", "equity": "10000", "margin": None,
            "free_margin": None, "connected": True, "trade_allowed": True,
            "expert_allowed": True, "captured_at": now,
        }
        rows = iter([
            snapshot,
            {"net": Decimal("-60")},           # realized today
            {"open_positions": 0, "floating": Decimal("0")},
        ])
        config = AccountConfig.model_validate(
            {"risk": {"max_daily_loss_fraction": "0.005"}}
        )
        with (
            patch.object(risk, "_stop_states",
                         return_value=stopped_states("NORMAL", "NORMAL")),
            patch.object(risk.db, "query_one", side_effect=lambda *a: next(rows)),
            patch.object(risk, "escalate_auto_state") as escalate,
        ):
            decision = risk.evaluate_command(
                ACCOUNT, "OPEN_MARKET",
                {"symbol": "EURUSD", "side": "BUY", "volume": "0.02"},
                config, 2,
            )
        escalate.assert_called_once()
        self.assertEqual(escalate.call_args.kwargs["daily_loss"], Decimal("60"))
        self.assertEqual(escalate.call_args.kwargs["daily_limit"], Decimal("50"))
        self.assertEqual(decision.reason_codes, ["DAILY_LOSS_EXCEEDED"])


def _row(status: str, version: int = 3) -> dict:
    return {
        "scope": "ACCOUNT", "account_id": str(ACCOUNT), "status": status,
        "reason": "test", "updated_by": "api", "updated_at": None,
        "version": version,
    }


class StopResumeEndpointTests(unittest.TestCase):
    def setUp(self) -> None:
        import quantpilot_api.app as app_module

        self.app_module = app_module
        self.client = TestClient(app_module.app)

    def _post(self, path: str, body: dict) -> object:
        # R2 remediation: /risk/stop and /risk/resume are business routes, so
        # the service key no longer reaches them (P1-01). Ride the Bearer
        # lane as an ADMIN; the app-module db patches below are independent
        # of the auth-module db mock that bearer() installs.
        from rbac_testkit import bearer

        with (
            bearer(role="ADMIN") as headers,
            patch.object(self.app_module, "_account_or_404", return_value=ACCOUNT),
        ):
            return self.client.post(f"{path}", json=body, headers=headers)

    def test_stop_sets_each_escalated_state(self) -> None:
        for status in ESCALATED:
            with self.subTest(status=status):
                post_row = _row(status, version=4)
                connection = MagicMock()
                connection_context = MagicMock()
                connection_context.__enter__.return_value = connection
                with (
                    patch.object(self.app_module.db, "query_one",
                                 side_effect=[None, post_row]),
                    patch.object(self.app_module.db, "conn",
                                 return_value=connection_context),
                    patch.object(self.app_module.writers, "add_system_event"),
                ):
                    body = {"scope": "ACCOUNT", "account_id": str(ACCOUNT),
                            "reason": "unit", "updated_by": "unit-test"}
                    if status != "STOP_NEW_RISK":
                        body["status"] = status
                    resp = self._post("/api/v1/risk/stop", body)
                self.assertEqual(resp.status_code, 200, resp.text)
                self.assertEqual(resp.json()["data"]["status"], status)

    def test_stop_refuses_de_escalation(self) -> None:
        # currently LOCKED; asking for WARNING through /stop must 409 -
        # leaving a breaker state is reserved for POST /risk/resume
        with patch.object(self.app_module.db, "query_one",
                          side_effect=[_row("LOCKED")]):
            resp = self._post("/api/v1/risk/stop", {
                "scope": "ACCOUNT", "account_id": str(ACCOUNT),
                "status": "WARNING", "reason": "weaken", "updated_by": "unit-test",
            })
        self.assertEqual(resp.status_code, 409)
        self.assertIn("resume", resp.json()["error"]["message"])

    def test_stop_rejects_conflicting_locked_and_status(self) -> None:
        resp = self._post("/api/v1/risk/stop", {
            "scope": "ACCOUNT", "account_id": str(ACCOUNT),
            "status": "WARNING", "locked": True, "reason": "x",
        })
        self.assertEqual(resp.status_code, 422)

    def test_resume_clears_each_escalated_state(self) -> None:
        for status in ("WARNING", "CANCEL_PENDING", "MANAGE_EXISTING",
                       "STOP_NEW_RISK"):
            with self.subTest(status=status):
                normal_row = _row("NORMAL", version=4)
                with (
                    patch.object(self.app_module.db, "query_one",
                                 side_effect=[_row(status), normal_row]),
                    patch.object(self.app_module.db, "conn",
                                 return_value=MagicMock()),
                    patch.object(self.app_module.writers, "add_system_event"),
                ):
                    resp = self._post("/api/v1/risk/resume", {
                        "scope": "ACCOUNT", "account_id": str(ACCOUNT),
                        "reason": "resume", "updated_by": "unit-test",
                    })
                self.assertEqual(resp.status_code, 200, resp.text)
                self.assertEqual(resp.json()["data"]["status"], "NORMAL")

    def test_resume_from_locked_still_requires_confirm(self) -> None:
        with patch.object(self.app_module.db, "query_one",
                         side_effect=[_row("LOCKED")]):
            resp = self._post("/api/v1/risk/resume", {
                "scope": "ACCOUNT", "account_id": str(ACCOUNT),
                "reason": "resume", "updated_by": "unit-test",
            })
        self.assertEqual(resp.status_code, 409)
        self.assertIn("confirm_locked", resp.json()["error"]["message"])


if __name__ == "__main__":
    unittest.main()
