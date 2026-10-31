"""Shared durable-command creation for HTTP and strategy automation."""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from typing import Any
from uuid import UUID

import psycopg.errors
from quantpilot_contracts.models import TradeCommandCreate

from . import db, risk, writers
from .account_config import AccountConfig

COMMAND_MAX_TTL_SECONDS = 600

COMMAND_SELECT = """
    SELECT c.command_id::text AS command_id, c.account_id::text AS account_id,
           c.command_type, c.payload, c.status, c.expires_at,
           c.created_by, c.strategy_instance_id, c.signal_id,
           c.delivery_attempts, c.delivered_at, c.acked_at, c.ack_code,
           c.ack_message, c.reconciled, c.unknown_at, c.expired_at,
           c.created_at, c.updated_at
      FROM commands c
"""


class CommandError(ValueError):
    """Domain failure that the HTTP layer maps to its original status code."""

    def __init__(self, status_code: int, detail: str):
        super().__init__(detail)
        self.status_code = status_code
        self.detail = detail


def command_by_id(command_uuid: UUID) -> dict[str, Any] | None:
    return db.query_one(COMMAND_SELECT + " WHERE c.command_id = %s", (command_uuid,))


def command_payload(row: dict[str, Any], created: bool | None) -> dict[str, Any]:
    payload = db.jsonable(row)
    if created is not None:
        payload["created"] = created
    return payload


def effective_config(account_uuid: UUID) -> tuple[AccountConfig, int]:
    """Validated active account config, or safe defaults at version zero."""
    row = db.query_one(
        """
        SELECT version, config_json
          FROM risk_profiles
         WHERE account_id = %s AND status = 'ACTIVE'
         ORDER BY version DESC
         LIMIT 1
        """,
        (account_uuid,),
    )
    if row is None:
        return AccountConfig(), 0
    return AccountConfig.model_validate(row["config_json"]), row["version"]


def _account_or_error(account_id: str) -> UUID:
    try:
        account_uuid = UUID(account_id)
    except (ValueError, AttributeError, TypeError) as exc:
        raise CommandError(404, "account not found") from exc
    if db.query_one("SELECT id FROM trading_accounts WHERE id = %s", (account_uuid,)) is None:
        raise CommandError(404, "account not found")
    return account_uuid


def create_command(
    body: TradeCommandCreate,
    *,
    config: AccountConfig | None = None,
    config_version: int | None = None,
    context: dict[str, Any] | None = None,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Create or replay one durable command through the authoritative risk gate."""
    account_uuid = _account_or_error(str(body.account_id))
    now = now or datetime.now(timezone.utc)
    if not now < body.expires_at <= now + timedelta(seconds=COMMAND_MAX_TTL_SECONDS):
        raise CommandError(400, "expires_at must be in the future and at most 600s ahead")
    if body.idempotency_key is not None:
        clash = db.query_one(
            """
            SELECT command_id::text AS command_id
              FROM commands
             WHERE idempotency_key = %s AND command_id <> %s
            """,
            (str(body.idempotency_key), str(body.command_id)),
        )
        if clash is not None:
            raise CommandError(409, "idempotency_key already used by another command")
    existing = command_by_id(body.command_id)
    if existing is not None:
        if existing["account_id"] != str(account_uuid) or existing["command_type"] != body.command_type:
            raise CommandError(409, "command_id already exists with different content")
        payload = command_payload(existing, created=False)
        decision = risk.decision_for_command(body.command_id)
        if decision is not None:
            payload["risk_decision"] = decision
        return payload

    decision = None
    initial_status = "QUEUED"
    if body.command_type in risk.RISK_INCREASING_TYPES:
        if config is None:
            config, config_version = effective_config(account_uuid)
        assert config_version is not None
        decision = risk.evaluate_command(
            account_uuid,
            body.command_type,
            body.payload,
            config=config,
            config_version=config_version,
            context=context,
        )
        if decision.decision == "REJECT":
            initial_status = "REJECTED"

    try:
        with db.conn() as connection:
            inserted = connection.execute(
                """
                INSERT INTO commands (command_id, idempotency_key, account_id,
                                      command_type, payload, status, expires_at,
                                      created_by, strategy_instance_id, signal_id)
                VALUES (%s, %s, %s, %s, CAST(%s AS jsonb), %s, %s, %s, %s, %s)
                ON CONFLICT (command_id) DO NOTHING
                RETURNING command_id::text
                """,
                (
                    str(body.command_id),
                    str(body.idempotency_key) if body.idempotency_key is not None else None,
                    account_uuid,
                    body.command_type,
                    json.dumps(body.payload),
                    initial_status,
                    body.expires_at,
                    body.created_by,
                    str(body.strategy_instance_id) if body.strategy_instance_id is not None else None,
                    str(body.signal_id) if body.signal_id is not None else None,
                ),
            ).fetchone()
    except psycopg.errors.UniqueViolation as exc:
        raise CommandError(409, f"idempotency conflict: {exc}") from exc
    if inserted is None:
        row = command_by_id(body.command_id)
        assert row is not None
        return command_payload(row, created=False)

    if decision is not None:
        risk.record_decision(
            account_uuid,
            body.command_id,
            str(body.signal_id) if body.signal_id is not None else None,
            decision,
        )
    writers.add_system_event(
        source="api",
        event_type="command.created",
        severity="INFO",
        payload={
            "command_id": str(body.command_id),
            "account_id": str(account_uuid),
            "command_type": body.command_type,
            "status": initial_status,
            "created_by": body.created_by,
            "expires_at": body.expires_at.isoformat(),
            "signal_id": str(body.signal_id) if body.signal_id is not None else None,
            "strategy_instance_id": str(body.strategy_instance_id)
            if body.strategy_instance_id is not None
            else None,
        },
    )
    if decision is not None and decision.decision == "REJECT":
        writers.add_system_event(
            source="api",
            event_type="risk.rejected",
            severity="WARN",
            payload={
                "command_id": str(body.command_id),
                "account_id": str(account_uuid),
                "command_type": body.command_type,
                "reason_codes": decision.reason_codes,
                "config_version": decision.snapshot.get("config_version", 0),
                "detail": "cloud risk gate refused the new-risk command; stored"
                          " as REJECTED and never delivered (PRD 9.1)",
            },
        )
    row = command_by_id(body.command_id)
    assert row is not None
    payload = command_payload(row, created=True)
    if decision is not None:
        payload["risk_decision"] = {
            "decision": decision.decision,
            "reason_codes": decision.reason_codes,
            "decided_at": datetime.now(timezone.utc).isoformat(),
            "config_version": decision.snapshot.get("config_version", 0),
        }
    return payload
