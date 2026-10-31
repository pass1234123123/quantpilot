"""Trend Following strategy (PRD 7.3 #1, doc 12.2 seed trend_following).

Signal: the market ENTERS a qualified trend state at the trigger bar
(edge trigger) - direction from the EMA and its slope, strength from ADX:
  qualified_up(t)  = close(t) > EMA(t)  AND EMA(t)  > EMA(t-1)  AND ADX(t)  >= threshold
  qualified_down(t)= close(t) < EMA(t)  AND EMA(t)  < EMA(t-1)  AND ADX(t)  >= threshold
BUY when qualified_up holds at the trigger bar but NOT at the previous one
(SELL mirrored). The ADX gate keeps chop out (doc 7.3 note: ranging
environments repeatedly stop this style - the gate reduces, not removes,
that risk; position sizing/stops remain the risk engine's job).

reason_codes use the exact names from the PRD 7.4 example
(EMA_TREND_UP / ADX_ABOVE_THRESHOLD).
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
    Strategy,
    StrategySignal,
    bar_close_time,
    quantized_strength,
    stops_from_atr,
    window_features_ref,
)
from .indicators import adx_series, atr_series, ema_series


class TrendFollowingConfig(AtrStopsConfig):
    ema_period: int = Field(default=50, ge=2, le=400)
    adx_period: int = Field(default=14, ge=2, le=100)
    adx_threshold: int = Field(default=25, ge=10, le=50)


class TrendFollowing(Strategy):
    strategy_id: ClassVar[str] = "trend_following"
    version: ClassVar[str] = "1.0.0"
    ConfigModel: ClassVar[type] = TrendFollowingConfig

    @classmethod
    def warmup_bars(cls, config) -> int:
        cfg = (
            config
            if isinstance(config, TrendFollowingConfig)
            else TrendFollowingConfig.model_validate(config)
        )
        # ADX needs 2*period-1 bars; EMA needs ema_period (+1 for slope);
        # ATR +1; the edge check needs i and i-1 both valid.
        return cfg.ema_period + 2 * cfg.adx_period + cfg.atr_period + 3

    def on_bar(self, bars: list[Bar], config, timeframe: str) -> list[StrategySignal]:
        cfg = (
            config
            if isinstance(config, TrendFollowingConfig)
            else TrendFollowingConfig.model_validate(config)
        )
        closes = [bar.close for bar in bars]
        ema = ema_series(closes, cfg.ema_period)
        adx = adx_series(bars, cfg.adx_period)
        atr = atr_series(bars, cfg.atr_period)
        i = len(bars) - 1
        if i < 2:
            return []
        threshold = Decimal(cfg.adx_threshold)

        def state(index: int) -> int | None:
            """1 = qualified up, -1 = qualified down, 0 = no qualified trend;
            None when indicators are not computable at `index`."""
            if ema[index] is None or ema[index - 1] is None or adx[index] is None:
                return None
            if adx[index] < threshold:
                return 0
            rising = ema[index] > ema[index - 1]
            if closes[index] > ema[index] and rising:
                return 1
            if closes[index] < ema[index] and not rising:
                return -1
            return 0

        now_state, prev_state = state(i), state(i - 1)
        if (
            now_state is None
            or prev_state is None
            or atr[i] is None
            or now_state == 0
            or now_state == prev_state
        ):
            return []  # not in a trend, indicators incomplete, or state carried over
        direction = "BUY" if now_state == 1 else "SELL"
        reference = closes[i]
        strength = quantized_strength(adx[i] / Decimal(50))
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
                    ("EMA_TREND_UP", "ADX_ABOVE_THRESHOLD")
                    if now_state
                    else ("EMA_TREND_DOWN", "ADX_ABOVE_THRESHOLD")
                ),
                features_ref=window_features_ref(bars, timeframe),
            )
        ]
