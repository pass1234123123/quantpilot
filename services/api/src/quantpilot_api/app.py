"""FastAPI application: node/terminal/account routes + health (PRD 13.2).

All responses use the unified envelope {data, error, request_id, timestamp}.
This slice has NO auth/RBAC yet - routes are read-only or idempotent upserts;
command routes (POST /api/v1/commands etc.) are intentionally absent.
"""

from __future__ import annotations

import json
import logging
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from uuid import UUID

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field

from quantpilot_contracts.models import NodeHeartbeatRequest, NodeRegisterRequest

from . import db, writers
from .consumer import NodeEventsConsumer
from .envelope import err, ok

logger = logging.getLogger("quantpilot.api")

_consumer: NodeEventsConsumer | None = None


@asynccontextmanager
async def lifespan(app: FastAPI):
    global _consumer
    db.init_pool()
    _consumer = NodeEventsConsumer()
    _consumer.start()
    logger.info("api up: pool ready, consumer running")
    yield
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
# account config (doc 12.2 risk_profiles) - OBSERVE_ONLY slice: the config is
# stored and served for observation; nothing in this system enforces it.
# --------------------------------------------------------------------------

class FiltersConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    news_filter: bool = True
    high_impact_only: bool = True
    session_filter: bool = False
    max_spread: bool = True
    slippage_guard: bool = True
    weekend_block: bool = False
    max_daily_orders: int = Field(default=10, ge=1, le=1000)
    max_positions: int = Field(default=5, ge=1, le=500)


class ExpertConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    auto_trading: bool = True
    sound_alert: bool = False
    popup_alert: bool = True
    notification_alert: bool = False
    close_on_opposite: bool = True
    magic_number: int = Field(default=123456, ge=0)
    order_comment: str = Field(default="QuantPilot", max_length=63)


class AccountConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    filters: FiltersConfig = Field(default_factory=FiltersConfig)
    expert: ExpertConfig = Field(default_factory=ExpertConfig)


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


@app.post("/api/v1/config/lookup")
def config_lookup(body: ConfigLookupRequest, request: Request) -> JSONResponse:
    """Resolve an identity tuple (terminal data-path hash + login/server) to
    the account config. Unresolvable identities get the defaults with
    resolved=false (cloud stays stateless per doc 4.4)."""
    row = db.query_one(
        """
        SELECT a.id::text AS account_id
          FROM trading_accounts a
          JOIN mt5_terminals t ON t.id = a.terminal_id
         WHERE t.terminal_path_hash = %s AND a.login_ref = %s AND a.server = %s
         ORDER BY a.last_seen_at DESC NULLS LAST
         LIMIT 1
        """,
        (body.data_path_hash, body.login_ref, body.server),
    )
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
# error handling
# --------------------------------------------------------------------------

@app.exception_handler(HTTPException)
async def http_exc_handler(request: Request, exc: HTTPException) -> JSONResponse:  # noqa: D401
    code = {404: "NOT_FOUND", 400: "BAD_REQUEST", 401: "UNAUTHORIZED", 405: "METHOD_NOT_ALLOWED"}.get(
        exc.status_code, "ERROR"
    )
    message = exc.detail if isinstance(exc.detail, str) else str(exc.detail)
    return err(exc.status_code, code, message, request)