"""DB projection handlers for consumed node events (idempotent upserts).

Every handler is idempotent (PRD 4.5): JetStream is at-least-once, so
redeliveries must not duplicate rows or change state.

Identity model (PRD 6.3): events carry terminal data_path_hash + account
login/server; the cloud resolves terminal/account rows by that tuple. A login
different from the terminal's most recently seen account raises a
IDENTITY_MISMATCH system event (doc A.4 code).
"""

from __future__ import annotations

import json
import logging
from datetime import datetime
from decimal import Decimal, InvalidOperation
from typing import Any
from uuid import UUID

from . import db

logger = logging.getLogger("quantpilot.writers")

EVENT_COL = ("registered", "heartbeat", "symbols", "shutdown")


def _dec(value: Any) -> Decimal | None:
    if value in (None, ""):
        return None
    try:
        return Decimal(str(value))
    except InvalidOperation:
        return None


def _int(value: Any) -> int | None:
    if value in (None, ""):
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _ts(value: Any) -> datetime:
    if isinstance(value, datetime):
        return value
    text = str(value)
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    return datetime.fromisoformat(text)


def _uuid(value: Any) -> UUID:
    return UUID(str(value))


# --------------------------------------------------------------------------
# node (trading_nodes)
# --------------------------------------------------------------------------

def upsert_node(device_id: str, hostname: str, os_info: str, agent_version: str) -> dict[str, Any]:
    node = db.query_one(
        """
        INSERT INTO trading_nodes (device_id, hostname, os_info, agent_version,
                                   status, last_seen_at, updated_at)
        VALUES (%s, %s, %s, %s, 'ONLINE', now(), now())
        ON CONFLICT (device_id) DO UPDATE
           SET hostname = EXCLUDED.hostname,
               os_info = EXCLUDED.os_info,
               agent_version = EXCLUDED.agent_version,
               status = 'ONLINE',
               last_seen_at = now(),
               updated_at = now(),
               version = trading_nodes.version + 1
        RETURNING id::text AS node_id, device_id, status
        """,
        (device_id, hostname, os_info, agent_version),
    )
    assert node is not None
    return node


def touch_node(node_id: str) -> None:
    db.execute(
        "UPDATE trading_nodes SET last_seen_at = now(), status = 'ONLINE', updated_at = now() WHERE id = %s",
        (_uuid(node_id),),
    )


# --------------------------------------------------------------------------
# identity resolution
# --------------------------------------------------------------------------

def resolve_terminal(node_id: str, terminal: dict[str, Any]) -> str:
    path_hash = terminal.get("data_path_hash") or ""
    if not path_hash:
        raise ValueError("event terminal missing data_path_hash")
    row = db.query_one(
        "SELECT id::text AS id FROM mt5_terminals WHERE node_id = %s AND terminal_path_hash = %s",
        (_uuid(node_id), path_hash),
    )
    if row is None:
        with db.conn() as connection:
            connection.execute(
                """
                INSERT INTO mt5_terminals (node_id, terminal_path_hash, terminal_build,
                                           terminal_company, status, last_seen_at)
                VALUES (%s, %s, %s, %s, 'ACTIVE', now())
                """,
                (
                    _uuid(node_id),
                    path_hash,
                    _int(terminal.get("build")),
                    terminal.get("company") or None,
                ),
            )
        row = db.query_one(
            "SELECT id::text AS id FROM mt5_terminals WHERE node_id = %s AND terminal_path_hash = %s",
            (_uuid(node_id), path_hash),
        )
    else:
        db.execute(
            """
            UPDATE mt5_terminals
               SET terminal_build = COALESCE(%s, terminal_build),
                   terminal_company = COALESCE(%s, terminal_company),
                   last_seen_at = now(), updated_at = now()
             WHERE id = %s
            """,
            (_int(terminal.get("build")), terminal.get("company") or None, _uuid(row["id"])),
        )
    assert row is not None
    return row["id"]


def resolve_account(terminal_id: str, account: dict[str, Any]) -> str:
    login_ref = str(account.get("login_ref", ""))
    server = str(account.get("server", ""))
    if not login_ref or not server:
        raise ValueError("event account missing login_ref/server")
    row = db.query_one(
        "SELECT id::text AS id FROM trading_accounts WHERE terminal_id = %s AND login_ref = %s AND server = %s",
        (_uuid(terminal_id), login_ref, server),
    )
    if row is None:
        with db.conn() as connection:
            connection.execute(
                """
                INSERT INTO trading_accounts
                       (terminal_id, login_ref, server, company, currency, margin_mode,
                        account_mode, leverage, status, last_seen_at)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, 'OBSERVE_ONLY', now())
                """,
                (
                    _uuid(terminal_id),
                    login_ref,
                    server,
                    account.get("company") or None,
                    account.get("currency") or None,
                    account.get("margin_mode") or None,
                    account.get("account_mode") or None,
                    _int(account.get("leverage")),
                ),
            )
            # keep mt5_terminals.server in sync for convenience
            connection.execute(
                "UPDATE mt5_terminals SET server = %s, updated_at = now() WHERE id = %s",
                (server, _uuid(terminal_id)),
            )
        row = db.query_one(
            "SELECT id::text AS id FROM trading_accounts WHERE terminal_id = %s AND login_ref = %s AND server = %s",
            (_uuid(terminal_id), login_ref, server),
        )
    else:
        db.execute(
            """
            UPDATE trading_accounts
               SET company = COALESCE(%s, company),
                   currency = COALESCE(%s, currency),
                   margin_mode = COALESCE(%s, margin_mode),
                   account_mode = COALESCE(%s, account_mode),
                   leverage = COALESCE(%s, leverage),
                   last_seen_at = now(), updated_at = now()
             WHERE id = %s
            """,
            (
                account.get("company") or None,
                account.get("currency") or None,
                account.get("margin_mode") or None,
                account.get("account_mode") or None,
                _int(account.get("leverage")),
                _uuid(row["id"]),
            ),
        )
    assert row is not None
    return row["id"]


def check_identity_mismatch(terminal_id: str, new_login_ref: str) -> None:
    """PRD 6.3: terminal switching to a different login than the last seen one."""
    row = db.query_one(
        """
        SELECT login_ref FROM trading_accounts
         WHERE terminal_id = %s AND login_ref <> %s
         ORDER BY last_seen_at DESC NULLS LAST
         LIMIT 1
        """,
        (_uuid(terminal_id), new_login_ref),
    )
    if row is None:
        return
    add_system_event(
        source="api-consumer",
        event_type="IDENTITY_MISMATCH",
        severity="WARN",
        payload={
            "terminal_id": terminal_id,
            "previous_login_ref": row["login_ref"],
            "new_login_ref": new_login_ref,
            "action": "account rows are isolated; no risk action taken (OBSERVE_ONLY slice)",
        },
    )


# --------------------------------------------------------------------------
# event handlers
# --------------------------------------------------------------------------

def add_system_event(source: str, event_type: str, severity: str, payload: dict[str, Any]) -> None:
    with db.conn() as connection:
        connection.execute(
            """
            INSERT INTO system_events (source, event_type, severity, payload, occurred_at)
            VALUES (%s, %s, %s, %s, now())
            """,
            (source, event_type, severity, json.dumps(payload, default=str)),
        )


def _resolve(data: dict[str, Any]) -> tuple[str, str]:
    node_id = str(data.get("node_id") or "")
    if not node_id:
        raise ValueError("event data missing node_id")
    terminal = data.get("terminal") or {}
    account = data.get("account") or {}
    terminal_id = resolve_terminal(node_id, terminal)
    account_id = resolve_account(terminal_id, account)
    return terminal_id, account_id


def handle_registered(data: dict[str, Any]) -> None:
    terminal_id, account_id = _resolve(data)
    check_identity_mismatch(terminal_id, str((data.get("account") or {}).get("login_ref", "")))
    add_system_event(
        source="api-consumer",
        event_type="ea.registered",
        severity="INFO",
        payload={
            "terminal_id": terminal_id,
            "account_id": account_id,
            "ea_version": data.get("ea_version"),
            "terminal_build": (data.get("terminal") or {}).get("build"),
        },
    )


def handle_heartbeat(data: dict[str, Any]) -> None:
    terminal_id, account_id = _resolve(data)
    captured_at = _ts(data.get("observed_at"))
    with db.conn() as connection:
        # idempotent on (account_id, captured_at): JetStream redelivery safe
        connection.execute(
            """
            INSERT INTO account_snapshots
                   (account_id, balance, equity, margin, free_margin, currency,
                    open_positions, connected, trade_allowed, expert_allowed,
                    terminal_version, ea_version, captured_at)
            SELECT %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s
             WHERE NOT EXISTS (
                   SELECT 1 FROM account_snapshots
                    WHERE account_id = %s AND captured_at = %s
             )
            """,
            (
                _uuid(account_id),
                _dec(data.get("balance")),
                _dec(data.get("equity")),
                _dec(data.get("margin")),
                _dec(data.get("free_margin")),
                data.get("currency"),
                _int(data.get("open_positions")),
                bool(data.get("connected")),
                bool(data.get("trade_allowed")),
                bool(data.get("expert_allowed")),
                str((data.get("terminal") or {}).get("build") or ""),
                data.get("ea_version"),
                captured_at,
                _uuid(account_id),
                captured_at,
            ),
        )
        connection.execute(
            "UPDATE trading_accounts SET last_seen_at = now(), updated_at = now() WHERE id = %s",
            (_uuid(account_id),),
        )


def handle_symbols(data: dict[str, Any]) -> None:
    terminal_id, account_id = _resolve(data)
    observed_at = _ts(data.get("observed_at"))
    symbols = data.get("symbols") or []
    for sym in symbols:
        name = str(sym.get("broker_symbol", ""))
        if not name:
            continue
        tradable = str(sym.get("trade_mode", "")) in {"FULL", "LONGONLY", "SHORTONLY"}
        with db.conn() as connection:
            connection.execute(
                """
                INSERT INTO symbols (canonical_instrument) VALUES (%s)
                ON CONFLICT (canonical_instrument) DO NOTHING
                """,
                (name,),
            )
            canonical = connection.execute(
                "SELECT id::text AS id FROM symbols WHERE canonical_instrument = %s", (name,)
            ).fetchone()
            assert canonical is not None
            row = connection.execute(
                """
                INSERT INTO broker_symbols
                       (account_id, broker_symbol, canonical_symbol_id, enabled,
                        visible, selected, tradable, updated_at)
                VALUES (%s, %s, %s, true, %s, %s, %s, now())
                ON CONFLICT (account_id, broker_symbol) DO UPDATE
                   SET visible = EXCLUDED.visible,
                       selected = EXCLUDED.selected,
                       tradable = EXCLUDED.tradable,
                       enabled = true,
                       updated_at = now()
                RETURNING id::text AS id
                """,
                (
                    _uuid(account_id),
                    name,
                    _uuid(canonical[0]),
                    bool(sym.get("visible")),
                    bool(sym.get("selected")),
                    tradable,
                ),
            ).fetchone()
            assert row is not None
            # latest spread observation per (account, symbol): upsert only, no
            # quote history (doc 4.4 - the cloud keeps no tick-by-tick data).
            # The observed_at guard keeps out-of-order redeliveries idempotent.
            connection.execute(
                """
                INSERT INTO symbol_quotes
                       (account_id, broker_symbol, spread_points, bid, ask, observed_at)
                VALUES (%s, %s, %s, %s, %s, %s)
                ON CONFLICT (account_id, broker_symbol) DO UPDATE
                   SET spread_points = EXCLUDED.spread_points,
                       bid            = EXCLUDED.bid,
                       ask            = EXCLUDED.ask,
                       observed_at    = EXCLUDED.observed_at,
                       updated_at     = now()
                 WHERE symbol_quotes.observed_at IS NULL
                    OR symbol_quotes.observed_at <= EXCLUDED.observed_at
                """,
                (
                    _uuid(account_id),
                    name,
                    _int(sym.get("spread")) or 0,
                    _dec(sym.get("bid")),
                    _dec(sym.get("ask")),
                    observed_at,
                ),
            )
            spec_hash = sym.get("spec_hash")
            if spec_hash:
                spec_json = {
                    k: v for k, v in sym.items()
                    if k not in {"spec_hash", "bid", "ask", "spread"}
                }
                connection.execute(
                    """
                    INSERT INTO symbol_specs (broker_symbol_id, spec_json, spec_hash, observed_at)
                    VALUES (%s, %s, %s, %s)
                    ON CONFLICT (broker_symbol_id, spec_hash) DO NOTHING
                    """,
                    (_uuid(row[0]), json.dumps(spec_json, default=str), spec_hash, observed_at),
                )


def handle_positions(data: dict[str, Any]) -> None:
    """State-based open-position sync (doc 4.4 projection, 5.2 reconcile).

    The event carries the COMPLETE set of open positions at observed_at:
      - reported tickets are upserted as OPEN
      - previously OPEN tickets missing from the snapshot are marked CLOSED
    JetStream redelivery / out-of-order delivery is guarded by
    last_reported_at: a snapshot older than the last one applied is a no-op.
    """
    terminal_id, account_id = _resolve(data)
    observed_at = _ts(data.get("observed_at"))
    tickets: list[str] = []
    with db.conn() as connection:
        for pos in data.get("positions") or []:
            ticket = str(pos.get("broker_position_ticket", ""))
            if not ticket:
                continue
            tickets.append(ticket)
            connection.execute(
                """
                INSERT INTO positions
                       (account_id, broker_position_ticket, broker_symbol, direction,
                        volume, price_open, current_price, sl, tp, profit, swap,
                        open_time, status, last_reported_at)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, 'OPEN', %s)
                ON CONFLICT (account_id, broker_position_ticket) DO UPDATE
                   SET broker_symbol  = EXCLUDED.broker_symbol,
                       direction      = EXCLUDED.direction,
                       volume          = EXCLUDED.volume,
                       price_open      = EXCLUDED.price_open,
                       current_price   = EXCLUDED.current_price,
                       sl              = EXCLUDED.sl,
                       tp              = EXCLUDED.tp,
                       profit          = EXCLUDED.profit,
                       swap            = EXCLUDED.swap,
                       open_time       = EXCLUDED.open_time,
                       status          = 'OPEN',
                       last_reported_at = EXCLUDED.last_reported_at,
                       closed_at       = NULL,
                       updated_at      = now(),
                       version         = positions.version + 1
                 WHERE positions.last_reported_at IS NULL
                    OR positions.last_reported_at <= EXCLUDED.last_reported_at
                """,
                (
                    _uuid(account_id),
                    ticket,
                    str(pos.get("broker_symbol", "")),
                    str(pos.get("direction", "")),
                    _dec(pos.get("volume")),
                    _dec(pos.get("price_open")),
                    _dec(pos.get("current_price")),
                    _dec(pos.get("sl")),
                    _dec(pos.get("tp")),
                    _dec(pos.get("profit")),
                    _dec(pos.get("swap")),
                    _ts(pos.get("open_time")) if pos.get("open_time") else None,
                    observed_at,
                ),
            )
        # tickets missing from this snapshot -> CLOSED (only if not newer-stamped)
        if tickets:
            connection.execute(
                """
                UPDATE positions
                   SET status = 'CLOSED', closed_at = now(), updated_at = now(),
                       version = version + 1
                 WHERE account_id = %s AND status = 'OPEN'
                   AND broker_position_ticket <> ALL(%s)
                   AND (last_reported_at IS NULL OR last_reported_at <= %s)
                """,
                (_uuid(account_id), tickets, observed_at),
            )
        else:
            connection.execute(
                """
                UPDATE positions
                   SET status = 'CLOSED', closed_at = now(), updated_at = now(),
                       version = version + 1
                 WHERE account_id = %s AND status = 'OPEN'
                   AND (last_reported_at IS NULL OR last_reported_at <= %s)
                """,
                (_uuid(account_id), observed_at),
            )


def handle_deals(data: dict[str, Any]) -> None:
    """History-deal projection (doc 12.2, incremental from the EA).

    Deals are immutable broker facts: insert-only with ON CONFLICT DO NOTHING,
    so overlapping EA windows and JetStream redelivery are naturally
    idempotent (PRD 4.5).
    """
    terminal_id, account_id = _resolve(data)
    with db.conn() as connection:
        for deal in data.get("deals") or []:
            ticket = str(deal.get("broker_deal_ticket", ""))
            if not ticket:
                continue
            connection.execute(
                """
                INSERT INTO deals
                       (account_id, broker_deal_ticket, broker_order_ticket,
                        broker_position_ticket, broker_symbol, direction, entry,
                        volume, price, commission, swap, profit, deal_time,
                        magic, comment)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                ON CONFLICT (account_id, broker_deal_ticket) DO NOTHING
                """,
                (
                    _uuid(account_id),
                    ticket,
                    str(deal.get("broker_order_ticket", "")),
                    str(deal.get("broker_position_ticket", "")),
                    str(deal.get("broker_symbol", "")),
                    str(deal.get("direction", "")),
                    str(deal.get("entry", "")),
                    _dec(deal.get("volume")),
                    _dec(deal.get("price")),
                    _dec(deal.get("commission")),
                    _dec(deal.get("swap")),
                    _dec(deal.get("profit")),
                    _ts(deal.get("deal_time")) if deal.get("deal_time") else None,
                    _int(deal.get("magic")),
                    deal.get("comment") or None,
                ),
            )


def handle_orders(data: dict[str, Any]) -> None:
    """Pending-order projection, state-synced like handle_positions.

    Reported tickets are upserted ACTIVE; previously ACTIVE tickets missing
    from the snapshot are marked REMOVED (cancelled/expired/filled - the
    matching deal, if any, arrives via the deals channel).
    """
    terminal_id, account_id = _resolve(data)
    observed_at = _ts(data.get("observed_at"))
    tickets: list[str] = []
    with db.conn() as connection:
        for order in data.get("orders") or []:
            ticket = str(order.get("broker_ticket", ""))
            if not ticket:
                continue
            tickets.append(ticket)
            connection.execute(
                """
                INSERT INTO orders
                       (account_id, broker_ticket, broker_symbol, direction,
                        order_kind, volume, price, sl, tp, time_setup,
                        time_expiration, status, last_reported_at)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, 'ACTIVE', %s)
                ON CONFLICT (account_id, broker_ticket) DO UPDATE
                   SET broker_symbol    = EXCLUDED.broker_symbol,
                       direction        = EXCLUDED.direction,
                       order_kind       = EXCLUDED.order_kind,
                       volume            = EXCLUDED.volume,
                       price             = EXCLUDED.price,
                       sl                = EXCLUDED.sl,
                       tp                = EXCLUDED.tp,
                       time_setup        = EXCLUDED.time_setup,
                       time_expiration   = EXCLUDED.time_expiration,
                       status            = 'ACTIVE',
                       last_reported_at = EXCLUDED.last_reported_at,
                       removed_at        = NULL,
                       updated_at        = now(),
                       version           = orders.version + 1
                 WHERE orders.last_reported_at IS NULL
                    OR orders.last_reported_at <= EXCLUDED.last_reported_at
                """,
                (
                    _uuid(account_id),
                    ticket,
                    str(order.get("broker_symbol", "")),
                    str(order.get("direction", "")),
                    str(order.get("order_kind", "")),
                    _dec(order.get("volume")),
                    _dec(order.get("price")),
                    _dec(order.get("sl")),
                    _dec(order.get("tp")),
                    _ts(order.get("time_setup")) if order.get("time_setup") else None,
                    _ts(order.get("time_expiration")) if order.get("time_expiration") else None,
                    observed_at,
                ),
            )
        if tickets:
            connection.execute(
                """
                UPDATE orders
                   SET status = 'REMOVED', removed_at = now(), updated_at = now(),
                       version = version + 1
                 WHERE account_id = %s AND status = 'ACTIVE'
                   AND broker_ticket <> ALL(%s)
                   AND (last_reported_at IS NULL OR last_reported_at <= %s)
                """,
                (_uuid(account_id), tickets, observed_at),
            )
        else:
            connection.execute(
                """
                UPDATE orders
                   SET status = 'REMOVED', removed_at = now(), updated_at = now(),
                       version = version + 1
                 WHERE account_id = %s AND status = 'ACTIVE'
                   AND (last_reported_at IS NULL OR last_reported_at <= %s)
                """,
                (_uuid(account_id), observed_at),
            )


def handle_calendar(data: dict[str, Any]) -> None:
    """Economic-calendar projection from the MT5 built-in calendar (doc 12.2).

    Upsert by (account_id, event_uid): re-reports of the same window update
    actual/forecast/previous once values are released. The IS DISTINCT FROM
    guard keeps JetStream redeliveries of identical payloads no-ops
    (version stays put, PRD 4.5 idempotency).
    """
    terminal_id, account_id = _resolve(data)
    observed_at = _ts(data.get("observed_at"))
    with db.conn() as connection:
        for ev in data.get("events") or []:
            uid = str(ev.get("event_uid", ""))
            name = str(ev.get("name", ""))
            if not uid or not name:
                continue
            connection.execute(
                """
                INSERT INTO calendar_events
                       (account_id, event_uid, country_code, currency, name,
                        importance, multiplier, digits, event_time,
                        actual, forecast, previous, observed_at)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                ON CONFLICT (account_id, event_uid) DO UPDATE
                   SET country_code = EXCLUDED.country_code,
                       currency     = EXCLUDED.currency,
                       name         = EXCLUDED.name,
                       importance   = EXCLUDED.importance,
                       multiplier   = EXCLUDED.multiplier,
                       digits       = EXCLUDED.digits,
                       event_time   = EXCLUDED.event_time,
                       actual       = EXCLUDED.actual,
                       forecast     = EXCLUDED.forecast,
                       previous     = EXCLUDED.previous,
                       observed_at  = EXCLUDED.observed_at,
                       updated_at   = now(),
                       version      = calendar_events.version + 1
                 WHERE (calendar_events.actual, calendar_events.forecast,
                        calendar_events.previous, calendar_events.importance,
                        calendar_events.event_time)
                       IS DISTINCT FROM
                       (EXCLUDED.actual, EXCLUDED.forecast,
                        EXCLUDED.previous, EXCLUDED.importance,
                        EXCLUDED.event_time)
                """,
                (
                    _uuid(account_id),
                    uid,
                    str(ev.get("country_code", "")),
                    str(ev.get("currency", "")),
                    name,
                    str(ev.get("importance", "NONE")),
                    str(ev.get("multiplier", "NONE")),
                    _int(ev.get("digits")) or 0,
                    _ts(ev.get("event_time")),
                    _dec(ev.get("actual")),
                    _dec(ev.get("forecast")),
                    _dec(ev.get("previous")),
                    observed_at,
                ),
            )


def handle_shutdown(data: dict[str, Any]) -> None:
    terminal_id, account_id = _resolve(data)
    add_system_event(
        source="api-consumer",
        event_type="ea.shutdown",
        severity="INFO",
        payload={
            "terminal_id": terminal_id,
            "account_id": account_id,
            "reason": data.get("reason"),
            "ea_version": data.get("ea_version"),
        },
    )


HANDLERS = {
    "ea.registered": handle_registered,
    "ea.heartbeat": handle_heartbeat,
    "ea.symbols": handle_symbols,
    "ea.positions": handle_positions,
    "ea.deals": handle_deals,
    "ea.orders": handle_orders,
    "ea.calendar": handle_calendar,
    "ea.shutdown": handle_shutdown,
}
