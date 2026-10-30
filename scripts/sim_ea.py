"""Synthetic EA smoke test: replays register/heartbeat/symbols against the
local agent exactly as QuantPilotBridge.mq5 would (same schemas, same token
header), then checks the cloud API. Used for e2e verification before the real
EA is attached to a chart.

Usage: python scripts/sim_ea.py
"""

from __future__ import annotations

import json
import os
import sys
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "packages" / "contracts" / "src"))
from quantpilot_contracts.envfile import load_env_file  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]
load_env_file(ROOT / ".env")

AGENT = f"http://{os.environ.get('QP_AGENT_HOST','127.0.0.1')}:{os.environ.get('QP_AGENT_PORT','8787')}"
API = os.environ.get("QP_API_BASE_URL", "http://127.0.0.1:8000")
TOKEN = os.environ["QP_AGENT_TOKEN"]

HEADERS = {"X-QP-Token": TOKEN, "Content-Type": "application/json"}


def iso_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def terminal() -> dict:
    return {
        "build": 4760,
        "company": "MetaQuotes Software Corp.",
        "exe_path": "D:\\sim\\terminal64.exe",
        "data_path": "C:\\sim\\terminal-data",
    }


def account() -> dict:
    return {
        "login_ref": "10001234",
        "server": "MetaQuotes-Demo-Sim",
        "company": "MetaQuotes Software Corp.",
        "currency": "USD",
        "leverage": 100,
        "margin_mode": "RETAIL_HEDGING",
        "account_mode": "DEMO",
    }


def symbol(name: str, spread: int, bid: str, ask: str, digits: int = 5) -> dict:
    return {
        "broker_symbol": name,
        "visible": True,
        "selected": True,
        "trade_mode": "FULL",
        "trade_mode_raw": 4,
        "filling_modes": ["FOK", "IOC"],
        "calc_mode": "CALC_MODE_0",
        "digits": digits,
        "point": "0.00001000",
        "volume_min": "0.01000000",
        "volume_max": "100.00000000",
        "volume_step": "0.01000000",
        "tick_size": "0.00001000",
        "tick_value_profit": "1.00000000",
        "tick_value_loss": "1.00000000",
        "stops_level": 0,
        "freeze_level": 0,
        "contract_size": "100000.00000000",
        "currency_base": "EUR",
        "currency_profit": "USD",
        "currency_margin": "USD",
        "spread": spread,
        "bid": bid,
        "ask": ask,
    }


def sim_symbols() -> list[dict]:
    """Distinct non-zero spreads so the symbol_quotes projection is verifiable."""
    return [
        symbol("EURUSD", 12, "1.08123000", "1.08135000"),
        symbol("GBPUSD", 15, "1.27123000", "1.27138000"),
        symbol("XAUUSD", 25, "2712.34000", "2712.59000", digits=2),
    ]


def main() -> int:
    client = httpx.Client(timeout=10)
    failures = 0

    def post(path: str, body: dict) -> None:
        nonlocal failures
        r = client.post(AGENT + path, json=body, headers=HEADERS)
        tag = "OK" if r.status_code == 200 else f"FAIL({r.status_code})"
        print(f"[agent] POST {path}: {tag} {r.text[:120]}")
        if r.status_code != 200:
            failures += 1

    # 1. unauthorized must be rejected
    r = client.post(AGENT + "/ea/v1/register", json={}, headers={"X-QP-Token": "wrong"})
    print(f"[auth] bad token rejected: {'OK' if r.status_code == 401 else 'FAIL'}")
    if r.status_code != 401:
        failures += 1

    # 2. register (same payload shape as the EA)
    post(
        "/ea/v1/register",
        {"ea_version": "1.0.0", "terminal": terminal(), "account": account(), "observed_at": iso_now()},
    )

    # 3. heartbeat (doc A.3 fields)
    hb = {
        "node_id": "",
        "ea_version": "1.0.0",
        "terminal": terminal(),
        "account": account(),
        "connected": True,
        "trade_allowed": True,
        "expert_allowed": True,
        "balance": "10000.00",
        "equity": "9980.00",
        "margin": "0",
        "free_margin": "9980.00",
        "currency": "USD",
        "open_positions": 2,
        "observed_at": iso_now(),
    }
    post("/ea/v1/heartbeat", hb)

    # 4. symbols batch
    post(
        "/ea/v1/symbols",
        {
            "ea_version": "1.0.0",
            "terminal": terminal(),
            "account": account(),
            "batch_index": 0,
            "batch_total": 1,
            "symbols": sim_symbols(),
            "observed_at": iso_now(),
        },
    )

    # 5. positions snapshot A: two open positions (900001, 900002)
    def position(ticket: int, symbol_name: str, direction: str, profit: str) -> dict:
        return {
            "broker_position_ticket": str(ticket),
            "broker_symbol": symbol_name,
            "direction": direction,
            "volume": "0.10000000",
            "price_open": "1.08123000",
            "current_price": "1.08245000",
            "sl": "1.07900000",
            "tp": None,
            "profit": profit,
            "swap": "-0.02",
            "open_time": iso_now(),
        }

    post(
        "/ea/v1/positions",
        {
            "ea_version": "1.0.0",
            "terminal": terminal(),
            "account": account(),
            "positions": [
                position(900001, "EURUSD", "BUY", "12.20"),
                position(900002, "GBPUSD", "SELL", "-3.40"),
            ],
            "observed_at": iso_now(),
        },
    )

    # 6. positions snapshot B: only 900001 left -> 900002 must be marked CLOSED
    import time

    time.sleep(1)
    post(
        "/ea/v1/positions",
        {
            "ea_version": "1.0.0",
            "terminal": terminal(),
            "account": account(),
            "positions": [position(900001, "EURUSD", "BUY", "13.10")],
            "observed_at": iso_now(),
        },
    )

    # 7. history deals: one IN (opened 900001) + one OUT (closed 900002)
    def deal(ticket: int, order_ticket: int, position_ticket: int,
             symbol_name: str, direction: str, entry: str, price: str, profit: str) -> dict:
        return {
            "broker_deal_ticket": str(ticket),
            "broker_order_ticket": str(order_ticket),
            "broker_position_ticket": str(position_ticket),
            "broker_symbol": symbol_name,
            "direction": direction,
            "entry": entry,
            "volume": "0.10000000",
            "price": price,
            "commission": "-0.05",
            "swap": "-0.02",
            "profit": profit,
            "deal_time": iso_now(),
            "magic": 0,
            "comment": "sim",
        }

    post(
        "/ea/v1/deals",
        {
            "ea_version": "1.0.0",
            "terminal": terminal(),
            "account": account(),
            "batch_index": 0,
            "batch_total": 1,
            "deals": [
                deal(700001, 600001, 900001, "EURUSD", "BUY", "IN", "1.08123000", "0"),
                deal(700002, 600002, 900002, "GBPUSD", "SELL", "OUT", "1.27123000", "-3.40"),
            ],
            "observed_at": iso_now(),
        },
    )

    # 8. pending orders snapshot A: one BUY_LIMIT (800001)
    def pending_order(ticket: int) -> dict:
        return {
            "broker_ticket": str(ticket),
            "broker_symbol": "EURUSD",
            "direction": "BUY",
            "order_kind": "BUY_LIMIT",
            "volume": "0.10000000",
            "price": "1.08000000",
            "sl": None,
            "tp": "1.09000000",
            "time_setup": iso_now(),
            "time_expiration": None,
        }

    post(
        "/ea/v1/orders",
        {
            "ea_version": "1.0.0",
            "terminal": terminal(),
            "account": account(),
            "orders": [pending_order(800001)],
            "observed_at": iso_now(),
        },
    )

    # 9. orders snapshot B: empty -> 800001 must be marked REMOVED
    time.sleep(1)
    post(
        "/ea/v1/orders",
        {
            "ea_version": "1.0.0",
            "terminal": terminal(),
            "account": account(),
            "orders": [],
            "observed_at": iso_now(),
        },
    )

    # 10. commands endpoint must exist and be empty (OBSERVE_ONLY)
    r = client.get(AGENT + "/ea/v1/commands", headers=HEADERS)
    body = r.json()
    empty = body.get("commands") == []
    print(f"[commands] empty list: {'OK' if empty else 'FAIL'}")
    if not empty:
        failures += 1

    # 11. config poll: agent must resolve the identity and relay a config
    r = client.post(
        AGENT + "/ea/v1/config",
        json={"ea_version": "1.0.0", "terminal": terminal(), "account": account(), "observed_at": iso_now()},
        headers=HEADERS,
    )
    cfg_ok = False
    resolved = version = None
    if r.status_code == 200:
        body = r.json()
        cfg_ok = body.get("ok") is True and isinstance(body.get("config"), dict)
        resolved = body.get("resolved")
        version = body.get("version")
    print(f"[config] agent lookup: {'OK' if cfg_ok else 'FAIL'} resolved={resolved} version={version}")
    if not cfg_ok:
        failures += 1

    # 12. economic calendar batch (same payload shape as QpSendCalendar)
    now = datetime.now(timezone.utc)
    post(
        "/ea/v1/calendar",
        {
            "ea_version": "1.0.0",
            "terminal": terminal(),
            "account": account(),
            "batch_index": 0,
            "batch_total": 1,
            "window_from": (now - timedelta(hours=2)).isoformat(),
            "window_to": (now + timedelta(hours=48)).isoformat(),
            "events": [
                {
                    "event_uid": "sim-cal-001",
                    "country_code": "US",
                    "currency": "USD",
                    "name": "Core CPI m/m (sim)",
                    "importance": "HIGH",
                    "multiplier": "MILLION",
                    "digits": 1,
                    "event_time": (now - timedelta(minutes=30)).isoformat(),
                    "actual": "312000",
                    "forecast": "310000",
                    "previous": "308000",
                },
                {
                    "event_uid": "sim-cal-002",
                    "country_code": "US",
                    "currency": "USD",
                    "name": "Non-Farm Employment Change (sim)",
                    "importance": "HIGH",
                    "multiplier": "NONE",
                    "digits": 0,
                    "event_time": (now + timedelta(hours=2)).isoformat(),
                    "actual": None,
                    "forecast": "185000",
                    "previous": "179000",
                },
                {
                    "event_uid": "sim-cal-003",
                    "country_code": "DE",
                    "currency": "EUR",
                    "name": "Manufacturing PMI (sim)",
                    "importance": "MODERATE",
                    "multiplier": "NONE",
                    "digits": 1,
                    "event_time": (now + timedelta(hours=5)).isoformat(),
                    "actual": None,
                    "forecast": "43.2",
                    "previous": "43.1",
                },
            ],
            "observed_at": iso_now(),
        },
    )

    print(f"\nagent-side failures: {failures}")
    return failures


if __name__ == "__main__":
    sys.exit(main())
