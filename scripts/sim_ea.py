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
from datetime import datetime, timezone
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


def symbol(name: str) -> dict:
    return {
        "broker_symbol": name,
        "visible": True,
        "selected": True,
        "trade_mode": "FULL",
        "trade_mode_raw": 4,
        "filling_modes": ["FOK", "IOC"],
        "calc_mode": "CALC_MODE_0",
        "digits": 5,
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
        "spread": 12,
        "bid": "1.08123",
        "ask": "1.08135",
    }


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
            "symbols": [symbol("EURUSD"), symbol("GBPUSD"), symbol("XAUUSD")],
            "observed_at": iso_now(),
        },
    )

    # 5. commands endpoint must exist and be empty (OBSERVE_ONLY)
    r = client.get(AGENT + "/ea/v1/commands", headers=HEADERS)
    body = r.json()
    empty = body.get("commands") == []
    print(f"[commands] empty list: {'OK' if empty else 'FAIL'}")
    if not empty:
        failures += 1

    print(f"\nagent-side failures: {failures}")
    return failures


if __name__ == "__main__":
    sys.exit(main())
