"""Per-account configuration models (doc 12.2 risk_profiles.config_json shape).

Split out of app.py so the risk engine can consume the same validated shape
without an import cycle. `risk` is the cloud-gate threshold section (PRD 9);
`filters`/`expert` are the EA-side execution toggles handed down via
/config/lookup (the EA logs the config and applies nothing yet, PRD 21.3).
"""

from __future__ import annotations

from decimal import Decimal, InvalidOperation
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator


class FiltersConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    news_filter: bool = True
    high_impact_only: bool = True
    session_filter: bool = False
    max_spread: bool = True
    slippage_guard: bool = True
    weekend_block: bool = False
    max_daily_orders: int = Field(default=10, ge=1, le=1000)
    max_positions: int = Field(default=5, ge=1, le=500)


class ExpertConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    auto_trading: bool = True
    sound_alert: bool = False
    popup_alert: bool = True
    notification_alert: bool = False
    close_on_opposite: bool = True
    magic_number: int = Field(default=123456, ge=0)
    order_comment: str = Field(default="QuantPilot", max_length=63)


def _fraction(value: str) -> str:
    try:
        parsed = Decimal(value)
    except InvalidOperation as exc:
        raise ValueError(f"not a decimal: {value}") from exc
    if not Decimal(0) <= parsed <= Decimal(1):
        raise ValueError(f"fraction must be within [0, 1]: {value}")
    return value


def _percent(value: str) -> str:
    try:
        parsed = Decimal(value)
    except InvalidOperation as exc:
        raise ValueError(f"not a decimal: {value}") from exc
    if not Decimal(0) <= parsed <= Decimal(100000):
        raise ValueError(f"percent must be within [0, 100000]: {value}")
    return value


class RiskLimitsConfig(BaseModel):
    """Cloud-gate thresholds (PRD 9.4/9.6).

    Every monetary threshold defaults OFF ("0") per PRD 9.6: the gate rejects
    nothing on limits until the Owner explicitly configures a value.
    require_stop_loss defaults ON because it is protection, not a limit
    (PRD 13.4: never remove protection unintentionally).
    """

    model_config = ConfigDict(extra="forbid")

    # fraction of the basis (risk_basis) a single trade may risk; 0 = off
    per_trade_risk_fraction: str = "0"
    # which basis the fractions apply to - must be explicit (PRD 9.3)
    risk_basis: Literal["EQUITY", "BALANCE"] = "EQUITY"
    # daily realized+floating loss as a fraction of the basis; 0 = off
    max_daily_loss_fraction: str = "0"
    # drawdown from the observed peak equity; 0 = off
    max_drawdown_fraction: str = "0"
    # minimum margin level in percent; 0 = off
    min_margin_level: str = "0"
    # leading consecutive losing closed trades; 0 = off
    max_consecutive_losses: int = Field(default=0, ge=0, le=1000)
    require_stop_loss: bool = True
    # account snapshot freshness gate (PRD 2.5: stale data forbids new risk)
    snapshot_max_age_seconds: int = Field(default=120, ge=1, le=86400)
    # symbol_quotes freshness (EA refreshes specs hourly, doc 4.4 no tick data)
    quote_max_age_seconds: int = Field(default=3700, ge=1, le=86400)

    _frac = field_validator(
        "per_trade_risk_fraction", "max_daily_loss_fraction", "max_drawdown_fraction"
    )(_fraction)
    _pct = field_validator("min_margin_level")(_percent)


class AiConfig(BaseModel):
    """AI run modes (doc 8.5).

    First release supports OFF / ADVISORY only: ADVISORY analyses are display
    artifacts that never change trade permission; OFF is the required
    fallback (AI trouble -> run without AI). FILTER / REGIME_ROUTING come
    after validation, AUTONOMOUS_RESEARCH is out of scope for now.
    """

    model_config = ConfigDict(extra="forbid")

    mode: Literal["OFF", "ADVISORY"] = "ADVISORY"


class AccountConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    filters: FiltersConfig = Field(default_factory=FiltersConfig)
    expert: ExpertConfig = Field(default_factory=ExpertConfig)
    risk: RiskLimitsConfig = Field(default_factory=RiskLimitsConfig)
    ai: AiConfig = Field(default_factory=AiConfig)
