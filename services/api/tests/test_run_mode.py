"""Phase-3 P2 (mode truth): one run-mode definition, zero hardcoded displays.

Acceptance v2 P2: the EA already ships CTrade execution behind its
DryRunMode / LiveTrading inputs, yet the settings page hardcoded
OBSERVE_ONLY and the README still claimed the EA contains no execution
code. Truth chain (single definition, data-driven display):

  contracts: derive_ea_run_mode(dry_run, live_trading) ->
             OBSERVE_ONLY | DRY_RUN | LIVE (DryRunMode takes precedence,
             mirroring the EA's short-circuit in QpHandleCommand)
  EA: heartbeat carries run_mode derived from its own inputs
  cloud: account_snapshots.ea_run_mode persists exactly what the EA
         reported; unknown or missing values stay NULL (never guessed)
  API: GET /accounts/{id}/snapshot exposes ea_run_mode for the UI
"""

import json
import unittest
from unittest.mock import MagicMock, patch
from uuid import uuid4


def _heartbeat_body(run_mode=None, with_mode_key=True):
    body = {
        "ea_version": "1.9.0",
        "terminal": {"build": 4030, "data_path": "C:/mt5/data"},
        "account": {
            "login_ref": "25385951",
            "server": "Tickmill-Demo",
            "company": "Tickmill",
            "currency": "USD",
            "leverage": 500,
            "margin_mode": "RETAIL_HEDGING",
            "account_mode": "DEMO",
        },
        "connected": True,
        "trade_allowed": True,
        "expert_allowed": True,
        "balance": "1000.00",
        "equity": "1000.00",
        "margin": "0.00",
        "free_margin": "1000.00",
        "currency": "USD",
        "open_positions": 0,
        "observed_at": "2026-10-31T12:00:00+00:00",
    }
    if with_mode_key and run_mode is not None:
        body["run_mode"] = run_mode
    return body


# ---------------------------------------------------------------------------
# contract: derive_ea_run_mode is the single definition (EA mirrors it)
# ---------------------------------------------------------------------------


class DeriveRunModeTests(unittest.TestCase):
    def test_matrix(self) -> None:
        from quantpilot_contracts.constants import (
            EA_RUN_MODE_DRY_RUN,
            EA_RUN_MODE_LIVE,
            EA_RUN_MODE_OBSERVE_ONLY,
            derive_ea_run_mode,
        )

        # default posture: both inputs off -> pure observation
        self.assertEqual(derive_ea_run_mode(False, False), EA_RUN_MODE_OBSERVE_ONLY)
        # C1: pre-trade checks only, never an order
        self.assertEqual(derive_ea_run_mode(True, False), EA_RUN_MODE_DRY_RUN)
        # DryRunMode wins over LiveTrading (the EA short-circuits the same way)
        self.assertEqual(derive_ea_run_mode(True, True), EA_RUN_MODE_DRY_RUN)
        # C2: real CTrade execution (DEMO accounts only)
        self.assertEqual(derive_ea_run_mode(False, True), EA_RUN_MODE_LIVE)

    def test_values_are_stable_wire_strings(self) -> None:
        from quantpilot_contracts.constants import (
            EA_RUN_MODE_DRY_RUN,
            EA_RUN_MODE_LIVE,
            EA_RUN_MODE_OBSERVE_ONLY,
        )

        self.assertEqual(EA_RUN_MODE_OBSERVE_ONLY, "OBSERVE_ONLY")
        self.assertEqual(EA_RUN_MODE_DRY_RUN, "DRY_RUN")
        self.assertEqual(EA_RUN_MODE_LIVE, "LIVE")


# ---------------------------------------------------------------------------
# contract: the wire model must accept run_mode (agent validates EAs through
# StrictModel extra="forbid" - a missing field would DROP every EA heartbeat)
# ---------------------------------------------------------------------------


class HeartbeatContractTests(unittest.TestCase):
    def test_model_accepts_reported_run_mode(self) -> None:
        from quantpilot_contracts.models import EaHeartbeatData

        model = EaHeartbeatData.model_validate(_heartbeat_body(run_mode="DRY_RUN"))
        self.assertEqual(model.run_mode, "DRY_RUN")

    def test_model_defaults_run_mode_to_none(self) -> None:
        from quantpilot_contracts.models import EaHeartbeatData

        model = EaHeartbeatData.model_validate(_heartbeat_body(with_mode_key=False))
        self.assertIsNone(model.run_mode)

    def test_unknown_run_mode_does_not_break_the_heartbeat(self) -> None:
        """Forward compatibility: a future EA value parses; the storage edge normalizes."""
        from quantpilot_contracts.models import EaHeartbeatData

        model = EaHeartbeatData.model_validate(_heartbeat_body(run_mode="FUTURE_MODE"))
        self.assertEqual(model.run_mode, "FUTURE_MODE")


# ---------------------------------------------------------------------------
# cloud: heartbeats persist the reported run mode verbatim (or NULL)
# ---------------------------------------------------------------------------


class HeartbeatRunModeTests(unittest.TestCase):
    def _persisted(self, body):
        from quantpilot_api import writers

        with (
            patch.object(writers, "_resolve", return_value=(str(uuid4()), str(uuid4()))),
            patch.object(writers.db, "conn") as conn_factory,
        ):
            connection = conn_factory.return_value.__enter__.return_value
            connection.execute = MagicMock()
            writers.handle_heartbeat(body)
        # first execute on the mocked connection is the snapshot INSERT
        sql, params = connection.execute.call_args_list[0].args
        return sql, params

    def test_reported_run_mode_is_persisted(self) -> None:
        sql, params = self._persisted(_heartbeat_body(run_mode="DRY_RUN"))
        self.assertIn("ea_run_mode", sql)
        # ea_run_mode sits right after ea_version in the column list
        self.assertEqual(params[12], "DRY_RUN")

    def test_every_mode_round_trips(self) -> None:
        for mode in ("OBSERVE_ONLY", "DRY_RUN", "LIVE"):
            with self.subTest(mode=mode):
                _, params = self._persisted(_heartbeat_body(run_mode=mode))
                self.assertEqual(params[12], mode)

    def test_unknown_run_mode_is_never_stored_verbatim(self) -> None:
        """A garbage/spoofed label must not reach the DB as-is (NULL instead)."""
        _, params = self._persisted(_heartbeat_body(run_mode="SUPER_HACKED"))
        self.assertNotIn("SUPER_HACKED", params)
        self.assertIsNone(params[12])

    def test_missing_run_mode_stays_null(self) -> None:
        """Older EAs that never send run_mode are displayed as unknown, not guessed."""
        _, params = self._persisted(_heartbeat_body(with_mode_key=False))
        self.assertIsNone(params[12])


# ---------------------------------------------------------------------------
# API: the snapshot endpoint exposes ea_run_mode for the UI
# ---------------------------------------------------------------------------


class SnapshotRunModeTests(unittest.TestCase):
    def test_snapshot_selects_and_returns_ea_run_mode(self) -> None:
        import quantpilot_api.app as app_module

        row = {
            "snapshot_id": "s1",
            "account_id": str(uuid4()),
            "ea_run_mode": "DRY_RUN",
            "captured_at": "2026-10-31T12:00:00+00:00",
        }
        with patch.object(app_module.db, "query_one", return_value=row) as query_one:
            response = app_module.account_snapshot(str(uuid4()), None)
        self.assertIn("ea_run_mode", query_one.call_args.args[0])
        payload = json.loads(response.body)
        self.assertEqual(payload["data"]["ea_run_mode"], "DRY_RUN")


if __name__ == "__main__":
    unittest.main()
