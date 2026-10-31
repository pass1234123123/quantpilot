"""Strategy evaluation orchestration (PRD 3.2 closed loop, chapter 7).

Bar batch -> symbol_bars projection (writers.handle_bars) -> evaluation on
the newest closed bar for every ENABLED strategy instance bound to that
(account, symbol, timeframe) -> strategy_signals rows.

Signals are advisory ONLY: nothing here creates a command (PRD 7.1); the
signal -> aggregator -> risk gate -> command wiring is a later slice.

Determinism + idempotency: evaluation is stateless - the engine loads the
closed-bar window from symbol_bars and replays the indicator math, so a
re-delivered batch (JetStream at-least-once) or an EA history replay
re-produces the same signal; UNIQUE(strategy_instance_id, source_bar_time)
turns the second insert into a no-op (doc 7.5 dedup, 4.5 idempotency).

Evaluation failures NEVER block the bars projection: handle_bars commits
the upserts first and this module isolates per-instance errors to a
system event + log line (a poisoned strategy must not stall the consumer).
"""

from __future__ import annotations

import json
import logging
import uuid
from datetime import datetime, timezone
from typing import Any

from . import db, writers
from .strategies import REGISTRY, Bar

logger = logging.getLogger("quantpilot.strategy")

# extra bars beyond warmup loaded per evaluation (harmless margin: more
# history makes the very first evaluation after backfill immediately valid)
_WINDOW_MARGIN = 10


def _enabled_instances(account_id: str, broker_symbol: str, timeframe: str) -> list[dict[str, Any]]:
    return db.query(
        """
        SELECT si.id::text AS instance_id, si.config, si.broker_symbol, si.timeframe,
               s.code AS strategy_code, sv.version AS strategy_version
          FROM strategy_instances si
          JOIN strategy_versions sv ON sv.id = si.strategy_version_id
          JOIN strategies s ON s.id = sv.strategy_id
         WHERE si.account_id = %s AND si.broker_symbol = %s AND si.timeframe = %s
           AND si.status = 'ENABLED' AND s.status = 'ACTIVE'
         ORDER BY si.created_at
        """,
        (account_id, broker_symbol, timeframe),
    )


def _bar_window(
    account_id: str, broker_symbol: str, timeframe: str, source_bar_time: datetime, size: int
) -> list[Bar]:
    rows = db.query(
        """
        SELECT bar_time, open, high, low, close, tick_volume
          FROM symbol_bars
         WHERE account_id = %s AND broker_symbol = %s AND timeframe = %s
           AND bar_time <= %s
         ORDER BY bar_time DESC
         LIMIT %s
        """,
        (account_id, broker_symbol, timeframe, source_bar_time, size),
    )
    rows.reverse()  # oldest-first; trigger bar is the last element
    return [
        Bar(
            bar_time=row["bar_time"],
            open=row["open"],
            high=row["high"],
            low=row["low"],
            close=row["close"],
            tick_volume=row["tick_volume"],
        )
        for row in rows
    ]


def _signal_json(
    signal: Any,
    *,
    strategy_code: str,
    strategy_version: str,
    account_id: str,
    broker_symbol: str,
    timeframe: str,
    signal_id: str,
    generated_at: datetime,
) -> dict[str, Any]:
    """Doc 7.4 standard structure. Prices are decimal strings (PRD 13.1/21.3);
    strength is a dimensionless score, kept as a JSON number like the doc
    example (0.72)."""
    return {
        "signal_id": signal_id,
        "strategy_id": strategy_code,
        "strategy_version": strategy_version,
        "account_id": account_id,
        "symbol": broker_symbol,
        "timeframe": timeframe,
        "direction": signal.direction,
        "signal_type": signal.signal_type,
        "strength": float(signal.strength),
        "generated_at": generated_at.isoformat(),
        "market_data_at": signal.market_data_at.isoformat(),
        "reference_price": str(signal.reference_price),
        "proposed_stop_loss": str(signal.proposed_stop_loss),
        "proposed_take_profit": str(signal.proposed_take_profit),
        "valid_until": signal.valid_until.isoformat(),
        "features_ref": signal.features_ref,
        "reason_codes": list(signal.reason_codes),
    }


def _skip(instance: dict[str, Any], reason: str, detail: str) -> None:
    logger.warning("strategy instance %s skipped: %s (%s)",
                   instance["instance_id"], reason, detail)
    writers.add_system_event(
        source="strategy-engine",
        event_type="strategy.instance_skipped",
        severity="WARN",
        payload={
            "instance_id": instance["instance_id"],
            "strategy_code": instance["strategy_code"],
            "reason": reason,
            "detail": detail,
        },
    )


def evaluate_newest_bar(
    account_id: str, broker_symbol: str, timeframe: str, source_bar_time: datetime
) -> int:
    """Evaluate every ENABLED instance bound to (account, symbol, timeframe)
    on the bar that just closed (source_bar_time = its open time).

    Returns the number of signals newly persisted (0 when nothing fired or
    everything was a dedup no-op)."""
    emitted = 0
    instances = _enabled_instances(account_id, broker_symbol, timeframe)
    if not instances:
        return 0

    for instance in instances:
        code = instance["strategy_code"]
        cls = REGISTRY.get(code)
        if cls is None:
            _skip(instance, "NO_IMPLEMENTATION",
                  f"no python implementation registered for strategy code {code!r}")
            continue
        if instance["strategy_version"] != cls.version:
            # 7.6: instances are bound to an immutable version; a mismatched
            # implementation must never silently substitute itself
            _skip(
                instance, "VERSION_MISMATCH",
                f"instance bound to version {instance['strategy_version']!r} but the"
                f" registered implementation provides {cls.version!r}",
            )
            continue
        try:
            config = cls.validate_config(instance["config"])
        except Exception as exc:
            _skip(instance, "INVALID_CONFIG", str(exc))
            continue
        if timeframe not in cls.supported_timeframes:
            _skip(instance, "TIMEFRAME_UNSUPPORTED", timeframe)
            continue

        window = _bar_window(
            account_id,
            broker_symbol,
            timeframe,
            source_bar_time,
            cls.warmup_bars(config) + _WINDOW_MARGIN,
        )
        if not window or window[-1].bar_time != source_bar_time:
            continue  # trigger bar not (yet) persisted - nothing to evaluate
        if len(window) < cls.warmup_bars(config):
            logger.debug(
                "instance %s warmup: %d/%d bars",
                instance["instance_id"], len(window), cls.warmup_bars(config),
            )
            continue

        try:
            signals = cls().on_bar(window, config, timeframe)
        except Exception as exc:
            _skip(instance, "EVALUATION_ERROR", f"{type(exc).__name__}: {exc}")
            continue

        generated_at = datetime.now(timezone.utc)
        for signal in signals:
            signal_id = str(uuid.uuid4())
            doc = _signal_json(
                signal,
                strategy_code=code,
                strategy_version=instance["strategy_version"],
                account_id=account_id,
                broker_symbol=broker_symbol,
                timeframe=timeframe,
                signal_id=signal_id,
                generated_at=generated_at,
            )
            with db.conn() as connection:
                inserted = connection.execute(
                    """
                    INSERT INTO strategy_signals
                           (signal_id, account_id, strategy_instance_id, signal_json,
                            source_bar_time, generated_at, expires_at)
                    VALUES (%s, %s, %s, CAST(%s AS jsonb), %s, %s, %s)
                    ON CONFLICT DO NOTHING
                    RETURNING id::text
                    """,
                    (
                        uuid.UUID(signal_id),
                        uuid.UUID(account_id),
                        uuid.UUID(instance["instance_id"]),
                        json.dumps(doc),
                        source_bar_time,
                        generated_at,
                        signal.valid_until,
                    ),
                ).fetchone()
            if inserted is not None:
                emitted += 1
                logger.info(
                    "signal %s: %s %s %s@%s instance=%s",
                    signal_id, code, signal.direction, broker_symbol,
                    timeframe, instance["instance_id"],
                )
    return emitted
