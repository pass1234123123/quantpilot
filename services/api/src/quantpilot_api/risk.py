"""Cloud-side risk engine: the authoritative gate for new risk (PRD 9).

The gate runs once per risk-increasing command at creation time, BEFORE the
command is persisted (PRD 3.2: Signal Aggregator -> Risk Engine -> Command
Builder -> Durable Command Store). REJECT means the command is stored in the
terminal REJECTED state and is never delivered to the EA; ALLOW means it
enters the normal QUEUED lifecycle. Every evaluation leaves a risk_decisions
audit row carrying the numeric snapshot the decision was computed from
(doc 12.2, PRD 1.5 auditability).

Layer split (PRD 9.1: cloud policy review + EA-local execution protection):
this module implements only the checks the cloud projections can compute -
system level (stop states), account level (snapshot freshness, daily loss,
drawdown, margin level, consecutive losses), portfolio level (position count,
daily order count, weekend block) and order level (volume vs symbol specs,
stop-loss presence/side, estimated per-trade loss). Symbol-level checks that
need live market state (current spread, tick freshness, session windows) and
execution-level hard limits stay EA-local; the cloud holds no tick data by
design (doc 4.4).

Approximations, documented (broker server clock has no timezone API in
MQL5, doc 13.2 /deals note):
- "today" boundaries use UTC midnight;
- deals.deal_time is broker server time, compared against the UTC day cutoff;
- daily loss = realized P/L of today's OUT/OUT_BY deals + floating profit of
  OPEN positions, against a limit of fraction x CURRENT basis (not the
  day-start basis; the slice keeps one basis source, PRD 9.4 allows the
  day-cut policy to be configured later).

All money math is Decimal (PRD 21.3). Thresholds default OFF (PRD 9.6): the
gate enforces only what the Owner explicitly configured.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any
from uuid import UUID

from . import db
from .account_config import AccountConfig

logger = logging.getLogger("quantpilot.risk")

# command types that ADD risk; everything else (close/cancel/modify/stop/
# reconcile) passes the gate untouched - PRD 2.5 forbids only NEW risk
RISK_INCREASING_TYPES = frozenset({"OPEN_MARKET", "PLACE_PENDING"})

# reason codes (single source of truth; surfaced in risk_decisions and events)
R_GLOBAL_STOP = "GLOBAL_STOP_NEW_RISK"
R_ACCOUNT_STOP = "ACCOUNT_STOP_NEW_RISK"
R_SNAPSHOT_STALE = "SNAPSHOT_STALE"
R_NOT_CONNECTED = "TERMINAL_NOT_CONNECTED"
R_TRADE_NOT_ALLOWED = "TRADE_NOT_ALLOWED"
R_EXPERT_NOT_ALLOWED = "EXPERT_NOT_ALLOWED"
R_DAILY_LOSS = "DAILY_LOSS_EXCEEDED"
R_DRAWDOWN = "DRAWDOWN_EXCEEDED"
R_MARGIN_LEVEL = "MARGIN_LEVEL_BELOW_MIN"
R_CONSEC_LOSSES = "CONSECUTIVE_LOSSES"
R_MAX_POSITIONS = "MAX_POSITIONS_REACHED"
R_MAX_DAILY_ORDERS = "MAX_DAILY_ORDERS_REACHED"
R_WEEKEND = "WEEKEND_BLOCKED"
R_SPEC_UNKNOWN = "SYMBOL_SPEC_UNKNOWN"
R_NOT_TRADABLE = "SYMBOL_NOT_TRADABLE"
R_VOL_BELOW_MIN = "VOLUME_BELOW_MIN"
R_VOL_ABOVE_MAX = "VOLUME_ABOVE_MAX"
R_VOL_NOT_ON_STEP = "VOLUME_NOT_ON_STEP"
R_SL_REQUIRED = "STOP_LOSS_REQUIRED"
R_SL_INVALID = "INVALID_STOP_LOSS"
R_PER_TRADE = "PER_TRADE_RISK_EXCEEDED"
R_ESTIMATE_UNAVAILABLE = "RISK_ESTIMATE_UNAVAILABLE"


@dataclass
class RiskDecision:
    decision: str  # ALLOW | REJECT (REDUCE/REVIEW reserved, PRD 9.1)
    reason_codes: list[str] = field(default_factory=list)
    snapshot: dict[str, Any] = field(default_factory=dict)


def _dec(value: Any) -> Decimal:
    return Decimal(str(value))


def _jsonable(value: Any) -> Any:
    """Decimal -> str, datetime -> iso, recursively (PRD 21.3: never float)."""
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, dict):
        return {k: _jsonable(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_jsonable(v) for v in value]
    return value


def _stop_states(account_id: UUID) -> dict[str, str | None]:
    rows = db.query(
        "SELECT scope, status, reason FROM risk_states WHERE account_id = %s OR account_id IS NULL",
        (account_id,),
    )
    out = {"GLOBAL": "NORMAL", "ACCOUNT": "NORMAL", "global_reason": None, "account_reason": None}
    for row in rows:
        if row["scope"] == "GLOBAL":
            out["GLOBAL"] = row["status"]
            out["global_reason"] = row["reason"]
        else:
            out["ACCOUNT"] = row["status"]
            out["account_reason"] = row["reason"]
    return out


def evaluate_command(
    account_id: UUID,
    command_type: str,
    payload: dict[str, Any],
    config: AccountConfig,
    config_version: int,
) -> RiskDecision:
    """Run the cloud gate for one risk-increasing command (PRD 9.2 order).

    Only call for command_type in RISK_INCREASING_TYPES; other types never
    add risk and bypass the gate entirely (PRD 2.5).
    """
    risk = config.risk
    filters = config.filters
    now = datetime.now(timezone.utc)
    snap: dict[str, Any] = {
        "command_type": command_type,
        "config_version": config_version,
        "symbol": payload.get("symbol"),
        "side": payload.get("side"),
        "volume": payload.get("volume"),
        "thresholds": {
            "per_trade_risk_fraction": risk.per_trade_risk_fraction,
            "risk_basis": risk.risk_basis,
            "max_daily_loss_fraction": risk.max_daily_loss_fraction,
            "max_drawdown_fraction": risk.max_drawdown_fraction,
            "min_margin_level": risk.min_margin_level,
            "max_consecutive_losses": risk.max_consecutive_losses,
            "require_stop_loss": risk.require_stop_loss,
            "snapshot_max_age_seconds": risk.snapshot_max_age_seconds,
            "quote_max_age_seconds": risk.quote_max_age_seconds,
            "max_positions": filters.max_positions,
            "max_daily_orders": filters.max_daily_orders,
            "weekend_block": filters.weekend_block,
        },
    }

    def reject(reason: str) -> RiskDecision:
        snap_json = _jsonable(snap)
        logger.info("risk gate REJECT %s command_type=%s account=%s",
                    reason, command_type, account_id)
        return RiskDecision("REJECT", [reason], snap_json)

    # ---- 1. system level: durable stop states (PRD 3.3, 9.5) --------------
    stops = _stop_states(account_id)
    snap["stop_states"] = {"global": stops["GLOBAL"], "account": stops["ACCOUNT"]}
    if stops["GLOBAL"] != "NORMAL":
        snap["stop_reason"] = stops["global_reason"]
        return reject(R_GLOBAL_STOP)
    if stops["ACCOUNT"] != "NORMAL":
        snap["stop_reason"] = stops["account_reason"]
        return reject(R_ACCOUNT_STOP)

    # ---- 2. account level: snapshot freshness + permissions (PRD 2.5) -----
    shot = db.query_one(
        """
        SELECT balance, equity, margin, free_margin, connected, trade_allowed,
               expert_allowed, captured_at
          FROM account_snapshots
         WHERE account_id = %s
         ORDER BY captured_at DESC
         LIMIT 1
        """,
        (account_id,),
    )
    age = None
    if shot is not None and shot["captured_at"] is not None:
        age = (now - shot["captured_at"]).total_seconds()
    snap["snapshot_age_seconds"] = age
    if shot is None or age is None or age > risk.snapshot_max_age_seconds:
        return reject(R_SNAPSHOT_STALE)
    if shot["connected"] is False:
        return reject(R_NOT_CONNECTED)
    if shot["trade_allowed"] is False:
        return reject(R_TRADE_NOT_ALLOWED)
    if shot["expert_allowed"] is False:
        return reject(R_EXPERT_NOT_ALLOWED)

    basis = _dec(shot["equity"] if risk.risk_basis == "EQUITY" else shot["balance"])
    snap["basis_value"] = basis

    # ---- 3. account level: loss limits (PRD 9.4) --------------------------
    day_start = now.replace(hour=0, minute=0, second=0, microsecond=0)
    realized_today = db.query_one(
        """
        SELECT COALESCE(SUM(COALESCE(profit, 0) + COALESCE(swap, 0)
                             + COALESCE(commission, 0)), 0) AS net
          FROM deals
         WHERE account_id = %s AND entry IN ('OUT', 'OUT_BY') AND deal_time >= %s
        """,
        (account_id, day_start),
    )["net"]
    positions_row = db.query_one(
        """
        SELECT count(*) AS open_positions,
               COALESCE(SUM(COALESCE(profit, 0)), 0) AS floating
          FROM positions
         WHERE account_id = %s AND status = 'OPEN'
        """,
        (account_id,),
    )
    open_positions = positions_row["open_positions"]
    floating = positions_row["floating"]
    daily_loss = max(Decimal(0), -(realized_today + floating))
    snap["open_positions"] = open_positions
    snap["floating_pl"] = floating
    snap["realized_today"] = realized_today
    snap["daily_loss"] = daily_loss

    daily_limit_fraction = _dec(risk.max_daily_loss_fraction)
    if daily_limit_fraction > 0:
        daily_limit = daily_limit_fraction * basis
        snap["daily_loss_limit"] = daily_limit
        if daily_loss >= daily_limit:
            return reject(R_DAILY_LOSS)

    drawdown_fraction = _dec(risk.max_drawdown_fraction)
    if drawdown_fraction > 0:
        peak_row = db.query_one(
            """
            SELECT MAX(equity) AS peak
              FROM account_snapshots
             WHERE account_id = %s AND equity IS NOT NULL
            """,
            (account_id,),
        )
        peak = peak_row["peak"]
        snap["peak_equity"] = peak
        if peak is not None and peak > 0:
            equity = _dec(shot["equity"])
            drawdown = (peak - equity) / peak
            snap["drawdown"] = drawdown
            if drawdown >= drawdown_fraction:
                return reject(R_DRAWDOWN)

    min_margin_level = _dec(risk.min_margin_level)
    margin = shot["margin"]
    snap["margin"] = margin
    if min_margin_level > 0 and margin is not None and margin > 0:
        level = _dec(shot["equity"]) / _dec(margin) * Decimal(100)
        snap["margin_level"] = level
        if level <= min_margin_level:
            return reject(R_MARGIN_LEVEL)

    max_consec = risk.max_consecutive_losses
    if max_consec > 0:
        rows = db.query(
            """
            SELECT COALESCE(profit, 0) + COALESCE(swap, 0) + COALESCE(commission, 0) AS net
              FROM deals
             WHERE account_id = %s AND entry IN ('OUT', 'OUT_BY')
             ORDER BY deal_time DESC, broker_deal_ticket DESC
             LIMIT 50
            """,
            (account_id,),
        )
        streak = 0
        for row in rows:
            if row["net"] < 0:
                streak += 1
            else:
                break
        snap["consecutive_losses"] = streak
        if streak >= max_consec:
            return reject(R_CONSEC_LOSSES)

    # ---- 4. portfolio level (PRD 9.4) --------------------------------------
    if open_positions >= filters.max_positions:
        return reject(R_MAX_POSITIONS)
    if filters.weekend_block and now.weekday() >= 5:  # Sat/Sun UTC
        return reject(R_WEEKEND)
    orders_today = db.query_one(
        """
        SELECT count(*) AS n
          FROM commands
         WHERE account_id = %s
           AND command_type IN ('OPEN_MARKET', 'PLACE_PENDING')
           AND created_at >= %s
           AND status <> 'REJECTED'
        """,
        (account_id, day_start),
    )["n"]
    snap["risk_commands_today"] = orders_today
    if orders_today >= filters.max_daily_orders:
        return reject(R_MAX_DAILY_ORDERS)

    # ---- 5. order level: symbol spec + volume (PRD 13.4) -------------------
    symbol_name = str(payload.get("symbol") or "")
    spec_row = db.query_one(
        """
        SELECT bs.tradable AS tradable, ss.spec_json AS spec_json
          FROM broker_symbols bs
          JOIN symbol_specs ss ON ss.broker_symbol_id = bs.id
         WHERE bs.account_id = %s AND bs.broker_symbol = %s
         ORDER BY ss.observed_at DESC
         LIMIT 1
        """,
        (account_id, symbol_name),
    )
    if spec_row is None:
        return reject(R_SPEC_UNKNOWN)
    spec = spec_row["spec_json"] or {}
    if spec_row["tradable"] is not True or spec.get("trade_mode") != "FULL":
        # one-sided modes would need side-aware logic; treat as not tradable
        snap["trade_mode"] = spec.get("trade_mode")
        return reject(R_NOT_TRADABLE)

    volume = _dec(payload.get("volume"))
    vol_min = _dec(spec["volume_min"])
    vol_max = _dec(spec["volume_max"])
    vol_step = _dec(spec["volume_step"])
    snap["volume_limits"] = {"min": vol_min, "max": vol_max, "step": vol_step}
    if volume < vol_min:
        return reject(R_VOL_BELOW_MIN)
    if volume > vol_max:
        return reject(R_VOL_ABOVE_MAX)
    if vol_step > 0 and (volume - vol_min) % vol_step != 0:
        return reject(R_VOL_NOT_ON_STEP)

    # ---- 6. order level: stop loss + per-trade risk (PRD 9.3) ------------
    side = str(payload.get("side") or "")
    sl_raw = payload.get("stop_loss")
    if risk.require_stop_loss and (sl_raw is None or str(sl_raw) == ""):
        return reject(R_SL_REQUIRED)

    quote = db.query_one(
        """
        SELECT spread_points, bid, ask, observed_at
          FROM symbol_quotes
         WHERE account_id = %s AND broker_symbol = %s
        """,
        (account_id, symbol_name),
    )
    quote_age = None
    if quote is not None and quote["observed_at"] is not None:
        quote_age = (now - quote["observed_at"]).total_seconds()
    snap["quote"] = {
        "bid": quote["bid"] if quote else None,
        "ask": quote["ask"] if quote else None,
        "spread_points": quote["spread_points"] if quote else None,
        "age_seconds": quote_age,
    }

    is_pending = command_type == "PLACE_PENDING"
    if is_pending:
        entry = _dec(payload.get("price"))
    else:
        # market entry approximation: BUY fills near ask, SELL near bid
        if quote is None or quote["bid"] is None or quote["ask"] is None:
            entry = None
        else:
            entry = _dec(quote["ask"] if side == "BUY" else quote["bid"])
    snap["entry_reference"] = entry

    if sl_raw not in (None, ""):
        sl = _dec(sl_raw)
        snap["stop_loss"] = sl
        if entry is not None:
            if (side == "BUY" and sl >= entry) or (side == "SELL" and sl <= entry):
                return reject(R_SL_INVALID)
    else:
        sl = None

    per_trade_fraction = _dec(risk.per_trade_risk_fraction)
    if per_trade_fraction > 0:
        budget = per_trade_fraction * basis
        snap["per_trade_budget"] = budget
        tick_size = _dec(spec["tick_size"])
        tick_value_loss = _dec(spec["tick_value_loss"])
        needed = (entry, sl, tick_size, tick_value_loss, volume)
        if quote is None or quote_age is None or quote_age > risk.quote_max_age_seconds or any(
            v is None or v <= 0 for v in (tick_size, tick_value_loss)
        ) or any(v is None for v in needed):
            # PRD 9.3: if the estimated loss cannot be computed reliably,
            # refuse the new trade
            return reject(R_ESTIMATE_UNAVAILABLE)
        est_loss = abs(entry - sl) / tick_size * tick_value_loss * volume
        snap["estimated_loss"] = est_loss
        if est_loss > budget:
            return reject(R_PER_TRADE)

    snap_json = _jsonable(snap)
    logger.info("risk gate ALLOW command_type=%s account=%s symbol=%s",
                command_type, account_id, symbol_name)
    return RiskDecision("ALLOW", [], snap_json)


def decision_for_command(command_id: UUID) -> dict[str, Any] | None:
    """Latest gate decision of a command (for GET endpoints / replays)."""
    row = db.query_one(
        """
        SELECT decision, reason_codes, decided_at, config_version
          FROM risk_decisions
         WHERE command_id = %s
        """,
        (command_id,),
    )
    if row is None:
        return None
    return db.jsonable({
        "decision": row["decision"],
        "reason_codes": row["reason_codes"],
        "decided_at": row["decided_at"],
        "config_version": row["config_version"],
    })


def record_decision(
    account_id: UUID,
    command_id: UUID,
    signal_id: str | None,
    decision: RiskDecision,
) -> None:
    with db.conn() as connection:
        connection.execute(
            """
            INSERT INTO risk_decisions (account_id, command_id, signal_id,
                                         decision, reason_codes, risk_snapshot,
                                         config_version)
            VALUES (%s, %s, %s, %s, CAST(%s AS jsonb), CAST(%s AS jsonb), %s)
            """,
            (
                account_id,
                command_id,
                signal_id,
                decision.decision,
                json.dumps(decision.reason_codes),
                json.dumps(_jsonable(decision.snapshot)),
                decision.snapshot.get("config_version", 0),
            ),
        )
