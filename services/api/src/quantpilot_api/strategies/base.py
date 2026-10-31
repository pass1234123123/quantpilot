"""Strategy engine core types (PRD 7.1/7.2).

Strategies produce explainable, backtestable, versioned signals - never
orders (PRD 7.1: the strategy engine has no execution path; the signal ->
risk gate -> command wiring is a separate later slice). All indicator math
is Decimal end-to-end (doc 21.3: price values never pass through float).

Interface notes (bar-close evaluation form, doc 22.2 M15/H1 cycle):
- The engine feeds the FULL closed-bar window ending at the trigger bar,
  so evaluation is stateless and deterministic: re-evaluating the same bar
  (JetStream redelivery, EA backfill replay) reproduces the same signal,
  and the UNIQUE(instance, source_bar_time) constraint keeps persistence
  idempotent (doc 7.5 dedup, 4.5 idempotency).
- on_tick exists in the PRD 7.2 interface; the cloud keeps no tick data
  (doc 4.4), so tick-level evaluation would run EA-side in a future slice.
  Not invented here (PRD 21.1).
"""

from __future__ import annotations

import hashlib
from abc import ABC, abstractmethod
from dataclasses import dataclass
from datetime import datetime, timedelta
from decimal import ROUND_HALF_UP, Decimal
from typing import Annotated, ClassVar, Literal

from pydantic import BaseModel, Field, PlainSerializer, model_validator

# Price-like config values serialize as decimal strings in JSON
# (doc 13.1/21.3) - same convention as contracts.Money.
DecimalStr = Annotated[Decimal, PlainSerializer(str, return_type=str, when_used="json")]

TIMEFRAME_MINUTES: dict[str, int] = {"M5": 5, "M15": 15, "M30": 30, "H1": 60, "H4": 240}
SUPPORTED_TIMEFRAMES = tuple(TIMEFRAME_MINUTES)


@dataclass(slots=True, frozen=True)
class Bar:
    """One closed bar (immutable broker fact, already persisted in symbol_bars)."""

    bar_time: datetime
    open: Decimal
    high: Decimal
    low: Decimal
    close: Decimal
    tick_volume: int = 0


@dataclass(slots=True, frozen=True)
class StrategySignal:
    """Advisory signal payload fields consumed by the persistence layer
    (PRD 7.4 standard structure)."""

    direction: Literal["BUY", "SELL"]
    strength: Decimal  # 0..1, dimensionless score (not money)
    reference_price: Decimal
    proposed_stop_loss: Decimal
    proposed_take_profit: Decimal
    market_data_at: datetime  # trigger bar close time
    valid_until: datetime
    reason_codes: tuple[str, ...]
    features_ref: str
    signal_type: str = "ENTRY"


def bar_close_time(bar_time: datetime, timeframe: str) -> datetime:
    return bar_time + timedelta(minutes=TIMEFRAME_MINUTES[timeframe])


def clamp01(value: Decimal) -> Decimal:
    return min(max(value, Decimal("0")), Decimal("1"))


def quantized_strength(value: Decimal) -> Decimal:
    return clamp01(value).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)


def stops_from_atr(
    direction: str,
    reference: Decimal,
    atr: Decimal,
    sl_multiple: Decimal,
    tp_multiple: Decimal,
) -> tuple[Decimal, Decimal]:
    """ATR-based proposed SL/TP around the reference (close) price."""
    if direction == "BUY":
        return reference - atr * sl_multiple, reference + atr * tp_multiple
    return reference + atr * sl_multiple, reference - atr * tp_multiple


def window_features_ref(bars: list[Bar], timeframe: str) -> str:
    """Deterministic digest of the exact bar window a signal was computed
    from (doc 7.5: every signal records its feature snapshot for backtest
    reproducibility). Same window -> same ref, replay included."""
    digest = hashlib.sha256()
    digest.update(f"{timeframe}|{len(bars)}|".encode())
    for bar in bars:
        digest.update(
            f"{bar.bar_time.isoformat()}|{bar.open}|{bar.high}|{bar.low}|"
            f"{bar.close}|{bar.tick_volume};".encode()
        )
    return "fswin-" + digest.hexdigest()[:16]


class AtrStopsConfig(BaseModel):
    """Shared ATR-stop parameters (doc 7.3: ATR-based stop distance)."""

    model_config = {"extra": "forbid", "str_strip_whitespace": True}

    atr_period: int = Field(default=14, ge=2, le=100)
    sl_atr_multiple: DecimalStr = Field(default=Decimal("2"), gt=0)
    tp_atr_multiple: DecimalStr = Field(default=Decimal("3"), gt=0)


class Strategy(ABC):
    """PRD 7.2 strategy interface (bar-close evaluation form).

    strategy_id/version identify the catalog entry this class implements;
    the engine cross-checks version against the strategy_versions row bound
    to an instance before evaluating (7.6: live versions are never silently
    replaced). validate_config is the Pydantic gate required by 7.6
    (JSON Schema/Pydantic validation, configs immutable + revisioned).
    """

    strategy_id: ClassVar[str]
    version: ClassVar[str]
    supported_timeframes: ClassVar[tuple[str, ...]] = SUPPORTED_TIMEFRAMES
    ConfigModel: ClassVar[type[BaseModel]]

    @classmethod
    def validate_config(cls, config: dict | BaseModel) -> BaseModel:
        return cls.ConfigModel.model_validate(
            config.model_dump() if isinstance(config, BaseModel) else config
        )

    @classmethod
    @abstractmethod
    def warmup_bars(cls, config: BaseModel) -> int:
        """Bars needed before on_bar can emit (indicator warmup + edge check)."""

    @abstractmethod
    def on_bar(self, bars: list[Bar], config: BaseModel, timeframe: str) -> list[StrategySignal]:
        """Evaluate the closed-bar window (oldest-first, trigger bar last).

        Edge-triggered by contract (doc 7.3: avoid unclosed-bar jitter -
        signals fire when the condition CHANGES at the trigger bar, not on
        every bar the condition holds).
        """

    def on_reset(self) -> None:  # stateless evaluation: nothing to reset
        return None


__all__ = [
    "AtrStopsConfig",
    "Bar",
    "DecimalStr",
    "Strategy",
    "StrategySignal",
    "SUPPORTED_TIMEFRAMES",
    "TIMEFRAME_MINUTES",
    "bar_close_time",
    "clamp01",
    "quantized_strength",
    "stops_from_atr",
    "window_features_ref",
]
