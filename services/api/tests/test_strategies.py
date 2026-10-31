"""Unit tests for the two new built-in strategies (PRD 7.3 #4/#5) and the
Bollinger indicators they add: breakout (range high/low + ATR volatility
compression filter) and mean_reversion (Bollinger Z-score stretch + reversion
confirmation + ADX trend gate)."""

import unittest
from datetime import datetime, timedelta, timezone
from decimal import Decimal

D = Decimal
BASE = datetime(2026, 1, 5, tzinfo=timezone.utc)


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


def flat(index: int, close: str = "100", high: str = "100.1", low: str = "99.9"):
    return bar(index, close, high, low, close)


def flats(count: int, high: str = "100.1", low: str = "99.9"):
    return [flat(i, high=high, low=low) for i in range(count)]


class IndicatorMathTests(unittest.TestCase):
    def test_stddev_rolling_population_values(self) -> None:
        from quantpilot_api.strategies.indicators import stddev_series

        # population sigma of [1,3] = 1; rolling pairs of [1,2,3,4] all 0.5
        self.assertEqual(stddev_series([D("1"), D("3")], 2), [None, D("1")])
        self.assertEqual(
            stddev_series([D(x) for x in ("1", "2", "3", "4")], 2),
            [None, D("0.5"), D("0.5"), D("0.5")],
        )
        self.assertEqual(
            stddev_series([D("2")] * 4, 2), [None, D("0"), D("0"), D("0")]
        )

    def test_stddev_short_window_is_all_none(self) -> None:
        from quantpilot_api.strategies.indicators import stddev_series

        self.assertEqual(stddev_series([D("1")], 3), [None])
        self.assertEqual(stddev_series([], 2), [])

    def test_bollinger_bands_exact_values(self) -> None:
        from quantpilot_api.strategies.indicators import bollinger_series

        middle, upper, lower = bollinger_series([D("1"), D("3")], 2, D("2"))
        self.assertEqual(middle, [None, D("2")])
        self.assertEqual(upper, [None, D("4")])
        self.assertEqual(lower, [None, D("0")])

        middle, upper, lower = bollinger_series([D("10"), D("12")], 2, D("2.5"))
        self.assertEqual(upper, [None, D("13.5")])
        self.assertEqual(lower, [None, D("8.5")])

    def test_bollinger_insufficient_window_is_all_none(self) -> None:
        from quantpilot_api.strategies.indicators import bollinger_series

        middle, upper, lower = bollinger_series([D("1"), D("2")], 5, D("2"))
        self.assertEqual(middle, [None, None])
        self.assertEqual(upper, [None, None])
        self.assertEqual(lower, [None, None])


class BreakoutTests(unittest.TestCase):
    """Close through the prior-N-bar range after volatility compression."""

    @staticmethod
    def config(**overrides) -> dict:
        config = {"lookback_period": 6, "atr_period": 5}
        config.update(overrides)
        return config

    @staticmethod
    def breakout():
        from quantpilot_api.strategies import Breakout

        return Breakout()

    def test_warmup_formula(self) -> None:
        from quantpilot_api.strategies import Breakout

        cfg = Breakout.validate_config(self.config())
        self.assertEqual(Breakout.warmup_bars(cfg), 6 + 5 + 3)

    def test_buy_on_close_breakout_of_compressed_range(self) -> None:
        bars = flats(13) + [bar(13, "100", "100.6", "99.9", "100.5")]
        signals = self.breakout().on_bar(bars, self.config(), "M15")

        self.assertEqual(len(signals), 1)
        signal = signals[0]
        self.assertEqual(signal.direction, "BUY")
        self.assertEqual(signal.reference_price, D("100.5"))
        # ATR at the trigger bar = ((0.2 * 4) + 0.7) / 5 = 0.3 exactly
        self.assertEqual(signal.proposed_stop_loss, D("99.9"))
        self.assertEqual(signal.proposed_take_profit, D("101.4"))
        self.assertEqual(signal.reason_codes,
                         ("RANGE_BREAKOUT_UP", "VOLATILITY_COMPRESSION"))
        self.assertEqual(signal.strength, D("1"))
        trigger_close = bars[-1].bar_time + timedelta(minutes=15)
        self.assertEqual(signal.market_data_at, trigger_close)
        self.assertEqual(signal.valid_until, trigger_close + timedelta(minutes=15))
        self.assertTrue(signal.features_ref.startswith("fswin-"))

    def test_short_window_emits_nothing(self) -> None:
        bars = flats(3)
        self.assertEqual(self.breakout().on_bar(bars, self.config(), "M15"), [])

    def test_close_inside_range_emits_nothing(self) -> None:
        bars = flats(13) + [bar(13, "100", "100.2", "99.9", "100.05")]
        self.assertEqual(self.breakout().on_bar(bars, self.config(), "M15"), [])

    def test_volatility_compression_filter_blocks_wide_range(self) -> None:
        bars = flats(13) + [bar(13, "100", "100.6", "99.9", "100.5")]
        config = self.config(squeeze_width_max="0.5")  # width 0.2 > 0.5 * ATR 0.2
        self.assertEqual(self.breakout().on_bar(bars, config, "M15"), [])

    def test_no_repeat_signal_while_breakout_persists(self) -> None:
        bars = flats(13) + [
            bar(13, "100", "100.6", "99.9", "100.5"),
            bar(14, "100.5", "101.1", "99.9", "101.0"),
        ]
        # precondition: the first breakout bar fires exactly once ...
        self.assertEqual(len(self.breakout().on_bar(bars[:14], self.config(), "M15")), 1)
        # ... and the follow-through bar is a carried-over state, not a new edge
        self.assertEqual(self.breakout().on_bar(bars, self.config(), "M15"), [])

    def test_sell_mirror_on_downside_breakout(self) -> None:
        bars = flats(13) + [bar(13, "100", "100", "99.3", "99.4")]
        signals = self.breakout().on_bar(bars, self.config(), "M15")

        self.assertEqual(len(signals), 1)
        signal = signals[0]
        self.assertEqual(signal.direction, "SELL")
        self.assertEqual(signal.reference_price, D("99.4"))
        self.assertEqual(signal.proposed_stop_loss, D("100.0"))
        self.assertEqual(signal.proposed_take_profit, D("98.5"))
        self.assertEqual(signal.reason_codes,
                         ("RANGE_BREAKOUT_DOWN", "VOLATILITY_COMPRESSION"))
        self.assertEqual(signal.strength, D("1"))

    def test_config_rejects_unknown_fields_and_bounds(self) -> None:
        from pydantic import ValidationError

        from quantpilot_api.strategies import Breakout

        with self.assertRaises(ValidationError):
            Breakout.validate_config(self.config(bogus=1))
        with self.assertRaises(ValidationError):
            Breakout.validate_config(self.config(lookback_period=4))

    def test_json_serialization_uses_decimal_strings(self) -> None:
        from quantpilot_api.strategies import Breakout

        dumped = Breakout.validate_config(self.config()).model_dump(mode="json")
        self.assertEqual(dumped["squeeze_width_max"], "4")
        self.assertEqual(dumped["sl_atr_multiple"], "2")


class MeanReversionTests(unittest.TestCase):
    """Bollinger Z-score stretch that starts to revert, gated out of trends."""

    @staticmethod
    def config(**overrides) -> dict:
        config = {
            "bollinger_period": 10,
            "adx_period": 14,
            "atr_period": 5,
            "entry_z": "1.2",
        }
        config.update(overrides)
        return config

    @staticmethod
    def mean_reversion():
        from quantpilot_api.strategies import MeanReversion

        return MeanReversion()

    @staticmethod
    def z_scores(closes: list[str], period: int) -> list:
        from quantpilot_api.strategies.indicators import bollinger_series

        values = [D(value) for value in closes]
        middle, upper, _ = bollinger_series(values, period, D("2"))
        out = []
        for i, close in enumerate(values):
            if middle[i] is None:
                out.append(None)
                continue
            std = (upper[i] - middle[i]) / D("2")
            out.append(D("0") if std == 0 else (close - middle[i]) / std)
        return out

    def test_warmup_formula(self) -> None:
        from quantpilot_api.strategies import MeanReversion

        cfg = MeanReversion.validate_config(self.config())
        self.assertEqual(MeanReversion.warmup_bars(cfg), 10 + 2 * 14 + 5 + 3)

    def test_sell_on_stretch_then_reversion(self) -> None:
        # 46 flat bars seed sigma~0 and ADX~0; close 130 is a 3-sigma stretch;
        # close 125 starts reverting while still 1.76-sigma above the mean.
        bars = flats(46, high="100.02", low="99.98") + [
            bar(46, "100", "130.5", "100", "130"),
            bar(47, "130", "131", "124", "125"),
        ]
        config = self.config()

        # while the stretch is still extending (z rising) there is no entry
        self.assertEqual(self.mean_reversion().on_bar(bars[:-1], config, "M15"), [])

        signals = self.mean_reversion().on_bar(bars, config, "M15")
        self.assertEqual(len(signals), 1)
        signal = signals[0]
        self.assertEqual(signal.direction, "SELL")
        self.assertEqual(signal.reference_price, D("125"))
        sl_distance = signal.proposed_stop_loss - signal.reference_price
        tp_distance = signal.reference_price - signal.proposed_take_profit
        self.assertTrue(sl_distance > 0 and tp_distance > 0)
        self.assertEqual(sl_distance / 2, tp_distance / 3)
        self.assertEqual(
            signal.reason_codes,
            ("BOLLINGER_DEVIATION", "Z_SCORE_REVERSION", "RANGE_STABILITY"),
        )
        self.assertEqual(signal.strength, D("0.73"))
        trigger_close = bars[-1].bar_time + timedelta(minutes=15)
        self.assertEqual(signal.market_data_at, trigger_close)
        self.assertEqual(signal.valid_until, trigger_close + timedelta(minutes=15))
        self.assertTrue(signal.features_ref.startswith("fswin-"))

    def test_no_repeat_on_second_consecutive_reversion_bar(self) -> None:
        closes = ["100"] * 46 + ["130", "125", "128"]
        bars = [
            bar(46, "100", "130.5", "100", "130"),
            bar(47, "130", "131", "124", "125"),
            bar(48, "125", "129", "123", "128"),
        ]
        full = flats(46, high="100.02", low="99.98") + bars

        # precondition: bar 48 is still stretched (z >= entry) and still
        # reverting (z falling) - the state simply carries over
        z = self.z_scores(closes, 10)
        self.assertIsNotNone(z[47])
        self.assertIsNotNone(z[48])
        self.assertGreaterEqual(z[48], D("1.2"))
        self.assertLess(z[48], z[47])
        # the ADX gate must NOT be the reason for the silence below
        from quantpilot_api.strategies.indicators import adx_series

        self.assertLess(adx_series(full, 14)[48], D("25"))

        config = self.config()
        self.assertEqual(len(self.mean_reversion().on_bar(full[:-1], config, "M15")), 1)
        self.assertEqual(self.mean_reversion().on_bar(full, config, "M15"), [])

    def test_buy_mirror_below_lower_band(self) -> None:
        bars = flats(46, high="100.02", low="99.98") + [
            bar(46, "100", "100.02", "69.5", "70"),
            bar(47, "70", "100.02", "74", "75"),
        ]
        signals = self.mean_reversion().on_bar(bars, self.config(), "M15")

        self.assertEqual(len(signals), 1)
        signal = signals[0]
        self.assertEqual(signal.direction, "BUY")
        self.assertEqual(signal.reference_price, D("75"))
        sl_distance = signal.reference_price - signal.proposed_stop_loss
        tp_distance = signal.proposed_take_profit - signal.reference_price
        self.assertTrue(sl_distance > 0 and tp_distance > 0)
        self.assertEqual(sl_distance / 2, tp_distance / 3)
        self.assertEqual(
            signal.reason_codes,
            ("BOLLINGER_DEVIATION", "Z_SCORE_REVERSION", "RANGE_STABILITY"),
        )

    def test_adx_gate_blocks_entry_in_strong_trend(self) -> None:
        # 46-bar monotonic ramp (ADX ~100) then a flat pullback bar: the
        # stretch/revert pattern holds but doc 7.3 forbids fading strong
        # trends ("may stay counter-trend for extended periods").
        closes = [str(100 + i) for i in range(46)] + ["146", "146"]
        bars = []
        for i, close in enumerate(closes):
            open_ = closes[i - 1] if i else close
            high = str(max(D(open_), D(close)) + D("0.01"))
            low = str(min(D(open_), D(close)) - D("0.01"))
            bars.append(bar(i, open_, high, low, close))

        z = self.z_scores(closes, 10)
        self.assertGreaterEqual(z[47], D("1"))
        self.assertLess(z[47], z[46])
        from quantpilot_api.strategies.indicators import adx_series

        self.assertGreaterEqual(adx_series(bars, 14)[47], D("25"))

        signals = self.mean_reversion().on_bar(bars, self.config(entry_z="1"), "M15")
        self.assertEqual(signals, [])

    def test_unreachable_entry_threshold_emits_nothing(self) -> None:
        bars = flats(46, high="100.02", low="99.98") + [
            bar(46, "100", "130.5", "100", "130"),
            bar(47, "130", "131", "124", "125"),
        ]
        config = self.config(entry_z="5")  # max |z| on this path is 3.0
        self.assertEqual(self.mean_reversion().on_bar(bars, config, "M15"), [])

    def test_config_rejects_unknown_fields_and_bounds(self) -> None:
        from pydantic import ValidationError

        from quantpilot_api.strategies import MeanReversion

        with self.assertRaises(ValidationError):
            MeanReversion.validate_config(self.config(bogus=1))
        with self.assertRaises(ValidationError):
            MeanReversion.validate_config(self.config(entry_z="0"))
        with self.assertRaises(ValidationError):
            MeanReversion.validate_config(self.config(adx_threshold=9))

    def test_json_serialization_uses_decimal_strings(self) -> None:
        from quantpilot_api.strategies import MeanReversion

        dumped = MeanReversion.validate_config(self.config()).model_dump(mode="json")
        self.assertEqual(dumped["entry_z"], "1.2")
        self.assertEqual(dumped["std_multiple"], "2")


class RegistryTests(unittest.TestCase):
    def test_registry_binds_all_five_builtins(self) -> None:
        from quantpilot_api.strategies import REGISTRY

        self.assertEqual(
            set(REGISTRY),
            {"ma_cross", "rsi_momentum", "trend_following",
             "breakout", "mean_reversion"},
        )
        for code, cls in REGISTRY.items():
            self.assertEqual(cls.strategy_id, code)
            self.assertEqual(cls.version, "1.0.0")


if __name__ == "__main__":
    unittest.main()
