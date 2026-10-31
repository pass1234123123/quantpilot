"""Phase-2 task 7 (MT5 DRY_RUN): the C1 pre-trade-check ack channel.

Design: the EA gains a DryRunMode input. For risk-increasing commands
(OPEN_MARKET / PLACE_PENDING) it runs full C1 pre-trade checks (trade mode,
volume spec, SL/TP stop distances, spread guard, session, OrderCheck) and
acks DRY_RUN_PASSED / DRY_RUN_REJECTED - still WITHOUT OrderSend (slice 2
dry-run, never real execution; real fills are task 8, demo-only).

These tests pin the cloud side of that channel so nobody later narrows it:
1. EaCommandAckData (EA -> agent contract) must accept DRY_RUN_* ack codes;
   ack_code stays a free string, never a Literal enum.
2. handle_command_ack must transition DELIVERED -> ACKED and write the EA's
   ack_code verbatim for DRY_RUN_REJECTED too - the cloud records the EA's
   verdict, it never second-guesses it (PRD 21.3: cloud is not the trader).
3. The OPEN_MARKET payload contract stays the source of truth for what the
   C1 checker in the EA parses.
"""

import unittest
from unittest.mock import patch
from uuid import uuid4

TEST_API_KEY = "test-api-key"


def _terminal() -> dict:
    return {
        "build": 4030,
        "company": "MetaQuotes",
        "exe_path": "C:/mt5/terminal64.exe",
        "data_path": "C:/mt5/data",
    }


def _account() -> dict:
    return {
        "login_ref": "12345678",
        "server": "Demo-Server",
        "company": "MetaQuotes",
        "currency": "USD",
        "leverage": 100,
        "margin_mode": "RETAIL_HEDGING",
        "account_mode": "DEMO",
    }


def _ack_body(ack_code: str, ack_message: str) -> dict:
    return {
        "ea_version": "1.0.0",
        "terminal": _terminal(),
        "account": _account(),
        "command_id": str(uuid4()),
        "ack_code": ack_code,
        "ack_message": ack_message,
        "observed_at": "2026-10-31T12:00:00+00:00",
    }


class EaCommandAckContractTests(unittest.TestCase):
    """The EA -> agent ack body contract accepts DRY_RUN_* codes."""

    def test_dry_run_passed_is_a_valid_ack(self) -> None:
        from quantpilot_contracts.models import EaCommandAckData

        model = EaCommandAckData.model_validate(
            _ack_body(
                "DRY_RUN_PASSED",
                "C1 pre-trade checks passed; not executed (DRY_RUN, no OrderSend)",
            )
        )
        self.assertEqual(model.ack_code, "DRY_RUN_PASSED")

    def test_dry_run_rejected_is_a_valid_ack(self) -> None:
        from quantpilot_contracts.models import EaCommandAckData

        model = EaCommandAckData.model_validate(
            _ack_body("DRY_RUN_REJECTED", "C1 check failed: SESSION_CLOSED (18:02)")
        )
        self.assertEqual(model.ack_code, "DRY_RUN_REJECTED")
        self.assertIn("SESSION_CLOSED", model.ack_message)

    def test_agent_relay_route_accepts_dry_run_body(self) -> None:
        """The agent's /ea/v1/commands/ack handler validates with the same
        model, so contract acceptance implies relay acceptance."""
        from quantpilot_contracts.models import EaCommandAckData

        for code in ("OBSERVE_ONLY_ACK", "DRY_RUN_PASSED", "DRY_RUN_REJECTED"):
            data = EaCommandAckData.model_validate(_ack_body(code, "smoke"))
            self.assertLessEqual(len(data.ack_code), 64)


class CommandAckStateMachineTests(unittest.TestCase):
    """handle_command_ack rides DRY_RUN codes through the same state machine:
    DELIVERED/UNKNOWN -> ACKED with the EA's ack_code written verbatim."""

    TERMINAL_ID = str(uuid4())
    ACCOUNT_ID = str(uuid4())

    def _run_handle_command_ack(self, ack_code: str, ack_message: str) -> tuple:
        from quantpilot_api import writers

        command_id = uuid4()
        data = {
            "ea_version": "1.0.0",
            "terminal": _terminal(),
            "account": _account(),
            "command_id": str(command_id),
            "ack_code": ack_code,
            "ack_message": ack_message,
            "observed_at": "2026-10-31T12:00:00+00:00",
        }
        connection = type("C", (), {})  # placeholder, replaced by MagicMock below
        from unittest.mock import MagicMock

        connection = MagicMock()
        connection.execute.return_value.fetchone.return_value = {"status": "ACKED"}
        context = MagicMock()
        context.__enter__.return_value = connection
        with (
            patch.object(writers, "_resolve", return_value=(self.TERMINAL_ID, self.ACCOUNT_ID)),
            patch.object(writers.db, "conn", return_value=context),
        ):
            writers.handle_command_ack(data)
        update_call = connection.execute.call_args_list[0]
        return command_id, update_call

    def test_dry_run_rejected_still_transitions_to_acked(self) -> None:
        command_id, update_call = self._run_handle_command_ack(
            "DRY_RUN_REJECTED", "C1 check failed: SPREAD_TOO_HIGH (spread=45 > max=30)"
        )
        sql, params = update_call.args
        self.assertIn("status         = 'ACKED'", sql)
        self.assertIn("AND status IN ('DELIVERED', 'UNKNOWN')", sql)
        # the EA's verdict is written verbatim - no cloud-side rewriting
        self.assertEqual(params[0], "DRY_RUN_REJECTED")
        self.assertIn("SPREAD_TOO_HIGH", params[1])
        self.assertEqual(params[2], command_id)
        self.assertEqual(str(params[3]), self.ACCOUNT_ID)

    def test_dry_run_passed_writes_ack_code_verbatim(self) -> None:
        command_id, update_call = self._run_handle_command_ack(
            "DRY_RUN_PASSED", "C1 pre-trade checks passed; not executed (DRY_RUN, no OrderSend)"
        )
        _, params = update_call.args
        self.assertEqual(params[0], "DRY_RUN_PASSED")

    def test_observe_only_ack_unchanged(self) -> None:
        command_id, update_call = self._run_handle_command_ack(
            "OBSERVE_ONLY_ACK", "received; not executed (OBSERVE_ONLY slice 1)"
        )
        _, params = update_call.args
        self.assertEqual(params[0], "OBSERVE_ONLY_ACK")


class DryRunCommandPayloadTests(unittest.TestCase):
    """The C1 checker in the EA parses what these contracts define; keep the
    fields stable (OPEN_MARKET payload, PRD 13.3)."""

    def test_open_market_payload_fields_stable(self) -> None:
        from quantpilot_contracts.models import OpenMarketPayload

        payload = OpenMarketPayload.model_validate(
            {
                "symbol": "EURUSD",
                "side": "BUY",
                "volume": "0.02",
                "stop_loss": "1.078",
                "take_profit": "1.095",
            }
        )
        self.assertEqual(payload.symbol, "EURUSD")
        self.assertEqual(payload.side, "BUY")

    def test_place_pending_payload_fields_stable(self) -> None:
        from quantpilot_contracts.models import PlacePendingPayload

        payload = PlacePendingPayload.model_validate(
            {
                "symbol": "EURUSD",
                "side": "SELL",
                "order_kind": "SELL_LIMIT",
                "volume": "0.10",
                "price": "1.0920",
            }
        )
        self.assertEqual(payload.order_kind, "SELL_LIMIT")


if __name__ == "__main__":
    unittest.main()
