"""Phase-3 R1 (crash-safe execution): unresolved outcomes + reconciliation.

Acceptance v2 P1 #1: the EA used to mark a command processed BEFORE
executing it. A terminal crash in that window permanently lost the command:
the cloud swept it to UNKNOWN (never resent, PRD 5.5) while the surviving
dedup marker made the EA re-ack DUPLICATE forever - no execution, no
verdict, no recovery path.

Phase-3 contract (EA v1.8.0 + cloud):

  EA: live-mutating commands are journaled to disk BEFORE the processed
  marker (status EXECUTING), then the broker outcome + order/deal/position
  tickets + the ack-delivery flag are persisted. Startup + throttled timer
  scans recover: a journal stuck at EXECUTING means the terminal died
  mid-execution - the outcome is UNCERTAIN, so the EA acks
  EXECUTE_UNRESOLVED and NEVER blindly re-executes; a final-but-unacked
  entry re-sends its stored verdict verbatim.

  Cloud: EXECUTE_UNRESOLVED rides the same DELIVERED/UNKNOWN -> ACKED
  transition (final, never a delivery retry), the structured execution
  tickets carried by the ack are stored on the command row, a WARN
  command.execute_unresolved event asks for reconciliation, and the
  reconcile service lets an operator confirm the true outcome against the
  broker projections (positions / orders / deals) before any re-issue.
"""

import unittest
from unittest.mock import MagicMock, patch
from uuid import UUID, uuid4

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


def _ack_body(ack_code: str, ack_message: str, **tickets: str) -> dict:
    body = {
        "ea_version": "1.8.0",
        "terminal": _terminal(),
        "account": _account(),
        "command_id": str(uuid4()),
        "ack_code": ack_code,
        "ack_message": ack_message,
        "observed_at": "2026-10-31T12:00:00+00:00",
    }
    body.update(tickets)
    return body


# ---------------------------------------------------------------------------
# contract: the ack body accepts EXECUTE_UNRESOLVED + structured tickets
# ---------------------------------------------------------------------------


class UnresolvedAckContractTests(unittest.TestCase):
    def test_execute_unresolved_is_a_valid_ack(self) -> None:
        from quantpilot_contracts.models import EaCommandAckData

        model = EaCommandAckData.model_validate(
            _ack_body(
                "EXECUTE_UNRESOLVED",
                "interrupted by terminal restart before the outcome was recorded",
                order_ticket="123456789",
                deal_ticket="0",
            )
        )
        self.assertEqual(model.ack_code, "EXECUTE_UNRESOLVED")
        self.assertEqual(model.order_ticket, "123456789")

    def test_execute_unresolved_fits_the_64_char_budget(self) -> None:
        from quantpilot_contracts.models import EaCommandAckData

        model = EaCommandAckData.model_validate(_ack_body("EXECUTE_UNRESOLVED", "smoke"))
        self.assertLessEqual(len(model.ack_code), 64)

    def test_structured_tickets_are_optional_back_compat(self) -> None:
        from quantpilot_contracts.models import EaCommandAckData

        # v1.7.0 acks carry no ticket fields at all - they must still pass
        model = EaCommandAckData.model_validate(_ack_body("EXECUTED", "order=1 deal=2"))
        self.assertEqual(model.order_ticket, "")
        self.assertEqual(model.deal_ticket, "")
        self.assertEqual(model.position_ticket, "")

    def test_executed_ack_carries_structured_tickets(self) -> None:
        from quantpilot_contracts.models import EaCommandAckData

        model = EaCommandAckData.model_validate(
            _ack_body(
                "EXECUTED",
                "CLOSE_POSITION executed (partial): position=987654321",
                position_ticket="987654321",
                deal_ticket="987654330",
            )
        )
        self.assertEqual(model.position_ticket, "987654321")
        self.assertEqual(model.deal_ticket, "987654330")


# ---------------------------------------------------------------------------
# state machine: handle_command_ack rides EXECUTE_UNRESOLVED + tickets
# ---------------------------------------------------------------------------


class UnresolvedStateMachineTests(unittest.TestCase):
    """EXECUTE_UNRESOLVED is a FINAL acked state (never a retry); the
    structured tickets persist next to the command; a WARN event demands
    reconciliation against the broker projections."""

    TERMINAL_ID = str(uuid4())
    ACCOUNT_ID = str(uuid4())

    def _run_handle_command_ack(self, ack_code: str, ack_message: str, **tickets: str) -> tuple:
        from quantpilot_api import writers

        command_id = uuid4()
        data = {
            "ea_version": "1.8.0",
            "terminal": _terminal(),
            "account": _account(),
            "command_id": str(command_id),
            "ack_code": ack_code,
            "ack_message": ack_message,
            "observed_at": "2026-10-31T12:00:00+00:00",
        }
        data.update(tickets)
        connection = MagicMock()
        connection.execute.return_value.fetchone.return_value = {"status": "ACKED"}
        context = MagicMock()
        context.__enter__.return_value = connection
        with (
            patch.object(writers, "_resolve", return_value=(self.TERMINAL_ID, self.ACCOUNT_ID)),
            patch.object(writers.db, "conn", return_value=context),
            patch.object(writers, "add_system_event") as event,
        ):
            writers.handle_command_ack(data)
        return command_id, connection, event

    def test_execute_unresolved_transitions_to_acked_verbatim(self) -> None:
        command_id, connection, _ = self._run_handle_command_ack(
            "EXECUTE_UNRESOLVED", "interrupted mid-execution"
        )
        sql, params = connection.execute.call_args_list[0].args
        self.assertIn("status         = 'ACKED'", sql)
        self.assertIn("AND status IN ('DELIVERED', 'UNKNOWN')", sql)
        self.assertEqual(params[0], "EXECUTE_UNRESOLVED")
        self.assertEqual(params[2], command_id)

    def test_execute_unresolved_raises_a_warn_reconciliation_event(self) -> None:
        _, _, event = self._run_handle_command_ack(
            "EXECUTE_UNRESOLVED", "interrupted mid-execution"
        )
        kinds = [call.kwargs["event_type"] for call in event.call_args_list]
        self.assertIn("command.acked", kinds)
        self.assertIn("command.execute_unresolved", kinds)
        warn_call = next(
            c for c in event.call_args_list if c.kwargs["event_type"] == "command.execute_unresolved"
        )
        self.assertEqual(warn_call.kwargs["severity"], "WARN")

    def test_executed_ack_emits_no_unresolved_event(self) -> None:
        _, _, event = self._run_handle_command_ack("EXECUTED", "order=1 deal=2")
        kinds = [call.kwargs["event_type"] for call in event.call_args_list]
        self.assertIn("command.acked", kinds)
        self.assertNotIn("command.execute_unresolved", kinds)

    def test_ack_tickets_persist_to_the_command_row(self) -> None:
        command_id, connection, _ = self._run_handle_command_ack(
            "EXECUTED",
            "OPEN_MARKET executed: order=123 deal=456",
            order_ticket="123",
            deal_ticket="456",
        )
        self.assertEqual(len(connection.execute.call_args_list), 2)
        sql, params = connection.execute.call_args_list[1].args
        self.assertIn("exec_order_ticket", sql)
        self.assertIn("exec_deal_ticket", sql)
        self.assertIn("exec_position_ticket", sql)
        self.assertEqual(params[0], "123")
        self.assertEqual(params[1], "456")
        self.assertIsNone(params[2])
        self.assertEqual(params[3], command_id)

    def test_ticket_update_is_skipped_for_ack_without_tickets(self) -> None:
        # v1.7.0 acks (no structured tickets) must not change behavior
        _, connection, _ = self._run_handle_command_ack("EXECUTED", "order=1 deal=2")
        self.assertEqual(len(connection.execute.call_args_list), 1)


# ---------------------------------------------------------------------------
# reconcile service: operator verdicts on uncertain outcomes
# ---------------------------------------------------------------------------


def _command_row(**overrides) -> dict:
    row = {
        "command_id": str(uuid4()),
        "account_id": str(uuid4()),
        "command_type": "OPEN_MARKET",
        "payload": {"symbol": "USDCHF", "side": "BUY", "volume": "0.01"},
        "status": "UNKNOWN",
        "ack_code": None,
        "ack_message": None,
        "reconciled": False,
        "exec_order_ticket": None,
        "exec_deal_ticket": None,
        "exec_position_ticket": None,
    }
    row.update(overrides)
    return row


class ReconcileVerdictTests(unittest.TestCase):
    def _apply(self, row: dict, verdict: str, note: str = "manual check") -> tuple:
        from quantpilot_api import reconcile

        connection = MagicMock()
        connection.execute.return_value.fetchone.return_value = {"command_id": row["command_id"]}
        context = MagicMock()
        context.__enter__.return_value = connection
        with (
            patch.object(reconcile, "command_by_id", return_value=row),
            patch.object(reconcile.db, "conn", return_value=context),
            patch.object(reconcile.writers, "add_system_event") as event,
            patch.object(reconcile.audit, "record") as audit_record,
        ):
            out = reconcile.apply(row["command_id"], verdict, note=note)
        return out, connection, event, audit_record

    def test_apply_on_unknown_command_confirms_executed(self) -> None:
        row = _command_row(status="UNKNOWN")
        out, connection, event, audit_record = self._apply(row, "CONFIRMED_EXECUTED")
        sql, params = connection.execute.call_args_list[0].args
        self.assertIn("status         = 'ACKED'", sql)
        self.assertIn("ack_code       = 'RECONCILED_MANUAL'", sql)
        self.assertIn("reconciled     = true", sql)
        self.assertIn("unknown_at     = NULL", sql)
        self.assertIn("CONFIRMED_EXECUTED", params[0])
        self.assertEqual(params[1], UUID(row["command_id"]))
        self.assertEqual(params[2], "EXECUTE_UNRESOLVED")
        kinds = [call.kwargs["event_type"] for call in event.call_args_list]
        self.assertIn("command.reconciled", kinds)
        audit_record.assert_called_once()

    def test_apply_on_execute_unresolved_confirms_not_executed(self) -> None:
        row = _command_row(status="ACKED", ack_code="EXECUTE_UNRESOLVED")
        out, connection, _, _ = self._apply(row, "CONFIRMED_NOT_EXECUTED")
        sql, params = connection.execute.call_args_list[0].args
        self.assertIn("ack_code       = 'RECONCILED_MANUAL'", sql)
        self.assertIn("CONFIRMED_NOT_EXECUTED", params[0])

    def test_apply_rejects_unknown_verdict(self) -> None:
        from quantpilot_api import reconcile
        from quantpilot_api.command_service import CommandError

        row = _command_row()
        with (
            patch.object(reconcile, "command_by_id", return_value=row),
            patch.object(reconcile.db, "conn"),
        ):
            with self.assertRaises(CommandError) as ctx:
                reconcile.apply(row["command_id"], "MAYBE", note="")
        self.assertEqual(ctx.exception.status_code, 400)

    def test_apply_missing_command_404(self) -> None:
        from quantpilot_api import reconcile
        from quantpilot_api.command_service import CommandError

        with patch.object(reconcile, "command_by_id", return_value=None):
            with self.assertRaises(CommandError) as ctx:
                reconcile.apply(str(uuid4()), "CONFIRMED_EXECUTED")
        self.assertEqual(ctx.exception.status_code, 404)


class ReconcileEvidenceTests(unittest.TestCase):
    def _evidence(self, row: dict, broker_rows: list) -> dict:
        from quantpilot_api import reconcile

        with (
            patch.object(reconcile, "command_by_id", return_value=row),
            patch.object(reconcile.db, "query_one", side_effect=broker_rows),
        ):
            return reconcile.evidence(row["command_id"])

    def test_close_position_already_closed_suggests_executed(self) -> None:
        row = _command_row(
            command_type="CLOSE_POSITION",
            payload={"position_ticket": "987654321"},
        )
        report = self._evidence(row, [{"status": "CLOSED", "volume": 0, "sl": None, "tp": None}])
        self.assertEqual(report["suggested_verdict"], "CONFIRMED_EXECUTED")

    def test_close_position_still_open_suggests_not_executed(self) -> None:
        row = _command_row(
            command_type="CLOSE_POSITION",
            payload={"position_ticket": "987654321"},
        )
        report = self._evidence(row, [{"status": "OPEN", "volume": "0.01", "sl": None, "tp": None}])
        self.assertEqual(report["suggested_verdict"], "CONFIRMED_NOT_EXECUTED")

    def test_cancel_order_removed_suggests_executed(self) -> None:
        row = _command_row(
            command_type="CANCEL_ORDER",
            payload={"order_ticket": "555555"},
        )
        report = self._evidence(row, [{"status": "REMOVED"}])
        self.assertEqual(report["suggested_verdict"], "CONFIRMED_EXECUTED")

    def test_open_market_with_matching_deal_suggests_executed(self) -> None:
        row = _command_row(
            status="ACKED",
            ack_code="EXECUTED",
            exec_order_ticket="123456789",
            exec_deal_ticket="123456790",
        )
        report = self._evidence(row, [{"broker_deal_ticket": "123456790"}])
        self.assertEqual(report["suggested_verdict"], "CONFIRMED_EXECUTED")

    def test_unresolved_open_market_without_tickets_is_uncertain(self) -> None:
        row = _command_row(status="UNKNOWN")
        report = self._evidence(row, [None, None])
        self.assertEqual(report["suggested_verdict"], "UNCERTAIN")
        self.assertIn("suggested_verdict", report)
        self.assertIn("command", report)


class UnresolvedListingTests(unittest.TestCase):
    def test_unresolved_query_covers_unknown_and_execute_unresolved(self) -> None:
        from quantpilot_api import reconcile

        with patch.object(reconcile.db, "query", return_value=[]) as q:
            out = reconcile.unresolved_commands()
        sql, params = q.call_args.args
        self.assertIn("status = 'UNKNOWN'", sql)
        self.assertIn("ack_code = %s", sql)
        self.assertEqual(params[0], "EXECUTE_UNRESOLVED")
        self.assertEqual(out, [])


if __name__ == "__main__":
    unittest.main()
