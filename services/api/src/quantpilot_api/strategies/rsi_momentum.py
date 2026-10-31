"""RSI/MACD Momentum strategy (PRD 7.3 #3, doc 12.2 seed rsi_momentum).

Signal: MACD histogram flips sign at the trigger bar (edge trigger) AND the
RSI sits inside the directional momentum band - BUY needs RSI in
[rsi_buy_min, rsi_buy_max], SELL needs RSI in [rsi_sell_min, rsi_sell_max].
The RSI band keeps "overbought/oversold" extremes out: the PRD warns that
extremes do not imply an immediate reversal, so the band is mid-range by
default (52-75 buy / 25-48 sell).
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
from .indicators import atr_series, macd_series, rsi_series


class RsiMomentumConfig(AtrStopsConfig):
    macd_fast: int = Field(default=12, ge=2, le=100)
    macd_slow: int = Field(default=26, ge=3, le=200)
    macd_signal: int = Field(default=9, ge=2, le=100)
    rsi_period: int = Field(default=14, ge=2, le=100)
    rsi_buy_min: int = Field(default=52, ge=50, le=90)
    rsi_buy_max: int = Field(default=75, ge=50, le=95)
    rsi_sell_max: int = Field(default=48, ge=5, le=50)
    rsi_sell_min: int = Field(default=25, ge=1, le=50)

    @model_validator(mode="after")
    def _bands_coherent(self) -> "RsiMomentumConfig":
        if self.macd_fast >= self.macd_slow:
            raise ValueError("macd_fast must be smaller than macd_slow")
        if self.rsi_buy_min >= self.rsi_buy_max:
            raise ValueError("rsi_buy_min must be smaller than rsi_buy_max")
        if self.rsi_sell_min >= self.rsi_sell_max:
            raise ValueError("rsi_sell_min must be smaller than rsi_sell_max")
        return self


class RsiMacdMomentum(Strategy):
    strategy_id: ClassVar[str] = "rsi_momentum"
    version: ClassVar[str] = "1.0.0"
    ConfigModel: ClassVar[type] = RsiMomentumConfig

    @classmethod
    def warmup_bars(cls, config) -> int:
        cfg = (
            config
            if isinstance(config, RsiMomentumConfig)
            else RsiMomentumConfig.model_validate(config)
        )
        # histogram needs slow+signal MAs; RSI needs rsi_period+1; ATR +1; edge +1
        return cfg.macd_slow + cfg.macd_signal + cfg.rsi_period + cfg.atr_period + 2

    def on_bar(self, bars: list[Bar], config, timeframe: str) -> list[StrategySignal]:
        cfg = (
            config
            if isinstance(config, RsiMomentumConfig)
            else RsiMomentumConfig.model_validate(config)
        )
        closes = [bar.close for bar in bars]
        _, _, hist = macd_series(closes, cfg.macd_fast, cfg.macd_slow, cfg.macd_signal)
        rsi = rsi_series(closes, cfg.rsi_period)
        atr = atr_series(bars, cfg.atr_period)
        i = len(bars) - 1
        if i < 1:
            return []
        if hist[i] is None or hist[i - 1] is None or rsi[i] is None or atr[i] is None:
            return []

        flip_up = (
            hist[i - 1] <= 0
            and hist[i] > 0
            and cfg.rsi_buy_min <= rsi[i] <= cfg.rsi_buy_max
        )
        flip_down = (
            hist[i - 1] >= 0
            and hist[i] < 0
            and cfg.rsi_sell_min <= rsi[i] <= cfg.rsi_sell_max
        )
        if not (flip_up or flip_down):
            return []  # no sign flip, or RSI outside the momentum band

        direction = "BUY" if flip_up else "SELL"
        reference = closes[i]
        strength = (
            quantized_strength(abs(hist[i]) / atr[i]) if atr[i] != 0 else Decimal("1")
        )
        stop_loss, take_profit = stops_from_atr(
            direction, reference, atr[i], cfg.sl_atr_multiple, cfg.tp_atr_multiple
        )
        close_at = bar_close_time(bars[i].bar_time, timeframe)
        reason_codes = (
            ("MACD_HIST_FLIPPED_POSITIVE", "RSI_IN_BUY_BAND")
            if flip_up
            else ("MACD_HIST_FLIPPED_NEGATIVE", "RSI_IN_SELL_BAND")
        )
        return [
            StrategySignal(
                direction=direction,
                strength=strength,
                reference_price=reference,
                proposed_stop_loss=stop_loss,
                proposed_take_profit=take_profit,
                market_data_at=close_at,
                valid_until=close_at + timedelta(minutes=TIMEFRAME_MINUTES[timeframe]),
                reason_codes=reason_codes,
                features_ref=window_features_ref(bars, timeframe),
            )
        ]
