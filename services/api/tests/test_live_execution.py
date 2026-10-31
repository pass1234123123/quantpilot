"""Phase-2 task 8 (MT5 live execution, acceptance C2): the execution ack channel.

Design: the EA gains a LiveTrading input (default false). Only when
DryRunMode=false AND LiveTrading=true AND the terminal account is a DEMO
account (dual guard, acceptance C2) do trade commands execute for real via
CTrade - after the full C1 pre-trade chain for risk-increasing types.
DryRunMode=true short-circuits everything: never a real order (PRD 21.3).

Cloud side is intentionally unchanged: ack_code stays a free string and
handle_command_ack rides the new codes through the same DELIVERED/UNKNOWN ->
ACKED transition, writing the EA's verdict verbatim (the cloud records
execution outcomes, it never second-guesses them; the cloud is not the
trader, PRD 21.3). These tests pin that contract so nobody narrows it later:

1. EaCommandAckData accepts the live-execution ack codes with broker
   tickets in ack_message (<= 512 chars, C3 reconciliation material).
2. handle_command_ack transitions to ACKED and writes the new codes
   verbatim - including EXECUTE_FAILED (a failed execution is still a
   FINAL acked command, never a delivery retry - single-attempt, C3).
3. The MODIFY/CLOSE/CANCEL payload contracts stay stable: the EA's live
   path parses exactly these fields (absent sl/tp = keep current; the EA
   never removes protection, PRD 13.4).
"""

import unittest
from unittest.mock import MagicMock, patch
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
        "login_ref": "25385951",
        "server": "Tickmill-Demo",
        "company": "Tickmill",
        "currency": "USD",
        "leverage": 500,
        "margin_mode": "RETAIL_HEDGING",
        "account_mode": "DEMO",
    }


def _ack_body(ack_code: str, ack_message: str) -> dict:
    return {
        "ea_version": "1.7.0",
        "terminal": _terminal(),
        "account": _account(),
        "command_id": str(uuid4()),
        "ack_code": ack_code,
        "ack_message": ack_message,
        "observed_at": "2026-10-31T12:00:00+00:00",
    }


class LiveExecutionAckContractTests(unittest.TestCase):
    """The EA -> agent ack body contract accepts the live-execution codes."""

    def test_executed_is_a_valid_ack(self) -> None:
        from quantpilot_contracts.models import EaCommandAckData

        message = (
            "OPEN_MARKET executed: order=123456789 deal=123456790 "
            "price=0.82530 volume=0.01 retcode=10009"
        )
        model = EaCommandAckData.model_validate(_ack_body("EXECUTED", message))
        self.assertEqual(model.ack_code, "EXECUTED")
        self.assertIn("order=", model.ack_message)
        self.assertIn("deal=", model.ack_message)

    def test_execute_failed_is_a_valid_ack(self) -> None:
        from quantpilot_contracts.models import EaCommandAckData

        message = "retcode=10016 Invalid stops (FATAL): order=0 deal=0"
        model = EaCommandAckData.model_validate(_ack_body("EXECUTE_FAILED", message))
        self.assertEqual(model.ack_code, "EXECUTE_FAILED")
        self.assertIn("retcode=", model.ack_message)

    def test_execute_rejected_is_a_valid_ack(self) -> None:
        from quantpilot_contracts.models import EaCommandAckData

        model = EaCommandAckData.model_validate(
            _ack_body("EXECUTE_REJECTED", "VOLUME_ABOVE_LOCAL_CAP: volume 1.00 > cap 0.10")
        )
        self.assertEqual(model.ack_code, "EXECUTE_REJECTED")

    def test_live_refused_not_demo_is_a_valid_ack(self) -> None:
        from quantpilot_contracts.models import EaCommandAckData

        model = EaCommandAckData.model_validate(
            _ack_body("LIVE_REFUSED_NOT_DEMO", "LiveTrading requires a DEMO account; mode=REAL")
        )
        self.assertEqual(model.ack_code, "LIVE_REFUSED_NOT_DEMO")

    def test_all_live_ack_codes_fit_the_64_char_budget(self) -> None:
        from quantpilot_contracts.models import EaCommandAckData

        for code in (
            "EXECUTED",
            "EXECUTE_FAILED",
            "EXECUTE_REJECTED",
            "LIVE_REFUSED_NOT_DEMO",
        ):
            data = EaCommandAckData.model_validate(_ack_body(code, "smoke"))
            self.assertLessEqual(len(data.ack_code), 64)

    def test_ticket_bearing_message_fits_the_512_char_budget(self) -> None:
        from quantpilot_contracts.models import EaCommandAckData

        message = (
            "CLOSE_POSITION executed (partial): position=987654321 closed=0.01 "
            "remaining=0.02 deal=987654330 retcode=10009"
        )
        model = EaCommandAckData.model_validate(_ack_body("EXECUTED", message))
        self.assertLessEqual(len(model.ack_message), 512)


class LiveExecutionStateMachineTests(unittest.TestCase):
    """handle_command_ack rides live-execution codes through the same
    DELIVERED/UNKNOWN -> ACKED transition, verbatim."""

    TERMINAL_ID = str(uuid4())
    ACCOUNT_ID = str(uuid4())

    def _run_handle_command_ack(self, ack_code: str, ack_message: str) -> tuple:
        from quantpilot_api import writers

        command_id = uuid4()
        data = {
            "ea_version": "1.7.0",
            "terminal": _terminal(),
            "account": _account(),
            "command_id": str(command_id),
            "ack_code": ack_code,
            "ack_message": ack_message,
            "observed_at": "2026-10-31T12:00:00+00:00",
        }
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

    def test_executed_transitions_to_acked_verbatim(self) -> None:
        command_id, update_call = self._run_handle_command_ack(
            "EXECUTED",
            "OPEN_MARKET executed: order=123456789 deal=123456790 retcode=10009",
        )
        sql, params = update_call.args
        self.assertIn("status         = 'ACKED'", sql)
        self.assertIn("AND status IN ('DELIVERED', 'UNKNOWN')", sql)
        self.assertEqual(params[0], "EXECUTED")
        self.assertIn("order=", params[1])
        self.assertEqual(params[2], command_id)
        self.assertEqual(str(params[3]), self.ACCOUNT_ID)

    def test_execute_failed_is_final_not_a_delivery_retry(self) -> None:
        command_id, update_call = self._run_handle_command_ack(
            "EXECUTE_FAILED", "retcode=10019 No money (FATAL): order=0 deal=0"
        )
        sql, params = update_call.args
        # a failed execution is still a final ACKED command - the cloud never
        # re-delivers it (single-attempt principle, acceptance C3)
        self.assertIn("AND status IN ('DELIVERED', 'UNKNOWN')", sql)
        self.assertEqual(params[0], "EXECUTE_FAILED")
        self.assertEqual(params[2], command_id)

    def test_execute_rejected_writes_code_verbatim(self) -> None:
        _, update_call = self._run_handle_command_ack(
            "EXECUTE_REJECTED", "SL_TOO_CLOSE: |ref - SL| < stops_level 0.00080"
        )
        _, params = update_call.args
        self.assertEqual(params[0], "EXECUTE_REJECTED")


class LiveExecutionPayloadTests(unittest.TestCase):
    """The EA live path parses exactly these payload contracts (PRD 13.4)."""

    def test_modify_position_fields_stable(self) -> None:
        from quantpilot_contracts.models import ModifyPositionPayload

        payload = ModifyPositionPayload.model_validate(
            {"position_ticket": "123456789", "sl": "0.81900"}
        )
        self.assertEqual(payload.position_ticket, "123456789")
        self.assertEqual(str(payload.sl), "0.81900")
        # absent tp = keep current (the EA never removes protection)
        self.assertIsNone(payload.tp)

    def test_close_position_volume_optional_full_close(self) -> None:
        from quantpilot_contracts.models import ClosePositionPayload

        full = ClosePositionPayload.model_validate({"position_ticket": "123456789"})
        self.assertIsNone(full.volume)  # None = full close
        partial = ClosePositionPayload.model_validate(
            {"position_ticket": "123456789", "volume": "0.01"}
        )
        self.assertEqual(str(partial.volume), "0.01")

    def test_cancel_order_fields_stable(self) -> None:
        from quantpilot_contracts.models import CancelOrderPayload

        payload = CancelOrderPayload.model_validate({"order_ticket": "987654321"})
        self.assertEqual(payload.order_ticket, "987654321")

    def test_open_market_defaults_pinned(self) -> None:
        from quantpilot_contracts.models import OpenMarketPayload

        payload = OpenMarketPayload.model_validate(
            {"symbol": "USDCHF", "side": "BUY", "volume": "0.01"}
        )
        # the EA's C1 chain and live path substitute these defaults when the
        # payload omits them - keep the defaults in sync
        self.assertEqual(payload.max_deviation_points, 15)
        self.assertEqual(payload.magic_number, 0)


if __name__ == "__main__":
    unittest.main()
