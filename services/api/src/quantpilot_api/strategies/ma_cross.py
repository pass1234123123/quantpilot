"""Moving Average Cross strategy (PRD 7.3 #2, doc 12.2 seed ma_cross).

Signal: fast MA crosses above (BUY) / below (SELL) the slow MA on the
newest CLOSED bar, edge-triggered (the cross must happen at the trigger
bar; a bar where the condition merely persists emits nothing). Only closed
bars are evaluated, so unclosed-bar jitter is structurally impossible.
"""

from __future__ import annotations

from datetime import timedelta
from decimal import Decimal
from typing import ClassVar

from pydantic import Field, model_validator

from .base import (
    TIMEFRAME_MINUTES,
    AtrStopsConfig,
    Bar,
    Strategy,
    StrategySignal,
    bar_close_time,
    quantized_strength,
    stops_from_atr,
    window_features_ref,
)
from .indicators import atr_series, ema_series, sma_series


class MaCrossConfig(AtrStopsConfig):
    fast_period: int = Field(default=9, ge=2, le=200)
    slow_period: int = Field(default=21, ge=3, le=400)
    ma_type: str = Field(default="EMA", pattern="^(EMA|SMA)$")

    @model_validator(mode="after")
    def _fast_below_slow(self) -> "MaCrossConfig":
        if self.fast_period >= self.slow_period:
            raise ValueError("fast_period must be smaller than slow_period")
        return self


class MovingAverageCross(Strategy):
    strategy_id: ClassVar[str] = "ma_cross"
    version: ClassVar[str] = "1.0.0"
    ConfigModel: ClassVar[type] = MaCrossConfig

    @classmethod
    def warmup_bars(cls, config) -> int:
        cfg = config if isinstance(config, MaCrossConfig) else MaCrossConfig.model_validate(config)
        # slow MA needs slow_period bars; ATR needs atr_period+1; the edge
        # check needs both the trigger bar and the previous one valid.
        return cfg.slow_period + cfg.atr_period + 2

    def on_bar(self, bars: list[Bar], config, timeframe: str) -> list[StrategySignal]:
        cfg = config if isinstance(config, MaCrossConfig) else MaCrossConfig.model_validate(config)
        closes = [bar.close for bar in bars]
        ma = ema_series if cfg.ma_type == "EMA" else sma_series
        fast = ma(closes, cfg.fast_period)
        slow = ma(closes, cfg.slow_period)
        atr = atr_series(bars, cfg.atr_period)
        i = len(bars) - 1
        if i < 1:
            return []
        if any(
            series is None or series[index] is None
            for series, index in (
                (fast, i), (fast, i - 1), (slow, i), (slow, i - 1), (atr, i),
            )
        ):
            return []

        crossed_up = fast[i - 1] <= slow[i - 1] and fast[i] > slow[i]
        crossed_down = fast[i - 1] >= slow[i - 1] and fast[i] < slow[i]
        if not (crossed_up or crossed_down):
            return []  # no edge at this bar: condition unchanged (doc 7.5 dedup)

        direction = "BUY" if crossed_up else "SELL"
        reference = closes[i]
        strength = (
            quantized_strength(abs(fast[i] - slow[i]) / atr[i])
            if atr[i] != 0
            else Decimal("1")
        )
        stop_loss, take_profit = stops_from_atr(
            direction, reference, atr[i], cfg.sl_atr_multiple, cfg.tp_atr_multiple
        )
        close_at = bar_close_time(bars[i].bar_time, timeframe)
        return [
            StrategySignal(
                direction=direction,
                strength=strength,
                reference_price=reference,
                proposed_stop_loss=stop_loss,
                proposed_take_profit=take_profit,
                market_data_at=close_at,
                valid_until=close_at + timedelta(minutes=TIMEFRAME_MINUTES[timeframe]),
                reason_codes=(
                    "MA_FAST_CROSSED_ABOVE" if crossed_up else "MA_FAST_CROSSED_BELOW",
                ),
                features_ref=window_features_ref(bars, timeframe),
            )
        ]
