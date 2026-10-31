"""Breakout strategy (PRD 7.3 #4, doc 12.2 catalog seed 'breakout').

Signal: the close breaks OUT of the prior `lookback_period`-bar high/low
range (the channel excludes the trigger bar so a bar can never break its
own extreme), that range was volatility-compressed relative to ATR, and
this is the FIRST such breakout bar in that direction (edge trigger):

  upper(i) = max(high[i-lookback .. i-1])    lower(i) = min(low[same])
  compressed(i) = upper(i) - lower(i) <= squeeze_width_max * ATR(i-1)
  BUY  when close(i) > upper(i)  AND compressed AND state flipped to +1 at i
  SELL mirrored below the lower channel.

Doc 7.3 names false breakouts and slippage as the style's risks: the
compression filter only trades range expansions out of quiet bases, stops
stay ATR-based, and sizing stays the risk engine's job.

reason_codes: RANGE_BREAKOUT_UP / RANGE_BREAKOUT_DOWN + VOLATILITY_COMPRESSION.
"""

from __future__ import annotations

from datetime import timedelta
from decimal import Decimal
from typing import ClassVar

from pydantic import Field

from .base import (
    TIMEFRAME_MINUTES,
    AtrStopsConfig,
    Bar,
    DecimalStr,
    Strategy,
    StrategySignal,
    bar_close_time,
    quantized_strength,
    stops_from_atr,
    window_features_ref,
)
from .indicators import atr_series


class BreakoutConfig(AtrStopsConfig):
    lookback_period: int = Field(default=20, ge=5, le=200)
    squeeze_width_max: DecimalStr = Field(default=Decimal("4"), gt=0)


class Breakout(Strategy):
    strategy_id: ClassVar[str] = "breakout"
    version: ClassVar[str] = "1.0.0"
    ConfigModel: ClassVar[type] = BreakoutConfig

    @classmethod
    def warmup_bars(cls, config) -> int:
        cfg = (
            config
            if isinstance(config, BreakoutConfig)
            else BreakoutConfig.model_validate(config)
        )
        # channel needs `lookback` prior bars at both i and i-1; ATR must be
        # defined at i and i-1; the edge check needs both states valid.
        return cfg.lookback_period + cfg.atr_period + 3

    def on_bar(self, bars: list[Bar], config, timeframe: str) -> list[StrategySignal]:
        cfg = (
            config
            if isinstance(config, BreakoutConfig)
            else BreakoutConfig.model_validate(config)
        )
        closes = [bar.close for bar in bars]
        atr = atr_series(bars, cfg.atr_period)
        lookback = cfg.lookback_period
        i = len(bars) - 1

        def channel(index: int) -> tuple[Decimal, Decimal] | None:
            if index < lookback:
                return None
            window = bars[index - lookback:index]
            return (
                max(bar.high for bar in window),
                min(bar.low for bar in window),
            )

        def state(index: int) -> int | None:
            """1 = close above the channel, -1 = below, 0 = inside;
            None when the channel is not computable at `index`."""
            bounds = channel(index)
            if bounds is None:
                return None
            upper, lower = bounds
            if closes[index] > upper:
                return 1
            if closes[index] < lower:
                return -1
            return 0

        now_state, prev_state = state(i), state(i - 1)
        if (
            now_state in (None, 0)
            or now_state == prev_state
            or atr[i] is None
            or atr[i - 1] is None
        ):
            return []  # no fresh breakout, or indicators incomplete
        upper, lower = channel(i)  # type: ignore[misc]
        if upper - lower > cfg.squeeze_width_max * atr[i - 1]:
            return []  # wide range: expansion out of it is not the quiet-base play
        direction = "BUY" if now_state == 1 else "SELL"
        reference = closes[i]
        atr_now = atr[i]
        assert atr_now is not None
        distance = reference - upper if direction == "BUY" else lower - reference
        strength = quantized_strength(distance / atr_now)
        stop_loss, take_profit = stops_from_atr(
            direction, reference, atr_now, cfg.sl_atr_multiple, cfg.tp_atr_multiple
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
                    ("RANGE_BREAKOUT_UP", "VOLATILITY_COMPRESSION")
                    if direction == "BUY"
                    else ("RANGE_BREAKOUT_DOWN", "VOLATILITY_COMPRESSION")
                ),
                features_ref=window_features_ref(bars, timeframe),
            )
        ]
