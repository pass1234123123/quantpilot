"""Mean Reversion strategy (PRD 7.3 #5, doc 12.2 seed 'mean_reversion').

Signal: price is stretched at least `entry_z` standard deviations beyond
its Bollinger mean AND the stretch starts reverting at the trigger bar -
doc 7.3: "price deviates from the mean and a reversion condition appears":

  z(i)  = (close(i) - SMA(i)) / stddev(i)     [0 when the window is flat]
  SELL  when z(i) >=  entry_z AND z(i) < z(i-1)   (stretched above, turning)
  BUY   when z(i) <= -entry_z AND z(i) > z(i-1)   (stretched below, turning)

Doc 7.3 warns the style "may stay counter-trend for extended periods during
strong trends", so an ADX gate blocks entries while ADX >= adx_threshold
(doc 8.4 tags the same idea with reason code RANGE_STABILITY). Stops stay
ATR-based; sizing stays the risk engine's job.

reason_codes: BOLLINGER_DEVIATION + Z_SCORE_REVERSION + RANGE_STABILITY.
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
from .indicators import adx_series, atr_series, bollinger_series

D0 = Decimal("0")


class MeanReversionConfig(AtrStopsConfig):
    bollinger_period: int = Field(default=20, ge=5, le=200)
    std_multiple: DecimalStr = Field(default=Decimal("2"), gt=0)
    entry_z: DecimalStr = Field(default=Decimal("2"), gt=0)
    adx_period: int = Field(default=14, ge=2, le=100)
    adx_threshold: int = Field(default=25, ge=10, le=50)


class MeanReversion(Strategy):
    strategy_id: ClassVar[str] = "mean_reversion"
    version: ClassVar[str] = "1.0.0"
    ConfigModel: ClassVar[type] = MeanReversionConfig

    @classmethod
    def warmup_bars(cls, config) -> int:
        cfg = (
            config
            if isinstance(config, MeanReversionConfig)
            else MeanReversionConfig.model_validate(config)
        )
        # Bollinger needs bollinger_period (+1 for the previous z); ADX needs
        # 2*period-1; ATR +1; the edge check needs both states valid.
        return cfg.bollinger_period + 2 * cfg.adx_period + cfg.atr_period + 3

    def on_bar(self, bars: list[Bar], config, timeframe: str) -> list[StrategySignal]:
        cfg = (
            config
            if isinstance(config, MeanReversionConfig)
            else MeanReversionConfig.model_validate(config)
        )
        closes = [bar.close for bar in bars]
        middle, upper, _ = bollinger_series(closes, cfg.bollinger_period, cfg.std_multiple)
        adx = adx_series(bars, cfg.adx_period)
        atr = atr_series(bars, cfg.atr_period)
        i = len(bars) - 1
        entry = cfg.entry_z
        threshold = Decimal(cfg.adx_threshold)

        def z_score(index: int) -> Decimal | None:
            if middle[index] is None or upper[index] is None:
                return None
            std = (upper[index] - middle[index]) / cfg.std_multiple
            if std == 0:
                return D0
            return (closes[index] - middle[index]) / std

        def state(index: int) -> int | None:
            """1 = stretched below and reverting up, -1 = stretched above and
            reverting down, 0 = no qualified reversion (incl. strong trend);
            None when indicators are not computable at `index`."""
            z_now, z_prev = z_score(index), z_score(index - 1)
            if z_now is None or z_prev is None or adx[index] is None:
                return None
            if adx[index] >= threshold:
                return 0  # strong trend: fading it is the documented failure mode
            if z_now >= entry and z_now < z_prev:
                return -1
            if z_now <= -entry and z_now > z_prev:
                return 1
            return 0

        now_state, prev_state = state(i), state(i - 1)
        if now_state in (None, 0) or now_state == prev_state or atr[i] is None:
            return []  # stretch without reversion, carry-over, or warmup gap
        direction = "SELL" if now_state == -1 else "BUY"
        reference = closes[i]
        z_now = z_score(i)
        assert z_now is not None
        strength = quantized_strength(abs(z_now) / (entry * 2))
        atr_now = atr[i]
        assert atr_now is not None
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
                    "BOLLINGER_DEVIATION",
                    "Z_SCORE_REVERSION",
                    "RANGE_STABILITY",
                ),
                features_ref=window_features_ref(bars, timeframe),
            )
        ]
