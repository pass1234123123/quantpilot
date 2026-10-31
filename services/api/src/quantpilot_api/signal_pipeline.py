"""Signal aggregation and deterministic command identity (PRD 3.2/7.5)."""

from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from typing import Any

from quantpilot_contracts.models import TradeCommandCreate

from . import command_service, db, writers
from .account_config import AccountConfig
from .risk import SizingError, calculate_volume


def aggregate_signals(signals: list[dict[str, Any]]) -> dict[str, Any]:
    """Group one bar's signals by direction; opposite directions conflict.

    Same-direction signals produce one group. The strongest signal supplies
    the executable levels, with signal_id as a deterministic tie-breaker; all
    contributor IDs remain available for the audit snapshot.
    """
    by_direction: dict[str, list[dict[str, Any]]] = {}
    for row in signals:
        direction = str(row["signal_json"]["direction"])
        by_direction.setdefault(direction, []).append(row)
    if len(by_direction) > 1:
        return {
            "groups": [],
            "conflict_signal_ids": sorted(str(row["signal_id"]) for row in signals),
        }

    groups = []
    for direction, rows in sorted(by_direction.items()):
        ordered = sorted(
            rows,
            key=lambda row: (
                -Decimal(str(row["signal_json"]["strength"])),
                str(row["signal_id"]),
            ),
        )
        groups.append(
            {
                "direction": direction,
                "primary": ordered[0],
                "signal_ids": sorted(str(row["signal_id"]) for row in rows),
            }
        )
    return {"groups": groups, "conflict_signal_ids": []}


def group_command_id(
    account_id: str,
    broker_symbol: str,
    timeframe: str,
    source_bar_time: datetime,
    direction: str,
) -> uuid.UUID:
    """One stable command ID per bar/direction across event redeliveries."""
    key = ":".join(
        (
            "quantpilot-signal-group-v1",
            account_id,
            broker_symbol,
            timeframe,
            source_bar_time.astimezone(timezone.utc).isoformat(),
            direction,
        )
    )
    return uuid.uuid5(uuid.NAMESPACE_URL, key)


def process_signals(
    signals: list[dict[str, Any]],
    *,
    account_id: str,
    broker_symbol: str,
    timeframe: str,
    source_bar_time: datetime,
    config: AccountConfig,
    config_version: int,
    snapshot: dict[str, Decimal] | None = None,
    quote: dict[str, Decimal] | None = None,
    spec: dict[str, str] | None = None,
    ai_analysis: dict[str, Any] | None = None,
    now: datetime | None = None,
    command_creator: Any = None,
) -> dict[str, Any]:
    """Aggregate one bar, size each safe group, and persist through the gate."""
    if not config.automation.command_generation:
        return {"command_ids": [], "conflict_signal_ids": [], "failures": []}
    aggregated = aggregate_signals(signals)
    result: dict[str, Any] = {
        "command_ids": [],
        "conflict_signal_ids": aggregated["conflict_signal_ids"],
        "failures": [],
    }
    now = now or datetime.now(timezone.utc)
    for group in aggregated["groups"]:
        try:
            command = build_trade_command(
                group,
                account_id=account_id,
                broker_symbol=broker_symbol,
                timeframe=timeframe,
                source_bar_time=source_bar_time,
                config=config,
                snapshot=snapshot,
                quote=quote,
                spec=spec,
                now=now,
            )
        except SizingError as exc:
            result["failures"].append(
                {"signal_ids": group["signal_ids"], "reason": exc.reason}
            )
            continue
        created = command_creator(
            command,
            config=config,
            config_version=config_version,
            context=risk_context(group, ai_analysis),
            now=now,
        )
        result["command_ids"].append(str(created["command_id"]))
    return result


def process_newest_bar(
    account_id: str,
    broker_symbol: str,
    timeframe: str,
    source_bar_time: datetime,
    *,
    ai_analysis: dict[str, Any] | None = None,
    now: datetime | None = None,
    command_creator: Any = None,
) -> dict[str, Any]:
    """Load one closed bar's signals and market context, then cross the gate."""
    account_uuid = uuid.UUID(account_id)
    config, config_version = command_service.effective_config(account_uuid)
    if not config.automation.command_generation:
        return {"command_ids": [], "conflict_signal_ids": [], "failures": []}

    signals = db.query(
        """
        SELECT ss.signal_id::text AS signal_id,
               ss.strategy_instance_id::text AS strategy_instance_id,
               ss.signal_json, ss.source_bar_time
          FROM strategy_signals ss
          JOIN strategy_instances si ON si.id = ss.strategy_instance_id
         WHERE ss.account_id = %s AND si.broker_symbol = %s
           AND si.timeframe = %s AND ss.source_bar_time = %s
         ORDER BY ss.generated_at, ss.signal_id
        """,
        (account_uuid, broker_symbol, timeframe, source_bar_time),
    )
    if not signals:
        return {"command_ids": [], "conflict_signal_ids": [], "failures": []}

    snapshot = db.query_one(
        """
        SELECT balance, equity
          FROM account_snapshots
         WHERE account_id = %s
         ORDER BY captured_at DESC
         LIMIT 1
        """,
        (account_uuid,),
    )
    quote = db.query_one(
        """
        SELECT bid, ask
          FROM symbol_quotes
         WHERE account_id = %s AND broker_symbol = %s
        """,
        (account_uuid, broker_symbol),
    )
    spec_row = db.query_one(
        """
        SELECT ss.spec_json
          FROM broker_symbols bs
          JOIN symbol_specs ss ON ss.broker_symbol_id = bs.id
         WHERE bs.account_id = %s AND bs.broker_symbol = %s
         ORDER BY ss.observed_at DESC
         LIMIT 1
        """,
        (account_uuid, broker_symbol),
    )
    now = now or datetime.now(timezone.utc)
    result = process_signals(
        signals,
        account_id=account_id,
        broker_symbol=broker_symbol,
        timeframe=timeframe,
        source_bar_time=source_bar_time,
        config=config,
        config_version=config_version,
        snapshot=snapshot,
        quote=quote,
        spec=spec_row["spec_json"] if spec_row is not None else None,
        ai_analysis=ai_analysis,
        now=now,
        command_creator=command_creator or command_service.create_command,
    )
    audit = {
        "account_id": account_id,
        "broker_symbol": broker_symbol,
        "timeframe": timeframe,
        "source_bar_time": source_bar_time.isoformat(),
    }
    if result["conflict_signal_ids"]:
        writers.add_system_event(
            source="signal-aggregator",
            event_type="signal.conflict",
            severity="WARN",
            payload={**audit, "signal_ids": result["conflict_signal_ids"]},
        )
    for failure in result["failures"]:
        writers.add_system_event(
            source="signal-aggregator",
            event_type="signal.command_failed",
            severity="WARN",
            payload={**audit, **failure},
        )
    return result


def build_trade_command(
    group: dict[str, Any],
    *,
    account_id: str,
    broker_symbol: str,
    timeframe: str,
    source_bar_time: datetime,
    config: AccountConfig,
    snapshot: dict[str, Decimal],
    quote: dict[str, Decimal],
    spec: dict[str, str],
    now: datetime,
) -> TradeCommandCreate:
    """Build one risk-sized market command from an aggregated signal group."""
    primary = group["primary"]
    signal_json = primary["signal_json"]
    direction = group["direction"]
    entry = quote["ask" if direction == "BUY" else "bid"]
    stop_loss = Decimal(str(signal_json["proposed_stop_loss"]))
    basis = snapshot[config.risk.risk_basis.lower()]
    volume = calculate_volume(
        basis=basis,
        risk_fraction=Decimal(config.risk.per_trade_risk_fraction),
        entry=entry,
        stop_loss=stop_loss,
        tick_size=Decimal(spec["tick_size"]),
        tick_value_loss=Decimal(spec["tick_value_loss"]),
        volume_min=Decimal(spec["volume_min"]),
        volume_max=Decimal(spec["volume_max"]),
        volume_step=Decimal(spec["volume_step"]),
    )
    command_id = group_command_id(
        account_id, broker_symbol, timeframe, source_bar_time, direction
    )
    signal_expiry = datetime.fromisoformat(signal_json["valid_until"])
    return TradeCommandCreate(
        command_id=command_id,
        idempotency_key=command_id,
        account_id=account_id,
        command_type="OPEN_MARKET",
        expires_at=min(signal_expiry, now + timedelta(seconds=600)),
        strategy_instance_id=primary["strategy_instance_id"],
        signal_id=primary["signal_id"],
        created_by="signal-aggregator",
        payload={
            "symbol": broker_symbol,
            "side": direction,
            "volume": format(volume.normalize(), "f"),
            "stop_loss": stop_loss,
            "take_profit": Decimal(str(signal_json["proposed_take_profit"])),
            "magic_number": config.expert.magic_number,
        },
    )


def risk_context(
    group: dict[str, Any], analysis: dict[str, Any] | None
) -> dict[str, Any]:
    """Attach contributor IDs and advisory AI evidence without gating."""
    ai_fields = (
        "analysis_id",
        "model_provider",
        "model_id",
        "market_regime",
        "regime_confidence",
        "signal_decision",
        "risk_flags",
        "source_bar_time",
        "expires_at",
    )
    return {
        "signal_ids": group["signal_ids"],
        "ai": {key: analysis.get(key) for key in ai_fields} if analysis else None,
    }
