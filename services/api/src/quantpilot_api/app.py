"""FastAPI application: node/terminal/account routes + health (PRD 13.2).

All responses use the unified envelope {data, error, request_id, timestamp}.
This slice has NO auth/RBAC yet; trade commands are channel-only: the EA
acknowledges receipt and never executes (OBSERVE_ONLY, PRD 21.3). The cloud
risk gate (PRD 9) refuses risk-increasing commands BEFORE they are queued:
they land in terminal state REJECTED and are never delivered.
"""

from __future__ import annotations

import json
import logging
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from typing import Literal
from uuid import UUID

import psycopg.errors
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator

from quantpilot_contracts.models import (
    NodeHeartbeatRequest,
    NodeRegisterRequest,
    TradeCommandCreate,
)

from . import command_service, db, risk, writers
from .account_config import AccountConfig
from .consumer import NodeEventsConsumer
from .envelope import err, ok
from .strategies import REGISTRY as _STRATEGY_REGISTRY
from .sweeper import CommandSweeper

logger = logging.getLogger("quantpilot.api")

_consumer: NodeEventsConsumer | None = None
_sweeper: CommandSweeper | None = None


@asynccontextmanager
async def lifespan(app: FastAPI):
    global _consumer, _sweeper
    db.init_pool()
    _consumer = NodeEventsConsumer()
    _consumer.start()
    _sweeper = CommandSweeper()
    _sweeper.start()
    logger.info("api up: pool ready, consumer + sweeper running")
    yield
    if _sweeper is not None:
        _sweeper.stop()
    if _consumer is not None:
        _consumer.stop()
    db.close_pool()
    logger.info("api down")


app = FastAPI(title="QuantPilot AI API", version="0.1.0", lifespan=lifespan)


# --------------------------------------------------------------------------
# health
# --------------------------------------------------------------------------

@app.get("/healthz")
def healthz() -> JSONResponse:
    try:
        row = db.query_one("SELECT 1 AS one")
        assert row is not None
    except Exception as exc:
        return err(503, "UNHEALTHY", f"db unreachable: {exc}")
    return ok({"status": "ok", "db": "reachable"})


# --------------------------------------------------------------------------
# nodes (PRD 13.2)
# --------------------------------------------------------------------------

@app.post("/api/v1/nodes/register")
def register_node(body: NodeRegisterRequest, request: Request) -> JSONResponse:
    node = writers.upsert_node(body.device_id, body.hostname, body.os_info, body.agent_version)
    return ok(node, request)


@app.post("/api/v1/nodes/heartbeat")
def node_heartbeat(body: NodeHeartbeatRequest, request: Request) -> JSONResponse:
    try:
        node_uuid = UUID(body.node_id)
    except (ValueError, AttributeError, TypeError):
        raise HTTPException(status_code=400, detail="invalid node_id")
    row = db.query_one(
        "SELECT id::text AS node_id, status FROM trading_nodes WHERE id = %s",
        (node_uuid,),
    )
    if row is None:
        raise HTTPException(status_code=404, detail="node not found")
    db.execute(
        "UPDATE trading_nodes SET last_seen_at = now(), status = 'ONLINE', updated_at = now() WHERE id = %s",
        (node_uuid,),
    )
    return ok({"node_id": body.node_id, "status": "ONLINE"}, request)


@app.get("/api/v1/nodes")
def list_nodes(request: Request) -> JSONResponse:
    rows = db.query(
        """
        SELECT n.id::text AS node_id, n.device_id, n.hostname, n.os_info,
               n.agent_version, n.status, n.last_seen_at, n.version,
               (SELECT count(*) FROM mt5_terminals t WHERE t.node_id = n.id) AS terminals,
               (SELECT count(*) FROM trading_accounts a
                  JOIN mt5_terminals t2 ON a.terminal_id = t2.id
                 WHERE t2.node_id = n.id) AS accounts
          FROM trading_nodes n
         ORDER BY n.created_at
        """
    )
    return ok(db.jsonable(rows), request)


@app.get("/api/v1/terminals")
def list_terminals(request: Request) -> JSONResponse:
    rows = db.query(
        """
        SELECT t.id::text AS terminal_id, t.node_id::text AS node_id, n.device_id,
               t.terminal_path_hash, t.terminal_build, t.terminal_company,
               t.server, t.status, t.last_seen_at
          FROM mt5_terminals t
          JOIN trading_nodes n ON n.id = t.node_id
         ORDER BY t.created_at
        """
    )
    return ok(db.jsonable(rows), request)


# --------------------------------------------------------------------------
# accounts / snapshots / symbols
# --------------------------------------------------------------------------

@app.get("/api/v1/accounts")
def list_accounts(request: Request) -> JSONResponse:
    rows = db.query(
        """
        SELECT a.id::text AS account_id, a.terminal_id::text AS terminal_id,
               t.node_id::text AS node_id, a.login_ref, a.server, a.company,
               a.currency, a.margin_mode, a.account_mode, a.leverage,
               a.status, a.last_seen_at
          FROM trading_accounts a
          JOIN mt5_terminals t ON t.id = a.terminal_id
         ORDER BY a.created_at
        """
    )
    return ok(db.jsonable(rows), request)


@app.get("/api/v1/accounts/{account_id}/snapshot")
def account_snapshot(account_id: str, request: Request) -> JSONResponse:
    row = db.query_one(
        """
        SELECT s.id::text AS snapshot_id, s.account_id::text AS account_id,
               s.balance, s.equity, s.margin, s.free_margin, s.currency,
               s.open_positions, s.connected, s.trade_allowed, s.expert_allowed,
               s.terminal_version, s.ea_version, s.captured_at
          FROM account_snapshots s
         WHERE s.account_id = %s
         ORDER BY s.captured_at DESC
         LIMIT 1
        """,
        (account_id,),
    )
    if row is None:
        raise HTTPException(status_code=404, detail="no snapshots for account")
    return ok(db.jsonable(row), request)


@app.get("/api/v1/accounts/{account_id}/snapshots")
def account_snapshots_history(account_id: str, request: Request, limit: int = 500) -> JSONResponse:
    """Read-only snapshot history (equity curve source). Oldest-first."""
    limit = max(1, min(limit, 2000))
    rows = db.query(
        """
        SELECT s.balance, s.equity, s.margin, s.free_margin,
               s.open_positions, s.captured_at
          FROM account_snapshots s
         WHERE s.account_id = %s
         ORDER BY s.captured_at DESC
         LIMIT %s
        """,
        (account_id, limit),
    )
    rows.reverse()
    return ok(db.jsonable(rows), request)


@app.get("/api/v1/accounts/{account_id}/positions")
def account_positions(account_id: str, request: Request) -> JSONResponse:
    """Open + recently closed position projection (doc 13.2, read-only).

    profit is the last observed floating profit (realized P/L needs deals).
    """
    rows = db.query(
        """
        SELECT p.id::text AS position_id, p.account_id::text AS account_id,
               p.broker_position_ticket, p.broker_symbol, p.direction,
               p.volume, p.price_open, p.current_price, p.sl, p.tp,
               p.profit, p.swap, p.open_time, p.status,
               p.last_reported_at, p.closed_at
          FROM positions p
         WHERE p.account_id = %s
         ORDER BY (p.status = 'OPEN') DESC, p.open_time DESC
         LIMIT 500
        """,
        (account_id,),
    )
    return ok(db.jsonable(rows), request)


@app.get("/api/v1/accounts/{account_id}/orders")
def account_orders(account_id: str, request: Request) -> JSONResponse:
    """Pending-order projection (doc 13.2, read-only, state-synced from EA).

    status REMOVED means the order left the terminal (cancelled / expired /
    filled); the matching deal, if any, arrives via the deals channel.
    """
    rows = db.query(
        """
        SELECT o.id::text AS order_id, o.account_id::text AS account_id,
               o.broker_ticket, o.broker_symbol, o.direction, o.order_kind,
               o.volume, o.price, o.sl, o.tp, o.time_setup, o.time_expiration,
               o.status, o.last_reported_at, o.removed_at
          FROM orders o
         WHERE o.account_id = %s
         ORDER BY (o.status = 'ACTIVE') DESC, o.time_setup DESC
         LIMIT 500
        """,
        (account_id,),
    )
    return ok(db.jsonable(rows), request)


@app.get("/api/v1/accounts/{account_id}/deals")
def account_deals(account_id: str, request: Request, limit: int = 500) -> JSONResponse:
    """Closed-trade history from the EA account history (read-only).

    deal_time is broker server time; MQL5 exposes no server-timezone API, so
    timestamps follow the server clock (documented approximation).
    """
    limit = max(1, min(limit, 2000))
    rows = db.query(
        """
        SELECT d.id::text AS deal_id, d.account_id::text AS account_id,
               d.broker_deal_ticket, d.broker_order_ticket, d.broker_position_ticket,
               d.broker_symbol, d.direction, d.entry, d.volume, d.price,
               d.commission, d.swap, d.profit, d.deal_time, d.magic, d.comment
          FROM deals d
         WHERE d.account_id = %s
         ORDER BY d.deal_time DESC, d.broker_deal_ticket DESC
         LIMIT %s
        """,
        (account_id, limit),
    )
    return ok(db.jsonable(rows), request)


@app.get("/api/v1/accounts/{account_id}/performance")
def account_performance(account_id: str, request: Request, days: int = 30) -> JSONResponse:
    """Derived read-only trading statistics computed from the deals projection.

    A "closed trade" is one OUT/OUT_BY deal; its realized P/L is
    profit + swap + commission as reported by the broker. Daily buckets and
    periods follow the broker server clock (see /deals note).
    """
    days = max(1, min(days, 90))
    closers = db.query(
        """
        SELECT d.entry, d.profit, d.swap, d.commission, d.deal_time
          FROM deals d
         WHERE d.account_id = %s AND d.entry IN ('OUT', 'OUT_BY')
         ORDER BY d.deal_time DESC, d.broker_deal_ticket DESC
         LIMIT 10000
        """,
        (account_id,),
    )
    closers.reverse()  # oldest first for cumulative metrics
    totals = db.query_one(
        """
        SELECT count(*) AS deal_count,
               COALESCE(SUM(commission), 0) AS commission,
               COALESCE(SUM(swap), 0) AS swap
          FROM deals
         WHERE account_id = %s
        """,
        (account_id,),
    )
    zero = Decimal("0")
    nets: list[Decimal] = []
    times: list = []
    for row in closers:
        nets.append((row["profit"] or zero) + (row["swap"] or zero) + (row["commission"] or zero))
        times.append(row["deal_time"])

    wins = [n for n in nets if n > 0]
    losses = [n for n in nets if n < 0]
    gross_profit = sum(wins, zero)
    gross_loss = sum((-n for n in losses), zero)
    net = gross_profit - gross_loss
    closed = len(nets)

    # streaks + cumulative curve (max drawdown)
    streak_win = streak_loss = max_win = max_loss = 0
    peak = zero
    max_dd = zero
    cum = zero
    for n in nets:
        if n > 0:
            streak_win += 1
            streak_loss = 0
        elif n < 0:
            streak_loss += 1
            streak_win = 0
        max_win = max(max_win, streak_win)
        max_loss = max(max_loss, streak_loss)
        cum += n
        if cum > peak:
            peak = cum
        if peak - cum > max_dd:
            max_dd = peak - cum

    # daily buckets, last N days (zero-filled)
    today = datetime.now(timezone.utc).date()
    daily_map: dict = {}
    for n, t in zip(nets, times):
        bucket = daily_map.setdefault(t.date(), {"profit": zero, "loss": zero, "net": zero, "trades": 0})
        if n > 0:
            bucket["profit"] += n
        elif n < 0:
            bucket["loss"] += -n
        bucket["net"] += n
        bucket["trades"] += 1
    daily = []
    for i in range(days - 1, -1, -1):
        day = today - timedelta(days=i)
        bucket = daily_map.get(day)
        if bucket:
            daily.append({
                "date": day.isoformat(), **bucket,
            })
        else:
            daily.append({"date": day.isoformat(), "profit": zero, "loss": zero, "net": zero, "trades": 0})

    def _q(numerator: Decimal, denominator: Decimal) -> str | None:
        return str(round(numerator / denominator, 6)) if denominator != 0 else None

    def _r(value: Decimal) -> str:
        return str(value.quantize(Decimal("0.01")))

    stats = {
        "closed_trades": closed,
        "wins": len(wins),
        "losses": len(losses),
        "win_rate": _q(Decimal(len(wins)), Decimal(closed)) if closed else None,
        "net_profit": _r(net),
        "gross_profit": _r(gross_profit),
        "gross_loss": _r(gross_loss),
        "profit_factor": _q(gross_profit, gross_loss) if gross_loss != 0 else None,
        "expectancy": _r(net / closed) if closed else None,
        "avg_win": _r(gross_profit / len(wins)) if wins else None,
        "avg_loss": _r(gross_loss / len(losses)) if losses else None,
        "max_consecutive_wins": max_win,
        "max_consecutive_losses": max_loss,
        "best_trade": _r(max(nets)) if nets else None,
        "worst_trade": _r(min(nets)) if nets else None,
        "max_drawdown": _r(max_dd),
        "recovery_factor": str(round(net / max_dd, 4)) if max_dd > 0 and closed else None,
        "total_commission": _r(totals["commission"]) if totals else "0.00",
        "total_swap": _r(totals["swap"]) if totals else "0.00",
        "all_deals": totals["deal_count"] if totals else 0,
    }
    today_net = daily[-1]["net"] if daily else zero
    week_net = sum((bucket["net"] for bucket in daily[-7:]), zero)
    month_net = sum((bucket["net"] for bucket in daily), zero)
    period = {
        "today_net": _r(today_net),
        "week_net": _r(week_net),
        "month_net": _r(month_net),
        "last_trade_net": _r(nets[-1]) if nets else None,
        "last_trade_at": times[-1].isoformat() if times else None,
    }
    return ok(db.jsonable({"stats": stats, "daily": daily, "period": period}), request)


@app.get("/api/v1/accounts/{account_id}/symbols")
def account_symbols(account_id: str, request: Request) -> JSONResponse:
    rows = db.query(
        """
        SELECT bs.id::text AS broker_symbol_id, bs.broker_symbol,
               bs.enabled, bs.visible, bs.selected, bs.tradable,
               cs.canonical_instrument,
               sp.spec_hash, sp.observed_at,
               q.spread_points, q.bid, q.ask, q.observed_at AS quote_observed_at
          FROM broker_symbols bs
          JOIN symbols cs ON cs.id = bs.canonical_symbol_id
     LEFT JOIN LATERAL (
                SELECT spec_hash, observed_at FROM symbol_specs
                 WHERE broker_symbol_id = bs.id
                 ORDER BY observed_at DESC LIMIT 1
              ) sp ON true
     LEFT JOIN LATERAL (
                SELECT spread_points, bid, ask, observed_at FROM symbol_quotes
                 WHERE account_id = bs.account_id AND broker_symbol = bs.broker_symbol
              ) q ON true
         WHERE bs.account_id = %s
         ORDER BY bs.broker_symbol
        """,
        (account_id,),
    )
    return ok(db.jsonable(rows), request)


@app.get("/api/v1/accounts/{account_id}/calendar")
def account_calendar(account_id: str, request: Request, hours: int = 48) -> JSONResponse:
    """Economic calendar reported by the EA (MT5 built-in, read-only).

    Window: past 2 hours (recent releases keep their actual values) through
    `hours` ahead, oldest-first. Values are raw broker figures; multiplier
    and digits are included for display formatting.
    """
    hours = max(1, min(hours, 168))
    rows = db.query(
        """
        SELECT c.id::text AS calendar_event_id, c.event_uid, c.country_code,
               c.currency, c.name, c.importance, c.multiplier, c.digits,
               c.event_time, c.actual, c.forecast, c.previous, c.observed_at
          FROM calendar_events c
         WHERE c.account_id = %s
           AND c.event_time >= now() - interval '2 hours'
           AND c.event_time <= now() + make_interval(hours => %s)
         ORDER BY c.event_time
        """,
        (account_id, hours),
    )
    return ok(db.jsonable(rows), request)


# --------------------------------------------------------------------------
# system events (doc 12.2/12.3) - real event stream written by the consumer
# (ea.registered / ea.shutdown / IDENTITY_MISMATCH etc.), read-only.
# --------------------------------------------------------------------------

@app.get("/api/v1/events")
def list_system_events(request: Request, limit: int = 100, severity: str | None = None) -> JSONResponse:
    """System event feed, newest-first. Optional severity filter (INFO/WARN/ERROR)."""
    limit = max(1, min(limit, 500))
    base = """
        SELECT e.id::text AS event_id, e.source, e.event_type, e.severity,
               e.payload, e.occurred_at
          FROM system_events e
    """
    if severity is None:
        rows = db.query(base + " ORDER BY e.occurred_at DESC LIMIT %s", (limit,))
    else:
        rows = db.query(
            base + " WHERE e.severity = %s ORDER BY e.occurred_at DESC LIMIT %s",
            (severity, limit),
        )
    return ok(db.jsonable(rows), request)


# --------------------------------------------------------------------------
# account config (doc 12.2 risk_profiles) - filters/expert are handed down to
# the EA (which only logs them, PRD 21.3); the risk section is enforced by
# the cloud gate at command creation (PRD 9). Shape: account_config.py.
# --------------------------------------------------------------------------

class ConfigLookupRequest(BaseModel):
    """Identity tuple -> account config (called by the local agent for an EA)."""

    model_config = ConfigDict(extra="forbid")

    data_path_hash: str
    login_ref: str
    server: str


def _account_or_404(account_id: str) -> UUID:
    try:
        account_uuid = UUID(account_id)
    except (ValueError, AttributeError, TypeError):
        raise HTTPException(status_code=404, detail="account not found")
    if db.query_one("SELECT id FROM trading_accounts WHERE id = %s", (account_uuid,)) is None:
        raise HTTPException(status_code=404, detail="account not found")
    return account_uuid


def _config_payload(row: dict | None, account_id: str | None) -> dict:
    if row is None:
        return {
            "account_id": account_id,
            "version": 0,
            "status": "DEFAULT",
            "source": "default",
            "config": AccountConfig().model_dump(mode="json"),
            "created_at": None,
        }
    return {
        "account_id": account_id,
        "version": row["version"],
        "status": row["status"],
        "source": "cloud",
        "config": row["config_json"],
        "created_at": row["created_at"],
    }


def _active_config_row(account_uuid: UUID) -> dict | None:
    return db.query_one(
        """
        SELECT version, config_json, status, created_at
          FROM risk_profiles
         WHERE account_id = %s AND status = 'ACTIVE'
         ORDER BY version DESC
         LIMIT 1
        """,
        (account_uuid,),
    )


@app.get("/api/v1/accounts/{account_id}/config")
def get_account_config(account_id: str, request: Request) -> JSONResponse:
    """Current account config; falls back to defaults when none was saved."""
    account_uuid = _account_or_404(account_id)
    row = _active_config_row(account_uuid)
    return ok(db.jsonable(_config_payload(row, account_id)), request)


@app.put("/api/v1/accounts/{account_id}/config")
def put_account_config(account_id: str, body: AccountConfig, request: Request) -> JSONResponse:
    """Save a new config version: ACTIVE -> SUPERSEDED, then version = max+1.

    Versioned append-only (doc 21.1): every change stays auditable.
    """
    account_uuid = _account_or_404(account_id)
    doc = json.dumps(body.model_dump(mode="json"))
    with db.conn() as connection:
        connection.execute(
            "UPDATE risk_profiles SET status = 'SUPERSEDED' WHERE account_id = %s AND status = 'ACTIVE'",
            (account_uuid,),
        )
        connection.execute(
            """
            INSERT INTO risk_profiles (account_id, version, config_json)
            VALUES (%s,
                    (SELECT COALESCE(MAX(version), 0) + 1 FROM risk_profiles WHERE account_id = %s),
                    CAST(%s AS jsonb))
            """,
            (account_uuid, account_uuid, doc),
        )
    row = _active_config_row(account_uuid)
    return ok(db.jsonable(_config_payload(row, account_id)), request)


def _resolve_account_by_identity(data_path_hash: str, login_ref: str, server: str) -> dict | None:
    """Identity tuple (terminal data-path hash + login/server) -> account row.

    Used by /config/lookup and /commands/pending; the cloud stays stateless
    per doc 4.4 - no extra round-trip is stored on the agent.
    """
    return db.query_one(
        """
        SELECT a.id::text AS account_id
          FROM trading_accounts a
          JOIN mt5_terminals t ON t.id = a.terminal_id
         WHERE t.terminal_path_hash = %s AND a.login_ref = %s AND a.server = %s
         ORDER BY a.last_seen_at DESC NULLS LAST
         LIMIT 1
        """,
        (data_path_hash, login_ref, server),
    )


@app.post("/api/v1/config/lookup")
def config_lookup(body: ConfigLookupRequest, request: Request) -> JSONResponse:
    """Resolve an identity tuple (terminal data-path hash + login/server) to
    the account config. Unresolvable identities get the defaults with
    resolved=false (cloud stays stateless per doc 4.4)."""
    row = _resolve_account_by_identity(body.data_path_hash, body.login_ref, body.server)
    if row is None:
        payload = _config_payload(None, None)
        payload["resolved"] = False
        return ok(db.jsonable(payload), request)
    account_uuid = _account_or_404(row["account_id"])
    active = _active_config_row(account_uuid)
    payload = _config_payload(active, row["account_id"])
    payload["resolved"] = True
    return ok(db.jsonable(payload), request)


# --------------------------------------------------------------------------
# trade commands (PRD 13.3-13.5, ADR-004) - durable channel + idempotency +
# UNKNOWN reconciliation + cloud risk gate (PRD 9). The gate runs at creation
# for risk-increasing types; REJECT persists the command in terminal state
# REJECTED (never delivered, sweeper ignores it). OBSERVE_ONLY overall: even
# an ALLOWED command is only acked, never executed by the EA (PRD 21.3).
# --------------------------------------------------------------------------

COMMAND_STATUSES = ("QUEUED", "DELIVERED", "ACKED", "EXPIRED", "UNKNOWN", "REJECTED")

_COMMAND_SELECT = """
    SELECT c.command_id::text AS command_id, c.account_id::text AS account_id,
           c.command_type, c.payload, c.status, c.expires_at,
           c.created_by, c.strategy_instance_id, c.signal_id,
           c.delivery_attempts, c.delivered_at, c.acked_at, c.ack_code,
           c.ack_message, c.reconciled, c.unknown_at, c.expired_at,
           c.created_at, c.updated_at
      FROM commands c
"""


class CommandPendingRequest(BaseModel):
    """POST /api/v1/commands/pending body (agent -> cloud).

    recently_acked echoes command_ids the EA already processed; the cloud
    reconciles DELIVERED/UNKNOWN commands to ACKED without resending (AT-006).
    """

    model_config = ConfigDict(extra="forbid")

    data_path_hash: str
    login_ref: str
    server: str
    recently_acked: list[UUID] = []


def _command_by_id(command_uuid: UUID) -> dict | None:
    return db.query_one(_COMMAND_SELECT + " WHERE c.command_id = %s", (command_uuid,))


def _command_payload(row: dict, created: bool | None) -> dict:
    payload = db.jsonable(row)
    if created is not None:
        payload["created"] = created
    return payload


@app.post("/api/v1/commands")
def create_command(body: TradeCommandCreate, request: Request) -> JSONResponse:
    """HTTP envelope for the shared durable-command creation core."""
    try:
        payload = command_service.create_command(body)
    except command_service.CommandError as exc:
        raise HTTPException(exc.status_code, exc.detail) from exc
    return ok(payload, request)


@app.get("/api/v1/commands")
def list_commands(
    request: Request,
    account_id: str | None = None,
    status: str | None = None,
    limit: int = 100,
) -> JSONResponse:
    """Command list, newest-first; optional account/status filters."""
    limit = max(1, min(limit, 500))
    if status is not None and status not in COMMAND_STATUSES:
        raise HTTPException(400, f"status must be one of {COMMAND_STATUSES}")
    where = ["1=1"]
    params: list = []
    if account_id is not None:
        try:
            params.append(UUID(account_id))
        except (ValueError, AttributeError, TypeError):
            raise HTTPException(400, "invalid account_id")
        where.append("c.account_id = %s")
    if status is not None:
        params.append(status)
        where.append("c.status = %s")
    rows = db.query(
        f"{_COMMAND_SELECT} WHERE {' AND '.join(where)} ORDER BY c.created_at DESC LIMIT %s",
        (*params, limit),
    )
    return ok(db.jsonable(rows), request)


@app.get("/api/v1/commands/{command_id}")
def get_command(command_id: str, request: Request) -> JSONResponse:
    """Current command state (PRD 13.5: the frontend must show the REAL state),
    including the risk-gate decision when the command was gated."""
    try:
        command_uuid = UUID(command_id)
    except (ValueError, AttributeError, TypeError):
        raise HTTPException(404, "command not found")
    row = _command_by_id(command_uuid)
    if row is None:
        raise HTTPException(404, "command not found")
    payload = _command_payload(row, created=None)
    decision = risk.decision_for_command(command_uuid)
    if decision is not None:
        payload["risk_decision"] = decision
    return ok(payload, request)


@app.post("/api/v1/commands/pending")
def pending_commands(body: CommandPendingRequest, request: Request) -> JSONResponse:
    """Agent -> cloud: hand out the next QUEUED command of an identity-resolved
    account - at most ONE per fetch (PRD 2.5 per-account serialization) - and
    reconcile the EA's recently-acked ids first (AT-006).

    Fetching marks the command DELIVERED; a fetched command is never handed
    out again even if the EA never saw it - it becomes UNKNOWN instead
    (PRD 5.5: no blind resend).
    """
    resolved = _resolve_account_by_identity(body.data_path_hash, body.login_ref, body.server)
    if resolved is None:
        return ok({"commands": [], "reconciled": 0, "resolved": False}, request)
    account_uuid = UUID(resolved["account_id"])
    reconciled = 0
    if body.recently_acked:
        with db.conn() as connection:
            rows = connection.execute(
                """
                UPDATE commands
                   SET status = 'ACKED', acked_at = now(),
                       ack_code = 'RECONCILED',
                       ack_message = 'reconciled via EA recently-acked echo',
                       reconciled = true, unknown_at = NULL,
                       updated_at = now(), version = version + 1
                 WHERE account_id = %s AND command_id = ANY(CAST(%s AS uuid[]))
                   AND status IN ('DELIVERED', 'UNKNOWN')
                RETURNING command_id::text, account_id::text
                """,
                (account_uuid, [str(u) for u in body.recently_acked]),
            ).fetchall()
        reconciled = len(rows)
        for row in rows:
            writers.add_system_event(
                source="api",
                event_type="command.reconciled",
                severity="INFO",
                payload={
                    "command_id": row[0],
                    "account_id": row[1],
                    "detail": "DELIVERED/UNKNOWN command matched the EA's"
                              " recently-acked echo (AT-006)",
                },
            )
    with db.conn() as connection:
        row = connection.execute(
            """
            UPDATE commands
               SET status = 'DELIVERED', delivered_at = now(),
                   delivery_attempts = delivery_attempts + 1,
                   updated_at = now(), version = version + 1
             WHERE id IN (
                    SELECT id FROM commands
                     WHERE account_id = %s AND status = 'QUEUED' AND expires_at > now()
                     ORDER BY created_at
                      FOR UPDATE SKIP LOCKED
                     LIMIT 1
                 )
            RETURNING command_id::text, command_type, payload, expires_at
            """,
            (account_uuid,),
        ).fetchone()
    commands = []
    if row is not None:
        expires = row[3]
        commands.append(
            {
                "command_id": row[0],
                "command_type": row[1],
                "payload": row[2],
                "expires_at": expires.isoformat(),
                # unix seconds let the EA compare against TimeGMT() without
                # parsing ISO timestamps (MQL5 has no ISO parser, PRD 21.3)
                "expires_at_unix": int(expires.timestamp()),
            }
        )
        writers.add_system_event(
            source="api",
            event_type="command.delivered",
            severity="INFO",
            payload={
                "command_id": row[0],
                "account_id": str(account_uuid),
                "detail": "handed to the local agent for the EA"
                          " (one per fetch, per-account serialization)",
            },
        )
    return ok({"commands": commands, "reconciled": reconciled, "resolved": True}, request)


# --------------------------------------------------------------------------
# risk engine: stop states + gate decisions (PRD 9, 3.3, doc 12.2)
# --------------------------------------------------------------------------

_RISK_STATE_SELECT = """
    SELECT scope, account_id::text AS account_id, status, reason,
           updated_by, updated_at, version
      FROM risk_states
"""


class RiskStopRequest(BaseModel):
    """POST /api/v1/risk/stop body: durable STOP_NEW_RISK (PRD 3.3).

    scope=GLOBAL stops every account; scope=ACCOUNT stops one. locked=True
    writes LOCKED (9.5: needs a human to confirm the resume explicitly).
    """

    model_config = ConfigDict(extra="forbid")

    scope: Literal["GLOBAL", "ACCOUNT"]
    account_id: UUID | None = None
    reason: str = Field(default="", max_length=512)
    locked: bool = False
    updated_by: str = Field(default="api", max_length=64)

    @model_validator(mode="after")
    def _scope_matches_account(self) -> "RiskStopRequest":
        if self.scope == "ACCOUNT" and self.account_id is None:
            raise ValueError("scope=ACCOUNT requires account_id")
        if self.scope == "GLOBAL" and self.account_id is not None:
            raise ValueError("scope=GLOBAL must not carry account_id")
        return self


class RiskResumeRequest(RiskStopRequest):
    """POST /api/v1/risk/resume body: back to NORMAL (idempotent)."""

    model_config = ConfigDict(extra="forbid")

    confirm_locked: bool = Field(
        default=False, description="required to leave the LOCKED state (PRD 9.5)"
    )


def _stop_row(scope: str, account_uuid: UUID | None) -> dict | None:
    if scope == "GLOBAL":
        return db.query_one(
            f"{_RISK_STATE_SELECT} WHERE scope = 'GLOBAL' AND account_id IS NULL",
            (),
        )
    return db.query_one(
        f"{_RISK_STATE_SELECT} WHERE scope = 'ACCOUNT' AND account_id = %s",
        (account_uuid,),
    )


def _state_payload(row: dict | None, scope: str, account_id: str | None) -> dict:
    if row is None:
        # absence means NORMAL (only the GLOBAL row is seeded)
        return {
            "scope": scope,
            "account_id": account_id,
            "status": "NORMAL",
            "reason": "",
            "updated_by": None,
            "updated_at": None,
            "version": 0,
        }
    payload = db.jsonable(row)
    payload["account_id"] = account_id if row["account_id"] is None else row["account_id"]
    return payload


@app.get("/api/v1/risk/state")
def risk_state(request: Request, account_id: str | None = None) -> JSONResponse:
    """Effective stop states: GLOBAL + per-account rows (absent = NORMAL).

    With account_id, returns the single account's effective state.
    """
    if account_id is not None:
        account_uuid = _account_or_404(account_id)
        row = _stop_row("ACCOUNT", account_uuid)
        return ok(_state_payload(row, "ACCOUNT", account_id), request)
    global_row = _stop_row("GLOBAL", None)
    account_rows = db.query(
        f"{_RISK_STATE_SELECT} WHERE scope = 'ACCOUNT' ORDER BY updated_at DESC"
    )
    return ok(
        {
            "global": _state_payload(global_row, "GLOBAL", None),
            "accounts": [_state_payload(r, "ACCOUNT", r["account_id"]) for r in account_rows],
        },
        request,
    )


@app.post("/api/v1/risk/stop")
def risk_stop(body: RiskStopRequest, request: Request) -> JSONResponse:
    """Set STOP_NEW_RISK (or LOCKED). Durable + audited (PRD 3.3: the state
    change must never be lost). Broadcast to the EA/agent is a later slice;
    the cloud gate reads this state on every new-risk command."""
    account_uuid: UUID | None = None
    if body.scope == "ACCOUNT":
        account_uuid = _account_or_404(str(body.account_id))
    status = "LOCKED" if body.locked else "STOP_NEW_RISK"
    conflict_target = (
        "ON CONFLICT (scope) WHERE account_id IS NULL"
        if body.scope == "GLOBAL"
        else "ON CONFLICT (account_id) WHERE account_id IS NOT NULL"
    )
    with db.conn() as connection:
        connection.execute(
            f"""
            INSERT INTO risk_states (scope, account_id, status, reason, updated_by)
            VALUES (%s, %s, %s, %s, %s)
            {conflict_target}
            DO UPDATE SET status = EXCLUDED.status,
                          reason = EXCLUDED.reason,
                          updated_by = EXCLUDED.updated_by,
                          updated_at = now(),
                          version = risk_states.version + 1
            """,
            (body.scope, account_uuid, status, body.reason, body.updated_by),
        )
    writers.add_system_event(
        source="api",
        event_type="risk.stop_set",
        severity="WARN",
        payload={
            "scope": body.scope,
            "account_id": str(account_uuid) if account_uuid else None,
            "status": status,
            "reason": body.reason,
            "updated_by": body.updated_by,
        },
    )
    row = _stop_row(body.scope, account_uuid)
    assert row is not None
    return ok(_state_payload(row, body.scope, str(account_uuid) if account_uuid else None), request)


@app.post("/api/v1/risk/resume")
def risk_resume(body: RiskResumeRequest, request: Request) -> JSONResponse:
    """Return to NORMAL. LOCKED requires confirm_locked=true (PRD 9.5)."""
    account_uuid: UUID | None = None
    if body.scope == "ACCOUNT":
        account_uuid = _account_or_404(str(body.account_id))
    row = _stop_row(body.scope, account_uuid)
    if row is None:
        # already NORMAL - idempotent no-op
        return ok(_state_payload(None, body.scope, str(account_uuid) if account_uuid else None), request)
    if row["status"] == "LOCKED" and not body.confirm_locked:
        raise HTTPException(409, "LOCKED state requires confirm_locked=true to resume")
    if row["status"] == "NORMAL":
        return ok(_state_payload(row, body.scope, str(account_uuid) if account_uuid else None), request)
    with db.conn() as connection:
        # scope-scoped WHERE built per branch: a NULL param inside
        # "IS NULL" cannot be type-inferred by PostgreSQL
        if body.scope == "GLOBAL":
            connection.execute(
                """
                UPDATE risk_states
                   SET status = 'NORMAL', reason = '', updated_by = %s,
                       updated_at = now(), version = version + 1
                 WHERE scope = 'GLOBAL' AND account_id IS NULL
                """,
                (body.updated_by,),
            )
        else:
            connection.execute(
                """
                UPDATE risk_states
                   SET status = 'NORMAL', reason = '', updated_by = %s,
                       updated_at = now(), version = version + 1
                 WHERE scope = 'ACCOUNT' AND account_id = %s
                """,
                (body.updated_by, account_uuid),
            )
    writers.add_system_event(
        source="api",
        event_type="risk.stop_cleared",
        severity="INFO",
        payload={
            "scope": body.scope,
            "account_id": str(account_uuid) if account_uuid else None,
            "updated_by": body.updated_by,
            "detail": "resumed to NORMAL; new-risk commands pass the gate again",
        },
    )
    row = _stop_row(body.scope, account_uuid)
    assert row is not None
    return ok(_state_payload(row, body.scope, str(account_uuid) if account_uuid else None), request)


@app.get("/api/v1/risk/decisions")
def risk_decisions(
    request: Request,
    account_id: str | None = None,
    command_id: str | None = None,
    limit: int = 100,
) -> JSONResponse:
    """Gate decision audit trail (doc 12.2 risk_decisions), newest first."""
    limit = max(1, min(limit, 500))
    where = ["1=1"]
    params: list = []
    if account_id is not None:
        try:
            params.append(UUID(account_id))
        except (ValueError, AttributeError, TypeError):
            raise HTTPException(400, "invalid account_id")
        where.append("d.account_id = %s")
    if command_id is not None:
        try:
            params.append(UUID(command_id))
        except (ValueError, AttributeError, TypeError):
            raise HTTPException(400, "invalid command_id")
        where.append("d.command_id = %s")
    rows = db.query(
        f"""
        SELECT d.id::text AS id, d.account_id::text AS account_id,
               d.command_id::text AS command_id, d.signal_id, d.decision,
               d.reason_codes, d.config_version, d.decided_at
          FROM risk_decisions d
         WHERE {' AND '.join(where)}
         ORDER BY d.decided_at DESC
         LIMIT %s
        """,
        (*params, limit),
    )
    return ok(db.jsonable(rows), request)


# --------------------------------------------------------------------------
# strategy engine (PRD 7): catalog + instances + signals + bars.
# Strategies produce advisory signals only - no endpoint here creates a
# command (7.1); the signal -> risk gate -> command wiring is a later slice.
# Instance configs are validated against the version's Pydantic model and
# every change produces a new config_revision (7.6: never overwritten).
# --------------------------------------------------------------------------

_TIMEFRAMES = ("M5", "M15", "M30", "H1", "H4")

_INSTANCE_SELECT = """
    SELECT si.id::text AS instance_id, si.account_id::text AS account_id,
           s.code AS strategy_code, sv.version AS strategy_version,
           sv.id::text AS strategy_version_id, si.broker_symbol, si.timeframe,
           si.config, si.config_revision, si.status,
           si.created_at, si.updated_at
      FROM strategy_instances si
      JOIN strategy_versions sv ON sv.id = si.strategy_version_id
      JOIN strategies s ON s.id = sv.strategy_id
"""


class StrategyInstanceCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    strategy_code: str
    strategy_version: str = "1.0.0"
    broker_symbol: str = Field(min_length=1)
    timeframe: Literal["M5", "M15", "M30", "H1", "H4"]
    config: dict = Field(default_factory=dict)
    status: Literal["ENABLED", "DISABLED"] = "ENABLED"


class StrategyInstanceStatusUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    status: Literal["ENABLED", "DISABLED"]


class StrategyInstanceConfigUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    config: dict


def _validate_instance_config(strategy_code: str, config: dict) -> dict:
    """Validate config against the registered implementation's Pydantic
    model (7.6). Returns the JSON-mode dict (money-ish values as decimal
    strings, matching the seeded config_schema)."""
    cls = _STRATEGY_REGISTRY.get(strategy_code)
    if cls is None:
        raise HTTPException(400, f"no python implementation registered for strategy {strategy_code!r}")
    try:
        return cls.validate_config(config).model_dump(mode="json")
    except ValidationError as exc:
        raise HTTPException(400, f"invalid strategy config: {exc.errors()[0]['msg']} ({exc.errors()[0]['loc']})")


@app.get("/api/v1/strategies")
def list_strategies(request: Request) -> JSONResponse:
    """Strategy catalog with versions + config schemas (doc 12.2)."""
    rows = db.query(
        """
        SELECT s.id::text AS strategy_id, s.code, s.name, s.description, s.status,
               v.id::text AS strategy_version_id, v.version, v.artifact_hash,
               v.config_schema, v.created_at
          FROM strategies s
          JOIN strategy_versions v ON v.strategy_id = s.id
         ORDER BY s.code, v.created_at
        """
    )
    return ok(db.jsonable(rows), request)


@app.get("/api/v1/accounts/{account_id}/strategy-instances")
def list_strategy_instances(account_id: str, request: Request) -> JSONResponse:
    account_uuid = _account_or_404(account_id)
    rows = db.query(
        _INSTANCE_SELECT + " WHERE si.account_id = %s ORDER BY si.created_at",
        (account_uuid,),
    )
    return ok(db.jsonable(rows), request)


@app.post("/api/v1/accounts/{account_id}/strategy-instances")
def create_strategy_instance(account_id: str, body: StrategyInstanceCreate, request: Request) -> JSONResponse:
    """Bind (account, strategy version, symbol, timeframe, config revision).

    One instance per binding (409 on duplicates). The config is validated
    against the version's Pydantic model at creation and stored as revision
    1; later updates append revisions (7.6: live configs never overwritten).
    """
    account_uuid = _account_or_404(account_id)
    strategy = db.query_one(
        "SELECT id::text AS strategy_id, code, status FROM strategies WHERE code = %s",
        (body.strategy_code,),
    )
    if strategy is None:
        raise HTTPException(404, f"strategy {body.strategy_code!r} not found")
    if strategy["status"] != "ACTIVE":
        raise HTTPException(400, f"strategy {body.strategy_code!r} is {strategy['status']}")
    version = db.query_one(
        """
        SELECT id::text AS strategy_version_id, version
          FROM strategy_versions
         WHERE strategy_id = %s AND version = %s
        """,
        (strategy["strategy_id"], body.strategy_version),
    )
    if version is None:
        raise HTTPException(404, f"strategy {body.strategy_code!r} has no version {body.strategy_version!r}")
    config = _validate_instance_config(body.strategy_code, body.config)
    try:
        with db.conn() as connection:
            connection.execute(
                """
                INSERT INTO strategy_instances (account_id, strategy_version_id,
                                                broker_symbol, timeframe, config,
                                                config_revision, status)
                VALUES (%s, %s, %s, %s, CAST(%s AS jsonb), 1, %s)
                """,
                (
                    account_uuid,
                    version["strategy_version_id"],
                    body.broker_symbol,
                    body.timeframe,
                    json.dumps(config),
                    body.status,
                ),
            )
    except psycopg.errors.UniqueViolation as exc:
        raise HTTPException(409, "an instance already binds this account+strategy+symbol+timeframe") from exc
    row = db.query_one(
        _INSTANCE_SELECT + " WHERE si.account_id = %s AND si.strategy_version_id = %s"
        " AND si.broker_symbol = %s AND si.timeframe = %s",
        (account_uuid, version["strategy_version_id"], body.broker_symbol, body.timeframe),
    )
    assert row is not None
    return ok(db.jsonable(row), request)


def _instance_or_404(account_uuid: UUID, instance_id: str) -> dict:
    try:
        instance_uuid = UUID(instance_id)
    except (ValueError, AttributeError, TypeError):
        raise HTTPException(404, "strategy instance not found")
    row = db.query_one(
        _INSTANCE_SELECT + " WHERE si.id = %s AND si.account_id = %s",
        (instance_uuid, account_uuid),
    )
    if row is None:
        raise HTTPException(404, "strategy instance not found")
    return row


@app.get("/api/v1/accounts/{account_id}/strategy-instances/{instance_id}")
def get_strategy_instance(account_id: str, instance_id: str, request: Request) -> JSONResponse:
    account_uuid = _account_or_404(account_id)
    return ok(db.jsonable(_instance_or_404(account_uuid, instance_id)), request)


@app.post("/api/v1/accounts/{account_id}/strategy-instances/{instance_id}/status")
def set_strategy_instance_status(
    account_id: str, instance_id: str, body: StrategyInstanceStatusUpdate, request: Request
) -> JSONResponse:
    """Enable/disable evaluation for one instance (idempotent)."""
    account_uuid = _account_or_404(account_id)
    _instance_or_404(account_uuid, instance_id)
    with db.conn() as connection:
        connection.execute(
            """
            UPDATE strategy_instances
               SET status = %s, updated_at = now(), version = version + 1
             WHERE id = %s AND account_id = %s
            """,
            (body.status, UUID(instance_id), account_uuid),
        )
    return ok(db.jsonable(_instance_or_404(account_uuid, instance_id)), request)


@app.put("/api/v1/accounts/{account_id}/strategy-instances/{instance_id}/config")
def update_strategy_instance_config(
    account_id: str, instance_id: str, body: StrategyInstanceConfigUpdate, request: Request
) -> JSONResponse:
    """Append a new config revision (7.6: configs are immutable - updates
    produce a new revision, the old one stays in history via version)."""
    account_uuid = _account_or_404(account_id)
    instance = _instance_or_404(account_uuid, instance_id)
    config = _validate_instance_config(instance["strategy_code"], body.config)
    with db.conn() as connection:
        connection.execute(
            """
            UPDATE strategy_instances
               SET config = CAST(%s AS jsonb), config_revision = config_revision + 1,
                   updated_at = now(), version = version + 1
             WHERE id = %s AND account_id = %s
            """,
            (json.dumps(config), UUID(instance_id), account_uuid),
        )
    return ok(db.jsonable(_instance_or_404(account_uuid, instance_id)), request)


@app.get("/api/v1/accounts/{account_id}/signals")
def account_signals(
    account_id: str, request: Request, instance_id: str | None = None, limit: int = 100
) -> JSONResponse:
    """Advisory signals produced by the strategy engine (doc 7.4 inside
    signal_json), newest first."""
    account_uuid = _account_or_404(account_id)
    limit = max(1, min(limit, 500))
    where = ["ss.account_id = %s"]
    params: list = [account_uuid]
    if instance_id is not None:
        try:
            params.append(UUID(instance_id))
        except (ValueError, AttributeError, TypeError):
            raise HTTPException(400, "invalid instance_id")
        where.append("ss.strategy_instance_id = %s")
    rows = db.query(
        f"""
        SELECT ss.id::text AS id, ss.signal_id::text AS signal_id,
               ss.account_id::text AS account_id,
               ss.strategy_instance_id::text AS strategy_instance_id,
               ss.signal_json, ss.source_bar_time, ss.generated_at, ss.expires_at
          FROM strategy_signals ss
         WHERE {' AND '.join(where)}
         ORDER BY ss.generated_at DESC
         LIMIT %s
        """,
        (*params, limit),
    )
    return ok(db.jsonable(rows), request)


@app.get("/api/v1/accounts/{account_id}/bars")
def account_bars(
    account_id: str,
    request: Request,
    symbol: str,
    timeframe: str,
    limit: int = 500,
) -> JSONResponse:
    """Closed-bar history for one (account, symbol, timeframe), oldest
    first (chart-ready). Bars are immutable broker facts; the EA reports
    closed bars only."""
    account_uuid = _account_or_404(account_id)
    if timeframe not in _TIMEFRAMES:
        raise HTTPException(400, f"timeframe must be one of {_TIMEFRAMES}")
    limit = max(1, min(limit, 2000))
    rows = db.query(
        """
        SELECT b.bar_time, b.open, b.high, b.low, b.close, b.tick_volume
          FROM symbol_bars b
         WHERE b.account_id = %s AND b.broker_symbol = %s AND b.timeframe = %s
         ORDER BY b.bar_time DESC
         LIMIT %s
        """,
        (account_uuid, symbol, timeframe, limit),
    )
    rows.reverse()
    return ok(db.jsonable(rows), request)


# --------------------------------------------------------------------------
# AI analysis engine (PRD 8, ADVISORY-only in the first release)
# --------------------------------------------------------------------------

class AiAnalyzeRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    broker_symbol: str
    timeframe: str


@app.get("/api/v1/accounts/{account_id}/ai/analyses")
def account_ai_analyses(
    account_id: str,
    request: Request,
    symbol: str | None = None,
    timeframe: str | None = None,
    limit: int = 100,
) -> JSONResponse:
    """Advisory AI analyses for the account (doc 8.4 structure inside
    output_json), newest first."""
    account_uuid = _account_or_404(account_id)
    limit = max(1, min(limit, 500))
    where = ["a.account_id = %s"]
    params: list = [account_uuid]
    if symbol is not None:
        where.append("a.broker_symbol = %s")
        params.append(symbol)
    if timeframe is not None:
        if timeframe not in _TIMEFRAMES:
            raise HTTPException(400, f"timeframe must be one of {_TIMEFRAMES}")
        where.append("a.timeframe = %s")
        params.append(timeframe)
    rows = db.query(
        f"""
        SELECT a.id::text AS id, a.analysis_id::text AS analysis_id,
               a.account_id::text AS account_id, a.broker_symbol, a.timeframe,
               a.model_provider, a.model_id, a.prompt_version, a.input_hash,
               a.output_json, a.source_bar_time, a.created_at, a.expires_at,
               a.duration_ms
          FROM ai_analyses a
         WHERE {' AND '.join(where)}
         ORDER BY a.created_at DESC
         LIMIT %s
        """,
        (*params, limit),
    )
    return ok(db.jsonable(rows), request)


@app.post("/api/v1/accounts/{account_id}/ai/analyze")
def ai_analyze(account_id: str, body: AiAnalyzeRequest, request: Request) -> JSONResponse:
    """Trigger one analysis on the newest closed bar (manual / UI / testing).

    Idempotent: re-analyzing the same feature snapshot returns the stored
    row. With the account AI mode OFF (doc 8.5) no analysis runs and the
    response carries ai_mode=OFF.
    """
    _account_or_404(account_id)
    if body.timeframe not in _TIMEFRAMES:
        raise HTTPException(400, f"timeframe must be one of {_TIMEFRAMES}")
    from . import ai_engine

    mode, analysis, error = ai_engine.analyze_on_demand(
        account_id, body.broker_symbol, body.timeframe
    )
    if error is not None:
        raise HTTPException(404, error)
    return ok({"ai_mode": mode, "analysis": analysis}, request)


# --------------------------------------------------------------------------
# error handling
# --------------------------------------------------------------------------

@app.exception_handler(HTTPException)
async def http_exc_handler(request: Request, exc: HTTPException) -> JSONResponse:  # noqa: D401
    code = {404: "NOT_FOUND", 400: "BAD_REQUEST", 401: "UNAUTHORIZED", 405: "METHOD_NOT_ALLOWED"}.get(
        exc.status_code, "ERROR"
    )
    message = exc.detail if isinstance(exc.detail, str) else str(exc.detail)
    return err(exc.status_code, code, message, request)