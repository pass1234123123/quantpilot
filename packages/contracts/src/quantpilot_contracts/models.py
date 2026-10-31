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
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, PlainSerializer, field_validator, model_validator

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


class DealRecord(BaseModel):
    """One closed-trade deal as observed via HistoryDealGet (read-only).

    Balance/credit operations are NOT deals here - the EA filters to
    DEAL_TYPE_BUY/SELL only, so every record is a market fill.
    """

    model_config = StrictModel

    broker_deal_ticket: str = Field(description="HistoryDealGetTicket as string")
    broker_order_ticket: str = Field(default="", description="DEAL_ORDER as string")
    broker_position_ticket: str = Field(default="", description="DEAL_POSITION_ID as string")
    broker_symbol: str
    direction: Literal["BUY", "SELL"]
    entry: Literal["IN", "OUT", "INOUT", "OUT_BY"] = Field(description="DEAL_ENTRY label")
    volume: Money
    price: Money
    commission: Money = Field(default=Decimal("0"))
    swap: Money = Field(default=Decimal("0"))
    profit: Money = Field(default=Decimal("0"))
    deal_time: datetime
    magic: int = 0
    comment: str = ""


class EaDealsData(BaseModel):
    """Event ea.deals payload: incremental history-deal batches for one account.

    Deals are immutable broker facts; the consumer upserts idempotently on
    (account_id, broker_deal_ticket), so overlapping windows and JetStream
    redeliveries are safe (doc 4.4/4.5).
    """

    model_config = StrictModel

    ea_version: str
    terminal: EaTerminalIdentity
    account: EaAccountIdentity
    batch_index: int = 0
    batch_total: int = 1
    deals: list[DealRecord]
    observed_at: datetime


class OrderRecord(BaseModel):
    """One pending order as observed via OrderGet* (read-only, doc 12.2)."""

    model_config = StrictModel

    broker_ticket: str = Field(description="OrderGetTicket as string")
    broker_symbol: str
    direction: Literal["BUY", "SELL"]
    order_kind: str = Field(description="ORDER_TYPE label: BUY_LIMIT/SELL_STOP/...")
    volume: Money
    price: Money
    sl: Money | None = None
    tp: Money | None = None
    time_setup: datetime
    time_expiration: datetime | None = None


class EaOrdersData(BaseModel):
    """Event ea.orders payload: full pending-order snapshot for one account.

    Same state-based sync contract as EaPositionsData: tickets missing from
    the snapshot are marked REMOVED by the cloud consumer.
    """

    model_config = StrictModel

    ea_version: str
    terminal: EaTerminalIdentity
    account: EaAccountIdentity
    orders: list[OrderRecord]
    observed_at: datetime


class CalendarEventRecord(BaseModel):
    """One economic-calendar event from the MT5 built-in calendar (read-only).

    Values are the broker-reported raw longs (MQL5 MqlCalendarValue); the
    multiplier/digits needed to format them travel alongside (MqlCalendarEvent).
    Unset values are omitted by the EA (the terminal marks them with extreme
    long sentinels, which never pass the sanity bound).
    """

    model_config = StrictModel

    event_uid: str = Field(description="MqlCalendarValue.id as string (stable per occurrence)")
    country_code: str = Field(default="", description="MqlCalendarCountry.code")
    currency: str = Field(default="", description="affected currency (country currency)")
    name: str
    importance: Literal["NONE", "LOW", "MODERATE", "HIGH"] = "NONE"
    multiplier: str = Field(default="NONE", description="MqlCalendarEvent.multiplier label")
    digits: int = Field(default=0, description="display digits (MqlCalendarEvent.digits)")
    event_time: datetime
    actual: Money | None = None
    forecast: Money | None = None
    previous: Money | None = None


class EaCalendarData(BaseModel):
    """Event ea.calendar payload: economic-calendar window for one account.

    Events come from the MT5 built-in calendar (broker-provided); the cloud
    upserts by (account_id, event_uid), so redeliveries and overlapping
    windows are idempotent. actual values arrive after release, so a
    re-report of the same window updates - not duplicates - the row.
    """

    model_config = StrictModel

    ea_version: str
    terminal: EaTerminalIdentity
    account: EaAccountIdentity
    batch_index: int = 0
    batch_total: int = 1
    window_from: datetime
    window_to: datetime
    events: list[CalendarEventRecord]
    observed_at: datetime


class BarRecord(BaseModel):
    """One CLOSED bar as observed via CopyRates (read-only, PRD 5.1 market
    snapshot / 7.2 strategy input).

    bar_time is the bar OPEN time in broker-server time; the forming bar is
    never included, so every record is final (doc 7.3: avoid unclosed-bar
    jitter). tick_volume is the number of price changes (real volume is 0 on
    most FX brokers).
    """

    model_config = StrictModel

    bar_time: datetime = Field(description="bar open time (closed bars only)")
    open: Money
    high: Money
    low: Money
    close: Money
    tick_volume: int = Field(default=0, ge=0)


class EaBarsData(BaseModel):
    """Event ea.bars payload: closed bars for ONE symbol+timeframe.

    Batches are per symbol+timeframe (unlike ea.symbols which mixes symbols);
    the cloud upserts idempotently on (account, symbol, timeframe, bar_time),
    so history backfill, new-bar updates and JetStream redeliveries are safe.
    Strategy evaluation triggers on the newest closed bar of each batch.
    """

    model_config = StrictModel

    ea_version: str
    terminal: EaTerminalIdentity
    account: EaAccountIdentity
    broker_symbol: str
    timeframe: str = Field(description="PERIOD label: M5/M15/M30/H1/H4")
    batch_index: int = Field(default=0)
    batch_total: int = Field(default=1)
    bars: list[BarRecord]
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


# ==========================================================================
# Trade command channel (PRD 13.3/13.4/13.5, ADR-004) - slice 1
# ==========================================================================
# Slice-1 semantics: the channel is durable + idempotent + reconcilable, but
# the EA ACKS with a free ack_code string: OBSERVE_ONLY_ACK (receipt only,
# the default), DRY_RUN_* (pre-trade checks, no order) or the live-execution
# codes EXECUTED / EXECUTE_FAILED / EXECUTE_REJECTED / LIVE_REFUSED_NOT_DEMO
# (slice 3, DEMO accounts only behind the EA's LiveTrading input; PRD 21.3).
# risk_profile_version / symbol_spec_hash / signature are
# slice-2 (risk-engine gate) fields and intentionally NOT accepted here.


class OpenMarketPayload(BaseModel):
    """OPEN_MARKET payload (PRD 13.3 example)."""

    model_config = StrictModel

    symbol: str = Field(min_length=1)
    side: Literal["BUY", "SELL"]
    volume: Money
    stop_loss: Money | None = None
    take_profit: Money | None = None
    max_deviation_points: int = Field(default=15, ge=0, le=1000)
    magic_number: int = Field(default=0, ge=0)


class PlacePendingPayload(BaseModel):
    """PLACE_PENDING payload (PRD 13.4: check validity/trigger price/specs)."""

    model_config = StrictModel

    symbol: str = Field(min_length=1)
    side: Literal["BUY", "SELL"]
    order_kind: Literal["BUY_LIMIT", "SELL_LIMIT", "BUY_STOP", "SELL_STOP"]
    volume: Money
    price: Money
    stop_loss: Money | None = None
    take_profit: Money | None = None
    magic_number: int = Field(default=0, ge=0)


class ModifyPositionPayload(BaseModel):
    """MODIFY_POSITION payload: ticket required, never remove protection
    unintentionally (PRD 13.4)."""

    model_config = StrictModel

    position_ticket: str = Field(min_length=1)
    sl: Money | None = None
    tp: Money | None = None


class ClosePositionPayload(BaseModel):
    """CLOSE_POSITION payload: ticket required; volume None = full close."""

    model_config = StrictModel

    position_ticket: str = Field(min_length=1)
    volume: Money | None = None


class CancelOrderPayload(BaseModel):
    """CANCEL_ORDER payload: ticket required (PRD 13.4)."""

    model_config = StrictModel

    order_ticket: str = Field(min_length=1)


class EmptyCommandPayload(BaseModel):
    """STOP_NEW_RISK / RECONCILE carry no parameters."""

    model_config = StrictModel


COMMAND_PAYLOAD_MODELS: dict[str, type[BaseModel]] = {
    "OPEN_MARKET": OpenMarketPayload,
    "PLACE_PENDING": PlacePendingPayload,
    "MODIFY_POSITION": ModifyPositionPayload,
    "CLOSE_POSITION": ClosePositionPayload,
    "CANCEL_ORDER": CancelOrderPayload,
    "STOP_NEW_RISK": EmptyCommandPayload,
    "RECONCILE": EmptyCommandPayload,
}

CommandType = Literal[
    "OPEN_MARKET",
    "PLACE_PENDING",
    "MODIFY_POSITION",
    "CLOSE_POSITION",
    "CANCEL_ORDER",
    "STOP_NEW_RISK",
    "RECONCILE",
]


def validate_command_payload(command_type: str, payload: dict[str, Any]) -> dict[str, Any]:
    """Validate a command payload against its per-type model (PRD 13.4).

    Returns the JSON-mode dict (money as decimal strings, PRD 13.1/21.3).
    """
    model = COMMAND_PAYLOAD_MODELS.get(command_type)
    if model is None:
        raise ValueError(f"unsupported command_type: {command_type}")
    return model.model_validate(payload).model_dump(mode="json")


class TradeCommandCreate(BaseModel):
    """POST /api/v1/commands body (PRD 13.3 subset for slice 1).

    command_id is the caller-assigned idempotency key: a repeated POST with
    the same command_id returns the original command state instead of
    creating a second one (AT-005, PRD 2.5/14.1).
    """

    model_config = StrictModel

    command_id: UUID = Field(description="caller-assigned UUID; unique idempotency key")
    idempotency_key: UUID | None = None
    account_id: UUID
    command_type: CommandType
    expires_at: datetime = Field(description="commands are undeliverable after this instant")
    strategy_instance_id: UUID | None = None
    signal_id: UUID | None = None
    created_by: str = Field(default="api", max_length=64)
    payload: dict[str, Any]

    @field_validator("expires_at")
    @classmethod
    def _require_tz_aware(cls, value: datetime) -> datetime:
        # naive datetimes cannot be compared against now(UTC); reject early
        # with a 422 instead of surfacing as a 500 inside the request handler
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("expires_at must be timezone-aware (ISO 8601 with offset)")
        return value

    @model_validator(mode="after")
    def _validate_payload(self) -> "TradeCommandCreate":
        self.payload = validate_command_payload(self.command_type, self.payload)
        return self


class EaCommandPollRequest(BaseModel):
    """POST /ea/v1/commands/poll body (EA -> agent).

    recently_acked echoes command_ids the EA already processed; the cloud
    uses the echo to reconcile DELIVERED/UNKNOWN commands to ACKED without
    resending them (AT-006, PRD 5.5).
    """

    model_config = StrictModel

    ea_version: str
    terminal: EaTerminalIdentity
    account: EaAccountIdentity
    recently_acked: list[UUID] = Field(default_factory=list)
    observed_at: datetime


class EaCommandAckData(BaseModel):
    """POST /ea/v1/commands/ack body (EA -> agent) and ea.command_ack data.

    ack_code stays a free string (<= 64 chars): OBSERVE_ONLY_ACK |
    DUPLICATE | EXPIRED | DRY_RUN_PASSED | DRY_RUN_REJECTED (dry-run
    pre-trade checks, no execution) | EXECUTED | EXECUTE_FAILED |
    EXECUTE_REJECTED | LIVE_REFUSED_NOT_DEMO (slice 3 live execution on
    DEMO accounts only, behind the EA's LiveTrading input; PRD 21.3) |
    EXECUTE_UNRESOLVED (phase-3 R1: the terminal died mid-execution, the
    outcome is uncertain and the command must be reconciled against the
    broker state - never blindly re-executed).

    The structured ticket fields (phase-3 R1) carry the broker execution
    evidence so the cloud can reconcile without parsing ack_message. They
    are optional: v1.7.0 acks without them stay valid.
    """

    model_config = StrictModel

    ea_version: str
    terminal: EaTerminalIdentity
    account: EaAccountIdentity
    command_id: UUID
    ack_code: str = Field(max_length=64)
    ack_message: str = Field(default="", max_length=512)
    observed_at: datetime
    order_ticket: str = Field(default="", max_length=32)
    deal_ticket: str = Field(default="", max_length=32)
    position_ticket: str = Field(default="", max_length=32)
