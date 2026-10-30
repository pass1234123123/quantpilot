"""Pydantic schemas shared by Agent and cloud API.

Design notes (PRD 12/13/A.3/A.4):
- Money / price / volume fields travel as decimal strings in JSON; parsed to
  Decimal here and stored as NUMERIC in PostgreSQL (never float, doc 21.3).
- The EA reports raw local terminal paths ONLY to the local Agent over
  loopback HTTP. The Agent hashes them and publishes hashes to the cloud.
- All event payloads carry the full identity tuple (node/device + terminal
  data-path hash + account login/server) so the cloud consumer can resolve
  rows statelessly without extra round-trips.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone
from decimal import Decimal
from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field, PlainSerializer

from .constants import SCHEMA_VERSION

StrictModel = ConfigDict(extra="forbid", str_strip_whitespace=True)

# Money/price/volume fields: parsed as Decimal (exact), serialized to JSON as
# decimal strings (doc 13.1) and stored in PG as NUMERIC (doc 21.3).
Money = Annotated[Decimal, PlainSerializer(str, return_type=str, when_used="json")]


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def new_event_id() -> str:
    return str(uuid.uuid4())


class EaTerminalIdentity(BaseModel):
    """Local-loopback only fields; Agent hashes paths before cloud publish."""

    model_config = StrictModel

    build: int = Field(description="TerminalInfoInteger(TERMINAL_BUILD)")
    company: str = Field(default="", description="TerminalInfoString(TERMINAL_COMPANY)")
    exe_path: str = Field(default="", description="TerminalInfoString(TERMINAL_PATH)")
    data_path: str = Field(description="TerminalInfoString(TERMINAL_DATA_PATH)")


class EaAccountIdentity(BaseModel):
    model_config = StrictModel

    login_ref: str = Field(description="AccountInfoInteger(ACCOUNT_LOGIN) as string")
    server: str = Field(description="AccountInfoString(ACCOUNT_SERVER)")
    company: str = Field(default="", description="AccountInfoString(ACCOUNT_COMPANY)")
    currency: str = Field(default="", description="AccountInfoString(ACCOUNT_CURRENCY)")
    leverage: int = Field(default=0, description="ACCOUNT_LEVERAGE")
    margin_mode: str = Field(
        default="",
        description="ACCOUNT_MARGIN_MODE as label: EXCHANGE/RETAIL_HEDGING/RETAIL_NETTING",
    )
    account_mode: Literal["DEMO", "REAL", "CONTEST", "UNKNOWN"] = "DEMO"


class EaRegisteredData(BaseModel):
    """Event ea.registered payload."""

    model_config = StrictModel

    ea_version: str
    terminal: EaTerminalIdentity
    account: EaAccountIdentity
    observed_at: datetime


class EaHeartbeatData(BaseModel):
    """Event ea.heartbeat payload (doc A.3 fields + identity for resolution)."""

    model_config = StrictModel

    node_id: str = ""  # filled by Agent for traceability; resolution uses identity
    ea_version: str
    terminal: EaTerminalIdentity
    account: EaAccountIdentity
    connected: bool
    trade_allowed: bool
    expert_allowed: bool
    balance: Money = Field()
    equity: Money = Field()
    margin: Money = Field(default=Decimal("0"))
    free_margin: Money = Field(default=Decimal("0"))
    currency: str
    open_positions: int
    observed_at: datetime


class SymbolSpec(BaseModel):
    """Per doc 5.4 symbol spec fields; numeric values as decimal strings."""

    model_config = StrictModel

    broker_symbol: str
    visible: bool
    # SYMBOL_SELECT: selected in Market Watch (this MT5 build exposes no
    # SYMBOL_SELECTABLE; naming follows the actual MQL5 property, PRD 21.3)
    selected: bool
    trade_mode: str = Field(description="SYMBOL_TRADE_MODE as label")
    trade_mode_raw: int
    filling_modes: list[str] = Field(default_factory=list, description="SYMBOL_FILLING_MODE labels")
    calc_mode: str = Field(description="SYMBOL_TRADE_CALC_MODE as label")
    digits: int
    point: Money
    volume_min: Money
    volume_max: Money
    volume_step: Money
    tick_size: Money
    tick_value_profit: Money
    tick_value_loss: Money
    stops_level: int
    freeze_level: int
    contract_size: Money
    currency_base: str = ""
    currency_profit: str = ""
    currency_margin: str = ""
    spread: int = Field(default=0, description="SYMBOL_SPREAD in points at observation")
    bid: Money | None = None
    ask: Money | None = None


class EaSymbolsData(BaseModel):
    """Event ea.symbols payload: a batch of specs for one terminal/account."""

    model_config = StrictModel

    ea_version: str
    terminal: EaTerminalIdentity
    account: EaAccountIdentity
    batch_index: int = Field(default=0)
    batch_total: int = Field(default=1)
    symbols: list[SymbolSpec]
    observed_at: datetime


class EaShutdownData(BaseModel):
    model_config = StrictModel

    ea_version: str
    terminal: EaTerminalIdentity
    account: EaAccountIdentity
    reason: str = Field(default="unspecified")
    observed_at: datetime


class PositionSnapshot(BaseModel):
    """One open position as observed via PositionGet* (read-only, doc 12.2).

    sl/tp are None when the broker reports 0 (not set). profit is the current
    floating profit, not realized P/L (deals arrive with the order slice).
    """

    model_config = StrictModel

    broker_position_ticket: str = Field(description="PositionGetInteger(POSITION_TICKET) as string")
    broker_symbol: str
    direction: Literal["BUY", "SELL"]
    volume: Money
    price_open: Money
    current_price: Money
    sl: Money | None = None
    tp: Money | None = None
    profit: Money = Field(default=Decimal("0"))
    swap: Money = Field(default=Decimal("0"))
    open_time: datetime


class EaPositionsData(BaseModel):
    """Event ea.positions payload: full open-position snapshot for one account.

    The list is the complete set of open positions at observed_at; the cloud
    consumer marks tickets missing from the snapshot as CLOSED (state-based
    sync, doc 4.4/5.2 reconciliation).
    """

    model_config = StrictModel

    ea_version: str
    terminal: EaTerminalIdentity
    account: EaAccountIdentity
    positions: list[PositionSnapshot]
    observed_at: datetime


class NodeEventEnvelope(BaseModel):
    """Wrapper published to JetStream subject node.<node_id>.events."""

    model_config = StrictModel

    event_id: str = Field(default_factory=new_event_id)
    event_type: str
    schema_version: str = SCHEMA_VERSION
    node_id: str
    occurred_at: datetime = Field(default_factory=utc_now)
    data: dict[str, Any]


class NodeRegisterRequest(BaseModel):
    """POST /api/v1/nodes/register (upsert by device_id)."""

    model_config = StrictModel

    device_id: str = Field(min_length=8)
    hostname: str = ""
    os_info: str = ""
    agent_version: str = "0.1.0"


class NodeRegisterResponse(BaseModel):
    model_config = StrictModel

    node_id: str
    device_id: str
    status: str


class NodeHeartbeatRequest(BaseModel):
    model_config = StrictModel

    node_id: str
