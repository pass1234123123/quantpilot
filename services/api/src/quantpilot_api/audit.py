"""Best-effort operation audit trail (phase-2 task 6, PRD 14 audit story).

audit_logs records who/what/when for every state-changing cloud operation
(config saves, instance updates, command creation, backtest creation, AI
triggers). record() is best-effort BY DESIGN: an audit write failure must
never break the audited operation - it logs loudly instead. The read side
powers GET /api/v1/audit-logs behind the standard X-API-Key gate.

Actor: in the single service API-key phase (PRD 14) there are no user
identities yet, so every row is attributed to ACTOR="api-key"; RBAC will
introduce real actors in the commercialization phase.
"""

from __future__ import annotations

import json
import logging
from typing import Any
from uuid import UUID

from . import db

logger = logging.getLogger("quantpilot.audit")

ACTOR = "api-key"

LIMIT_MAX = 500

_INSERT = """
    INSERT INTO audit_logs (account_id, actor, action, object_type, object_id, payload)
    VALUES (%s, %s, %s, %s, %s, CAST(%s AS jsonb))
"""

_SELECT = """
    SELECT l.id::text AS id, l.account_id::text AS account_id, l.actor,
           l.action, l.object_type, l.object_id, l.payload, l.created_at
      FROM audit_logs l
"""


def record(
    account_id: UUID,
    action: str,
    object_type: str,
    object_id: str | None = None,
    payload: dict[str, Any] | None = None,
    actor: str | None = None,
) -> None:
    """Append one audit row; NEVER raises (auditing must not break the
    audited operation - failures are logged loudly instead).

    actor: the authenticated principal's username (R3 P2-02 - the caller
    cannot forge it; it comes from request.state.principal). Machine-lane
    callers leave it None and fall back to the module default."""
    try:
        db.execute(
            _INSERT,
            (
                account_id,
                actor if actor else ACTOR,
                action,
                object_type,
                object_id,
                json.dumps(payload) if payload is not None else None,
            ),
        )
    except Exception:  # noqa: BLE001 - best-effort by design
        logger.exception(
            "audit write failed: action=%s object_type=%s object_id=%s",
            action,
            object_type,
            object_id,
        )


def list_audit_logs(
    account_id: UUID | None = None,
    action: str | None = None,
    limit: int = 100,
    granted_user_id: str | None = None,
) -> list[dict[str, Any]]:
    """Audit rows, newest first; optional account/action filters.

    granted_user_id narrows the unfiltered view to that user's granted
    accounts (R2 acceptance P1-02) - the caller is responsible for having
    grant-checked any explicit account_id beforehand."""
    where = ["1=1"]
    params: list[Any] = []
    if account_id is not None:
        where.append("l.account_id = %s")
        params.append(account_id)
    if granted_user_id is not None:
        where.append(
            "l.account_id IN (SELECT account_id FROM user_account_grants WHERE user_id = %s)"
        )
        params.append(granted_user_id)
    if action is not None:
        where.append("l.action = %s")
        params.append(action)
    params.append(max(1, min(limit, LIMIT_MAX)))
    return db.query(
        _SELECT + f" WHERE {' AND '.join(where)} ORDER BY l.created_at DESC LIMIT %s",
        tuple(params),
    )
