"""FastAPI application: node/terminal/account routes + health (PRD 13.2).

All responses use the unified envelope {data, error, request_id, timestamp}.
This slice has NO auth/RBAC yet - routes are read-only or idempotent upserts;
command routes (POST /api/v1/commands etc.) are intentionally absent.
"""

from __future__ import annotations

import logging
from contextlib import asynccontextmanager
from uuid import UUID

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse

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


@app.get("/api/v1/accounts/{account_id}/symbols")
def account_symbols(account_id: str, request: Request) -> JSONResponse:
    rows = db.query(
        """
        SELECT bs.id::text AS broker_symbol_id, bs.broker_symbol,
               bs.enabled, bs.visible, bs.selected, bs.tradable,
               cs.canonical_instrument,
               sp.spec_hash, sp.observed_at
          FROM broker_symbols bs
          JOIN symbols cs ON cs.id = bs.canonical_symbol_id
     LEFT JOIN LATERAL (
                SELECT spec_hash, observed_at FROM symbol_specs
                 WHERE broker_symbol_id = bs.id
                 ORDER BY observed_at DESC LIMIT 1
              ) sp ON true
         WHERE bs.account_id = %s
         ORDER BY bs.broker_symbol
        """,
        (account_id,),
    )
    return ok(db.jsonable(rows), request)


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