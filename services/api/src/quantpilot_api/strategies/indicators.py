"""Decimal-only market indicators (PRD 7.2 strategy inputs, doc 21.3).

Every function returns a series aligned to the input: warmup slots are None
so strategies can read both the latest value and the previous one for
edge-triggered signals. EMA/SMA/RSI follow standard definitions; ATR/ADX
use Wilder smoothing; MACD is fast/slow EMA difference with an EMA signal
line. 28-digit Decimal context; bar windows are a few hundred rows at
bar-close frequency, so this stays cheap.
"""

from __future__ import annotations

from decimal import Decimal
from typing import TYPE_CHECKING

if TYPE_CHECKING:  # pragma: no cover - import cycle guard for type checkers
    from .base import Bar

D0 = Decimal("0")
D100 = Decimal("100")


def sma_series(values: list[Decimal], period: int) -> list[Decimal | None]:
    if period <= 0:
        raise ValueError("period must be positive")
    out: list[Decimal | None] = [None] * len(values)
    if len(values) < period:
        return out
    running = sum(values[:period], D0)
    out[period - 1] = running / Decimal(period)
    for i in range(period, len(values)):
        running += values[i] - values[i - period]
        out[i] = running / Decimal(period)
    return out


def ema_series(values: list[Decimal], period: int) -> list[Decimal | None]:
    """EMA seeded with the SMA of the first `period` values."""
    if period <= 0:
        raise ValueError("period must be positive")
    out: list[Decimal | None] = [None] * len(values)
    if len(values) < period:
        return out
    alpha = Decimal(2) / (Decimal(period) + 1)
    prev = sum(values[:period], D0) / Decimal(period)
    out[period - 1] = prev
    for i in range(period, len(values)):
        prev = alpha * values[i] + (1 - alpha) * prev
        out[i] = prev
    return out


def rsi_series(closes: list[Decimal], period: int) -> list[Decimal | None]:
    """Wilder RSI. Defined from bar index `period` (needs period changes)."""
    n = len(closes)
    out: list[Decimal | None] = [None] * n
    if n < period + 1:
        return out
    gains: list[Decimal] = []
    losses: list[Decimal] = []
    for i in range(1, n):
        change = closes[i] - closes[i - 1]
        gains.append(change if change > 0 else D0)
        losses.append(-change if change < 0 else D0)
    avg_gain = sum(gains[:period], D0) / Decimal(period)
    avg_loss = sum(losses[:period], D0) / Decimal(period)
    out[period] = _rsi_value(avg_gain, avg_loss)
    for i in range(period + 1, n):
        avg_gain = (avg_gain * (period - 1) + gains[i - 1]) / Decimal(period)
        avg_loss = (avg_loss * (period - 1) + losses[i - 1]) / Decimal(period)
        out[i] = _rsi_value(avg_gain, avg_loss)
    return out


def _rsi_value(avg_gain: Decimal, avg_loss: Decimal) -> Decimal:
    if avg_loss == 0 and avg_gain == 0:
        return Decimal(50)  # flat: no directional information
    if avg_loss == 0:
        return D100
    rs = avg_gain / avg_loss
    return D100 - D100 / (1 + rs)


def true_ranges(bars: list["Bar"]) -> list[Decimal]:
    """TR[i] aligned to bar index; TR[0] = high - low (no previous close)."""
    out: list[Decimal] = []
    if not bars:
        return out
    out.append(bars[0].high - bars[0].low)
    for i in range(1, len(bars)):
        bar, prev = bars[i], bars[i - 1]
        out.append(
            max(
                bar.high - bar.low,
                abs(bar.high - prev.close),
                abs(bar.low - prev.close),
            )
        )
    return out


def atr_series(bars: list["Bar"], period: int) -> list[Decimal | None]:
    """Wilder ATR. First value at bar index `period` (mean of TR[1..period])."""
    n = len(bars)
    out: list[Decimal | None] = [None] * n
    if n < period + 1:
        return out
    trs = true_ranges(bars)
    atr = sum(trs[1 : period + 1], D0) / Decimal(period)
    out[period] = atr
    for i in range(period + 1, n):
        atr = (atr * (period - 1) + trs[i]) / Decimal(period)
        out[i] = atr
    return out


def adx_series(bars: list["Bar"], period: int) -> list[Decimal | None]:
    """Wilder ADX (+DM/-DM smoothed sums -> DI -> DX -> smoothed ADX).

    DX defined from bar index `period`; ADX first at index 2*period-1
    (mean of the first `period` DX values), then Wilder-smoothed.
    """
    n = len(bars)
    out: list[Decimal | None] = [None] * n
    if n < 2 * period + 1:
        return out
    plus_dm: list[Decimal] = [D0] * n
    minus_dm: list[Decimal] = [D0] * n
    trs = true_ranges(bars)
    for i in range(1, n):
        up = bars[i].high - bars[i - 1].high
        down = bars[i - 1].low - bars[i].low
        plus_dm[i] = up if (up > down and up > 0) else D0
        minus_dm[i] = down if (down > up and down > 0) else D0

    def _dx(plus_sum: Decimal, minus_sum: Decimal) -> Decimal:
        total = plus_sum + minus_sum
        if total == 0:
            return D0
        return D100 * abs(plus_sum - minus_sum) / total

    dx_series: list[Decimal | None] = [None] * n
    plus_sum = sum(plus_dm[1 : period + 1], D0)
    minus_sum = sum(minus_dm[1 : period + 1], D0)
    dx_series[period] = _dx(plus_sum, minus_sum)
    for i in range(period + 1, n):
        plus_sum = plus_sum - plus_sum / Decimal(period) + plus_dm[i]
        minus_sum = minus_sum - minus_sum / Decimal(period) + minus_dm[i]
        dx_series[i] = _dx(plus_sum, minus_sum)

    first_adx = sum(
        (dx for dx in dx_series[period : 2 * period] if dx is not None), D0
    ) / Decimal(period)
    out[2 * period - 1] = first_adx
    adx = first_adx
    for i in range(2 * period, n):
        dx = dx_series[i]
        assert dx is not None
        adx = (adx * (period - 1) + dx) / Decimal(period)
        out[i] = adx
    return out


def macd_series(
    closes: list[Decimal], fast_period: int, slow_period: int, signal_period: int
) -> tuple[list[Decimal | None], list[Decimal | None], list[Decimal | None]]:
    """(macd_line, signal_line, histogram); macd from index slow_period-1,
    histogram from index slow_period+signal_period-2."""
    n = len(closes)
    fast = ema_series(closes, fast_period)
    slow = ema_series(closes, slow_period)
    macd: list[Decimal | None] = [None] * n
    compact: list[Decimal] = []
    compact_at: list[int] = []
    for i in range(n):
        if fast[i] is not None and slow[i] is not None:
            macd[i] = fast[i] - slow[i]  # type: ignore[operator]
            compact.append(macd[i])  # type: ignore[arg-type]
            compact_at.append(i)
    signal: list[Decimal | None] = [None] * n
    for j, value in enumerate(ema_series(compact, signal_period)):
        if value is not None:
            signal[compact_at[j]] = value
    hist: list[Decimal | None] = [None] * n
    for i in range(n):
        if macd[i] is not None and signal[i] is not None:
            hist[i] = macd[i] - signal[i]
    return macd, signal, hist
