"""Deterministic closed-bar strategy backtesting (PRD 15).

The exact live Strategy.on_bar implementations and window sizing are reused.
Signals are known at a bar close and fill at the NEXT bar open, so signal-bar
OHLC can never be used for its own fill. This module is pure research math:
it has no database, command, risk-gate, Agent, or EA execution path.
"""

from __future__ import annotations

import math
import statistics
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from .strategies import Bar, Strategy, StrategySignal
from .strategies.base import DecimalStr, TIMEFRAME_MINUTES, bar_close_time
from .strategy_engine import _WINDOW_MARGIN

_ZERO = Decimal("0")


class BacktestCostModel(BaseModel):
    """Explicit research-cost assumptions; prices are broker-price units."""

    model_config = ConfigDict(extra="forbid")

    spread_per_side: DecimalStr = Field(default=_ZERO, ge=0)
    slippage_per_side: DecimalStr = Field(default=_ZERO, ge=0)
    commission_per_lot_per_side: DecimalStr = Field(default=_ZERO, ge=0)
    swap_per_lot_per_bar: DecimalStr = Field(default=_ZERO, ge=0)
    contract_size: DecimalStr = Field(default=Decimal("100000"), gt=0)
    quote_to_account_rate: DecimalStr = Field(default=Decimal("1"), gt=0)


@dataclass(slots=True)
class _Position:
    signal: StrategySignal
    source_bar_time: datetime
    entry_index: int
    entry_time: datetime
    raw_entry: Decimal
    entry_price: Decimal


def _price_value(volume: Decimal, costs: BacktestCostModel) -> Decimal:
    return volume * costs.contract_size * costs.quote_to_account_rate


def _side_costs(volume: Decimal, costs: BacktestCostModel, held_bars: int) -> dict[str, Decimal]:
    value = _price_value(volume, costs)
    return {
        "spread": costs.spread_per_side * 2 * value,
        "slippage": costs.slippage_per_side * 2 * value,
        "commission": costs.commission_per_lot_per_side * 2 * volume,
        "swap": costs.swap_per_lot_per_bar * volume * held_bars,
    }


def _raw_exit(position: _Position, bar: Bar, timeframe: str) -> tuple[Decimal, str, datetime] | None:
    """Conservative intrabar model: stop wins when SL and TP are both touched."""
    signal = position.signal
    sl, tp = signal.proposed_stop_loss, signal.proposed_take_profit
    close_at = bar_close_time(bar.bar_time, timeframe)
    if signal.direction == "BUY":
        if bar.open <= sl:
            return bar.open, "STOP_LOSS_GAP", bar.bar_time
        if bar.open >= tp:
            return bar.open, "TAKE_PROFIT_GAP", bar.bar_time
        hit_sl, hit_tp = bar.low <= sl, bar.high >= tp
    else:
        if bar.open >= sl:
            return bar.open, "STOP_LOSS_GAP", bar.bar_time
        if bar.open <= tp:
            return bar.open, "TAKE_PROFIT_GAP", bar.bar_time
        hit_sl, hit_tp = bar.high >= sl, bar.low <= tp
    if hit_sl:
        return sl, "STOP_LOSS", close_at
    if hit_tp:
        return tp, "TAKE_PROFIT", close_at
    return None


def _ratio(numerator: Decimal, denominator: Decimal) -> Decimal | None:
    return numerator / denominator if denominator != 0 else None


def _risk_metrics(returns: list[float], periods_per_year: int) -> tuple[float | None, float | None]:
    if len(returns) < 2:
        return None, None
    mean_return = statistics.fmean(returns)
    volatility = statistics.stdev(returns)
    annualizer = math.sqrt(periods_per_year)
    sharpe = mean_return / volatility * annualizer if volatility > 0 else None
    downside = math.sqrt(statistics.fmean([min(value, 0.0) ** 2 for value in returns]))
    sortino = mean_return / downside * annualizer if downside > 0 else None
    return sharpe, sortino


def run_backtest(
    *,
    bars: list[Bar],
    strategy: Strategy,
    config: Any,
    timeframe: str,
    initial_balance: Decimal,
    volume: Decimal,
    cost_model: BacktestCostModel,
) -> dict[str, Any]:
    """Run one fixed-volume position at a time over oldest-first closed bars."""
    if len(bars) < 2:
        raise ValueError("at least two closed bars are required")
    if initial_balance <= 0 or volume <= 0:
        raise ValueError("initial_balance and volume must be positive")
    if timeframe not in TIMEFRAME_MINUTES:
        raise ValueError(f"unsupported timeframe {timeframe!r}")

    warmup = strategy.warmup_bars(config)
    window_size = warmup + _WINDOW_MARGIN
    price_value = _price_value(volume, cost_model)
    balance = initial_balance
    position: _Position | None = None
    pending: tuple[StrategySignal, datetime] | None = None
    trades: list[dict[str, Any]] = []
    equity_curve: list[dict[str, Any]] = []
    exposed_bars = 0

    def close_position(raw_exit: Decimal, reason: str, exit_time: datetime, index: int) -> None:
        nonlocal balance, position
        assert position is not None
        direction = Decimal("1") if position.signal.direction == "BUY" else Decimal("-1")
        gross = (raw_exit - position.raw_entry) * direction * price_value
        costs = _side_costs(volume, cost_model, index - position.entry_index + 1)
        net = gross - sum(costs.values(), _ZERO)
        exit_price = raw_exit - direction * (cost_model.spread_per_side + cost_model.slippage_per_side)
        trades.append({
            "source_bar_time": position.source_bar_time,
            "entry_time": position.entry_time,
            "exit_time": exit_time,
            "direction": position.signal.direction,
            "volume": volume,
            "entry_price": position.entry_price,
            "exit_price": exit_price,
            "gross_profit": gross,
            "net_profit": net,
            "costs": costs,
            "exit_reason": reason,
            "holding_minutes": Decimal(int((exit_time - position.entry_time).total_seconds()) // 60),
            "features_ref": position.signal.features_ref,
            "reason_codes": list(position.signal.reason_codes),
        })
        balance += net
        position = None

    for index, current in enumerate(bars):
        if pending is not None and position is None:
            signal, source_bar_time = pending
            direction = Decimal("1") if signal.direction == "BUY" else Decimal("-1")
            position = _Position(
                signal=signal,
                source_bar_time=source_bar_time,
                entry_index=index,
                entry_time=current.bar_time,
                raw_entry=current.open,
                entry_price=current.open + direction * (
                    cost_model.spread_per_side + cost_model.slippage_per_side
                ),
            )
        pending = None

        if position is not None:
            exposed_bars += 1
            exit_fill = _raw_exit(position, current, timeframe)
            if exit_fill is not None:
                close_position(*exit_fill, index)

        if index == len(bars) - 1 and position is not None:
            close_position(
                current.close,
                "END_OF_TEST",
                bar_close_time(current.bar_time, timeframe),
                index,
            )

        if position is None:
            equity = balance
        else:
            direction = Decimal("1") if position.signal.direction == "BUY" else Decimal("-1")
            unrealized = (current.close - position.raw_entry) * direction * price_value
            estimated_costs = _side_costs(volume, cost_model, index - position.entry_index + 1)
            equity = balance + unrealized - sum(estimated_costs.values(), _ZERO)
        equity_curve.append({"bar_time": current.bar_time, "equity": equity})

        if position is None and index < len(bars) - 1:
            window = bars[max(0, index - window_size + 1):index + 1]
            if len(window) >= warmup:
                signals = strategy.on_bar(window, config, timeframe)
                if signals:
                    pending = (signals[0], current.bar_time)

    metrics_net = balance - initial_balance
    winning = [trade["net_profit"] for trade in trades if trade["net_profit"] > 0]
    losing = [trade["net_profit"] for trade in trades if trade["net_profit"] < 0]
    gross_profit = sum(winning, _ZERO)
    gross_loss = sum(losing, _ZERO)
    total_costs = {
        key: sum((trade["costs"][key] for trade in trades), _ZERO)
        for key in ("spread", "slippage", "commission", "swap")
    }

    peak = initial_balance
    max_drawdown = _ZERO
    max_drawdown_pct = 0.0
    equity_values = [initial_balance] + [point["equity"] for point in equity_curve]
    for equity in equity_values:
        peak = max(peak, equity)
        drawdown = peak - equity
        if drawdown > max_drawdown:
            max_drawdown = drawdown
            max_drawdown_pct = float(drawdown / peak) if peak > 0 else 0.0

    returns = [
        float((current - previous) / previous)
        for previous, current in zip(equity_values, equity_values[1:])
        if previous != 0
    ]
    periods_per_year = 252 * 24 * 60 // TIMEFRAME_MINUTES[timeframe]
    sharpe, sortino = _risk_metrics(returns, periods_per_year)
    trade_count = len(trades)
    metrics = {
        "initial_balance": initial_balance,
        "final_balance": balance,
        "net_profit": metrics_net,
        "max_drawdown": max_drawdown,
        "max_drawdown_pct": max_drawdown_pct,
        "profit_factor": _ratio(gross_profit, abs(gross_loss)) if gross_loss else None,
        "win_rate": len(winning) / trade_count if trade_count else 0.0,
        "expectancy": metrics_net / trade_count if trade_count else _ZERO,
        "sharpe": sharpe,
        "sortino": sortino,
        "recovery_factor": _ratio(metrics_net, max_drawdown),
        "exposure": exposed_bars / len(bars),
        "average_holding_minutes": (
            sum((trade["holding_minutes"] for trade in trades), _ZERO) / trade_count
            if trade_count else _ZERO
        ),
        "trade_count": trade_count,
        "winning_trades": len(winning),
        "losing_trades": len(losing),
        "cost_breakdown": total_costs,
    }
    return {
        "assumptions": {
            "entry": "NEXT_BAR_OPEN_FULL_FILL",
            "exit": "SL_TP_OR_FINAL_BAR_CLOSE_FULL_FILL",
            "ambiguous_bar": "STOP_LOSS_FIRST",
            "position_policy": "ONE_POSITION_IGNORE_ENTRIES_WHILE_OPEN",
            "spread_and_slippage": "PRICE_UNITS_PER_SIDE",
            "swap": "PER_LOT_PER_HELD_BAR",
            "drawdown": "EQUITY_AFTER_ESTIMATED_ROUND_TRIP_COSTS",
            "returns_frequency": f"{timeframe}_BAR",
            "annualization_periods": periods_per_year,
            "risk_free_rate": 0.0,
            "latency_bars": 0,
            "partial_fills": False,
            "rejections": False,
        },
        "metrics": metrics,
        "trades": trades,
        "equity_curve": equity_curve,
    }


__all__ = ["BacktestCostModel", "run_backtest"]
