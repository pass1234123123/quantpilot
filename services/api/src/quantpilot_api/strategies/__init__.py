"""Built-in Python strategy registry (doc 12.2 strategies/strategy_versions).

The migration seeds the catalog rows (code/name/config_schema); this module
binds each code to its Python implementation. Artifact marker 'py-builtin'
in strategy_versions rows corresponds to the classes registered here.
A catalog row with no registered class (unknown artifact) is skipped by the
engine and reported - never silently mis-evaluated (PRD 21.3: explicit
naming of actual behavior).
"""

from __future__ import annotations

from .base import Bar, Strategy, StrategySignal
from .breakout import Breakout, BreakoutConfig
from .ma_cross import MaCrossConfig, MovingAverageCross
from .mean_reversion import MeanReversion, MeanReversionConfig
from .rsi_momentum import RsiMacdMomentum, RsiMomentumConfig
from .trend_following import TrendFollowing, TrendFollowingConfig

# code -> implementation class (code == Strategy.strategy_id, doc 7.4)
REGISTRY: dict[str, type[Strategy]] = {
    "ma_cross": MovingAverageCross,
    "rsi_momentum": RsiMacdMomentum,
    "trend_following": TrendFollowing,
    "breakout": Breakout,
    "mean_reversion": MeanReversion,
}

__all__ = [
    "Bar",
    "REGISTRY",
    "Breakout",
    "BreakoutConfig",
    "MaCrossConfig",
    "MovingAverageCross",
    "MeanReversion",
    "MeanReversionConfig",
    "RsiMacdMomentum",
    "RsiMomentumConfig",
    "Strategy",
    "StrategySignal",
    "TrendFollowing",
    "TrendFollowingConfig",
]
