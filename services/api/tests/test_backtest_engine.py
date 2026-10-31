import unittest
from datetime import datetime, timedelta, timezone
from decimal import Decimal

from quantpilot_api.strategies import MovingAverageCross
from quantpilot_api.strategies.base import Strategy, StrategySignal


BASE = datetime(2026, 1, 5, tzinfo=timezone.utc)
D = Decimal


def bar(index: int, open_: str, high: str, low: str, close: str):
    from quantpilot_api.strategies import Bar

    return Bar(
        bar_time=BASE + timedelta(minutes=15 * index),
        open=D(open_),
        high=D(high),
        low=D(low),
        close=D(close),
        tick_volume=100 + index,
    )


class OneSignalStrategy(Strategy):
    strategy_id = "test_one_signal"
    version = "1.0.0"
    ConfigModel = MovingAverageCross.ConfigModel

    def __init__(self, stop_loss: str = "1", take_profit: str = "1000"):
        self.stop_loss = D(stop_loss)
        self.take_profit = D(take_profit)

    @classmethod
    def warmup_bars(cls, config) -> int:
        return 1

    def on_bar(self, bars, config, timeframe):
        if len(bars) != 1:
            return []
        close_at = bars[0].bar_time + timedelta(minutes=15)
        return [
            StrategySignal(
                direction="BUY",
                strength=D("1"),
                reference_price=bars[0].close,
                proposed_stop_loss=self.stop_loss,
                proposed_take_profit=self.take_profit,
                market_data_at=close_at,
                valid_until=close_at + timedelta(minutes=15),
                reason_codes=("TEST_ENTRY",),
                features_ref="test-window",
            )
        ]


class BacktestEngineTests(unittest.TestCase):
    def test_real_ma_cross_fills_at_next_bar_open_not_signal_close(self) -> None:
        from quantpilot_api.backtest_engine import BacktestCostModel, run_backtest

        bars = []
        previous = D("1.2000")
        closes = [previous]
        for _ in range(8):
            previous -= D("0.0010")
            closes.append(previous)
        for _ in range(5):
            previous += D("0.0020")
            closes.append(previous)
        for index, close in enumerate(closes):
            open_ = closes[index - 1] if index else close
            bars.append(bar(index, str(open_), str(max(open_, close) + D("0.0001")),
                            str(min(open_, close) - D("0.0001")), str(close)))

        strategy = MovingAverageCross()
        config = strategy.validate_config({
            "fast_period": 2,
            "slow_period": 3,
            "atr_period": 2,
            "sl_atr_multiple": "100",
            "tp_atr_multiple": "100",
        })
        signal_index = next(
            index for index in range(len(bars) - 1)
            if strategy.on_bar(bars[max(0, index - 16):index + 1], config, "M15")
        )
        bars[signal_index + 1] = bar(
            signal_index + 1,
            str(bars[signal_index].close + D("0.0050")),
            str(bars[signal_index].close + D("0.0060")),
            str(bars[signal_index].close - D("0.0001")),
            bars[signal_index + 1].close,
        )

        report = run_backtest(
            bars=bars,
            strategy=strategy,
            config=config,
            timeframe="M15",
            initial_balance=D("10000"),
            volume=D("1"),
            cost_model=BacktestCostModel(contract_size=D("100000")),
        )

        trade = report["trades"][0]
        self.assertEqual(trade["source_bar_time"], bars[signal_index].bar_time)
        self.assertEqual(trade["entry_time"], bars[signal_index + 1].bar_time)
        self.assertEqual(trade["entry_price"], bars[signal_index + 1].open)
        self.assertNotEqual(trade["entry_price"], bars[signal_index].close)

    def test_cost_breakdown_and_net_profit_are_exact_decimals(self) -> None:
        from quantpilot_api.backtest_engine import BacktestCostModel, run_backtest

        bars = [
            bar(0, "10", "10", "10", "10"),
            bar(1, "10", "11", "9.5", "10.5"),
            bar(2, "11", "12.5", "10.5", "12"),
        ]
        costs = BacktestCostModel(
            spread_per_side=D("0.01"),
            slippage_per_side=D("0.02"),
            commission_per_lot_per_side=D("3"),
            swap_per_lot_per_bar=D("1"),
            contract_size=D("100"),
            quote_to_account_rate=D("1"),
        )

        report = run_backtest(
            bars=bars,
            strategy=OneSignalStrategy(),
            config={},
            timeframe="M15",
            initial_balance=D("10000"),
            volume=D("2"),
            cost_model=costs,
        )

        trade = report["trades"][0]
        self.assertEqual(trade["entry_price"], D("10.03"))
        self.assertEqual(trade["exit_price"], D("11.97"))
        self.assertEqual(trade["gross_profit"], D("400"))
        self.assertEqual(trade["costs"], {
            "spread": D("4"), "slippage": D("8"),
            "commission": D("12"), "swap": D("4"),
        })
        self.assertEqual(trade["net_profit"], D("372"))
        self.assertEqual(report["metrics"]["net_profit"], D("372"))
        self.assertEqual(report["metrics"]["final_balance"], D("10372"))
        self.assertEqual(report["metrics"]["expectancy"], D("372"))
        self.assertEqual(report["metrics"]["cost_breakdown"], trade["costs"])

    def test_same_bar_stop_and_target_uses_conservative_stop_first(self) -> None:
        from quantpilot_api.backtest_engine import BacktestCostModel, run_backtest

        bars = [
            bar(0, "10", "10", "10", "10"),
            bar(1, "10", "12", "8", "10"),
            bar(2, "10", "10", "10", "10"),
        ]
        report = run_backtest(
            bars=bars,
            strategy=OneSignalStrategy(stop_loss="9", take_profit="11"),
            config={},
            timeframe="M15",
            initial_balance=D("1000"),
            volume=D("1"),
            cost_model=BacktestCostModel(contract_size=D("100")),
        )

        trade = report["trades"][0]
        self.assertEqual(trade["exit_reason"], "STOP_LOSS")
        self.assertEqual(trade["exit_price"], D("9"))
        self.assertEqual(trade["net_profit"], D("-100"))
        self.assertEqual(report["metrics"]["max_drawdown"], D("100"))
        self.assertEqual(report["metrics"]["profit_factor"], D("0"))
        self.assertEqual(report["metrics"]["win_rate"], 0.0)

    def test_replay_is_byte_for_byte_deterministic_and_reports_assumptions(self) -> None:
        from quantpilot_api.backtest_engine import BacktestCostModel, run_backtest

        bars = [
            bar(0, "10", "10", "10", "10"),
            bar(1, "10", "10.5", "9.5", "10.2"),
            bar(2, "10.2", "11", "10", "10.8"),
        ]
        kwargs = {
            "bars": bars,
            "strategy": OneSignalStrategy(),
            "config": {},
            "timeframe": "M15",
            "initial_balance": D("1000"),
            "volume": D("1"),
            "cost_model": BacktestCostModel(contract_size=D("100")),
        }

        first = run_backtest(**kwargs)
        replay = run_backtest(**kwargs)

        self.assertEqual(first, replay)
        self.assertEqual(first["assumptions"]["entry"], "NEXT_BAR_OPEN_FULL_FILL")
        self.assertEqual(first["assumptions"]["ambiguous_bar"], "STOP_LOSS_FIRST")
        self.assertEqual(first["metrics"]["trade_count"], 1)
        self.assertEqual(first["metrics"]["exposure"], 2 / 3)
        self.assertEqual(first["metrics"]["average_holding_minutes"], D("30"))
        self.assertIn("sharpe", first["metrics"])
        self.assertIn("sortino", first["metrics"])
        self.assertIn("recovery_factor", first["metrics"])
        self.assertEqual(len(first["equity_curve"]), len(bars))


if __name__ == "__main__":
    unittest.main()
