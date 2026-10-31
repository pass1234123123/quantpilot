"""Reconciliation of uncertain command outcomes (phase-3 R1, acceptance v2 P1 #1).

Two states need human attention:

  - UNKNOWN: delivered, never acked (EA crash / lost ack), outcome unknown
  - ACKED + EXECUTE_UNRESOLVED: the EA itself reported it died mid-execution

Neither is ever re-issued blindly (PRD 5.5). The operator confirms the true
outcome against the broker projections the cloud already keeps - positions,
orders and deals stream in continuously from the EA - and records a verdict:
CONFIRMED_EXECUTED or CONFIRMED_NOT_EXECUTED.

evidence() gathers the broker state around the command and proposes a
verdict (decisive broker facts only; anything circumstantial stays
UNCERTAIN). apply() records the decision as the terminal state
(status=ACKED, ack_code=RECONCILED_MANUAL, reconciled=true) with a full
audit trail. The EA never re-executes and the cloud never re-sends - the
next command, if any, is a fresh decision under the risk gate.
"""

from __future__ import annotations

from typing import Any
from uuid import UUID

from . import audit, db, writers
from .command_service import COMMAND_SELECT, CommandError, command_by_id

UNRESOLVED_ACK_CODE = "EXECUTE_UNRESOLVED"
VERDICTS = ("CONFIRMED_EXECUTED", "CONFIRMED_NOT_EXECUTED")


def unresolved_commands() -> list[dict[str, Any]]:
    """Commands whose outcome is uncertain and needs an operator verdict."""
    rows = db.query(
        COMMAND_SELECT
        + """
          WHERE c.status = 'UNKNOWN'
             OR (c.status = 'ACKED' AND c.ack_code = %s)
          ORDER BY c.updated_at DESC
          LIMIT 200
        """,
        (UNRESOLVED_ACK_CODE,),
    )
    return db.jsonable(rows)


def _load_or_404(command_id: str) -> dict[str, Any]:
    try:
        command_uuid = UUID(str(command_id))
    except (ValueError, AttributeError, TypeError) as exc:
        raise CommandError(404, "command not found") from exc
    row = command_by_id(command_uuid)
    if row is None:
        raise CommandError(404, "command not found")
    return row


def _position(account_uuid: UUID, ticket: str) -> dict[str, Any] | None:
    return db.query_one(
        """
        SELECT broker_position_ticket, status, volume, sl, tp
          FROM positions
         WHERE account_id = %s AND broker_position_ticket = %s
        """,
        (account_uuid, ticket),
    )


def _order(account_uuid: UUID, ticket: str) -> dict[str, Any] | None:
    return db.query_one(
        """
        SELECT broker_ticket, status, volume, price
          FROM orders
         WHERE account_id = %s AND broker_ticket = %s
        """,
        (account_uuid, ticket),
    )


def _deal_by_ticket(account_uuid: UUID, ticket: str) -> dict[str, Any] | None:
    return db.query_one(
        """
        SELECT broker_deal_ticket, broker_order_ticket, volume, price, deal_time
          FROM deals
         WHERE account_id = %s AND broker_deal_ticket = %s
        """,
        (account_uuid, ticket),
    )


def _num(value: Any) -> float | None:
    if value in (None, ""):
        return None
    try:
        return float(str(value))
    except (TypeError, ValueError):
        return None


def evidence(command_id: str) -> dict[str, Any]:
    """Broker-state evidence for one uncertain command + a suggested verdict.

    The suggestion is deliberately conservative: only a decisive broker
    fact yields CONFIRMED_*; anything circumstantial stays UNCERTAIN.
    """
    row = _load_or_404(command_id)
    account_uuid = UUID(row["account_id"])
    payload = row.get("payload") or {}
    command_type = row["command_type"]
    broker: dict[str, Any] = {}
    suggestion = "UNCERTAIN"
    rationale = "no decisive broker fact found; inspect the evidence manually"

    if command_type == "CLOSE_POSITION":
        ticket = str(payload.get("position_ticket", ""))
        position = _position(account_uuid, ticket) if ticket else None
        broker["position"] = position
        if position is not None:
            if position["status"] == "CLOSED":
                suggestion = "CONFIRMED_EXECUTED"
                rationale = f"position {ticket} is CLOSED in the broker projection"
            else:
                suggestion = "CONFIRMED_NOT_EXECUTED"
                rationale = f"position {ticket} is still OPEN"
    elif command_type == "MODIFY_POSITION":
        ticket = str(payload.get("position_ticket", ""))
        position = _position(account_uuid, ticket) if ticket else None
        broker["position"] = position
        wanted_sl = _num(payload.get("sl"))
        wanted_tp = _num(payload.get("tp"))
        if position is not None and position["status"] == "OPEN":
            checks: list[bool] = []
            if wanted_sl is not None:
                checks.append(_num(position["sl"]) == wanted_sl)
            if wanted_tp is not None:
                checks.append(_num(position["tp"]) == wanted_tp)
            if checks and all(checks):
                suggestion = "CONFIRMED_EXECUTED"
                rationale = f"position {ticket} carries the requested sl/tp"
            elif checks:
                suggestion = "CONFIRMED_NOT_EXECUTED"
                rationale = f"position {ticket} still has the previous sl/tp"
    elif command_type == "CANCEL_ORDER":
        ticket = str(payload.get("order_ticket", ""))
        order = _order(account_uuid, ticket) if ticket else None
        broker["order"] = order
        if order is not None:
            if order["status"] == "REMOVED":
                suggestion = "CONFIRMED_EXECUTED"
                rationale = f"order {ticket} is REMOVED from the broker projection"
            else:
                suggestion = "CONFIRMED_NOT_EXECUTED"
                rationale = f"order {ticket} is still ACTIVE"
    elif command_type in ("OPEN_MARKET", "PLACE_PENDING"):
        deal_ticket = str(row.get("exec_deal_ticket") or "")
        order_ticket = str(row.get("exec_order_ticket") or "")
        deal = _deal_by_ticket(account_uuid, deal_ticket) if deal_ticket else None
        broker["deal"] = deal
        order = None
        if deal is None and order_ticket:
            order = _order(account_uuid, order_ticket)
            broker["order"] = order
        if deal is not None:
            suggestion = "CONFIRMED_EXECUTED"
            rationale = f"deal {deal_ticket} exists in the broker projection"
        elif order is not None:
            suggestion = "CONFIRMED_EXECUTED"
            rationale = f"order {order_ticket} exists ({order['status']}) in the broker projection"

    return {
        "command": db.jsonable(row),
        "broker": db.jsonable(broker),
        "suggested_verdict": suggestion,
        "rationale": rationale,
    }


def apply(command_id: str, verdict: str, note: str = "") -> dict[str, Any]:
    """Record the operator's verdict: terminal ACKED + reconciled=true."""
    row = _load_or_404(command_id)
    if verdict not in VERDICTS:
        raise CommandError(400, f"verdict must be one of {VERDICTS}")
    account_uuid = UUID(row["account_id"])
    message = f"operator verdict {verdict}" + (f": {note}" if note else "")
    with db.conn() as connection:
        updated = connection.execute(
            """
            UPDATE commands
               SET status         = 'ACKED',
                   acked_at       = COALESCE(acked_at, now()),
                   ack_code       = 'RECONCILED_MANUAL',
                   ack_message    = %s,
                   reconciled     = true,
                   unknown_at     = NULL,
                   updated_at     = now(),
                   version        = version + 1
             WHERE command_id = %s
               AND (status IN ('DELIVERED', 'UNKNOWN') OR ack_code = %s)
            RETURNING command_id::text
            """,
            (message, UUID(row["command_id"]), UNRESOLVED_ACK_CODE),
        ).fetchone()
    if updated is None:
        already = command_by_id(UUID(row["command_id"]))
        if already is not None and already["reconciled"]:
            return db.jsonable(already)
        raise CommandError(409, "command is not in a reconcilable state")
    writers.add_system_event(
        source="api",
        event_type="command.reconciled",
        severity="INFO",
        payload={
            "command_id": row["command_id"],
            "account_id": row["account_id"],
            "verdict": verdict,
            "note": note or None,
            "previous_status": row["status"],
            "previous_ack_code": row["ack_code"],
            "detail": "operator confirmed the outcome against broker state"
                      " (phase-3 R1 reconciliation)",
        },
    )
    audit.record(
        account_uuid,
        "COMMAND_RECONCILE",
        "command",
        row["command_id"],
        {"verdict": verdict, "note": note, "previous_status": row["status"]},
    )
    final = command_by_id(UUID(row["command_id"]))
    return db.jsonable(final if final is not None else row)
