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
    # trust_env=False: never route 127.0.0.1 through a system proxy
    client = httpx.Client(timeout=10, trust_env=False)
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

    # 10. command channel (slice 1: OBSERVE_ONLY receipts, AT-005/AT-006)
    import uuid

    accounts = client.get(API + "/api/v1/accounts").json()["data"]
    sim_acc = next((a for a in accounts if a.get("login_ref") == "10001234"), None)

    def poll(recently: list[str]) -> dict:
        r = client.post(
            AGENT + "/ea/v1/commands/poll",
            json={
                "ea_version": "1.0.0",
                "terminal": terminal(),
                "account": account(),
                "recently_acked": recently,
                "observed_at": iso_now(),
            },
            headers=HEADERS,
        )
        return r.json() if r.status_code == 200 else {}

    def ack(cid: str, code: str = "OBSERVE_ONLY_ACK",
            msg: str = "received; not executed (OBSERVE_ONLY slice 1)") -> int:
        r = client.post(
            AGENT + "/ea/v1/commands/ack",
            json={
                "ea_version": "1.0.0",
                "terminal": terminal(),
                "account": account(),
                "command_id": cid,
                "ack_code": code,
                "ack_message": msg,
                "observed_at": iso_now(),
            },
            headers=HEADERS,
        )
        return r.status_code

    def command_status(cid: str) -> str:
        r = client.get(f"{API}/api/v1/commands/{cid}")
        return (r.json().get("data") or {}).get("status", f"HTTP{r.status_code}")

    if sim_acc is None:
        print("[commands] FAIL: sim account not registered yet")
        failures += 1
    else:
        account_id = sim_acc["account_id"]

        # 10a. create cmd1 + idempotent replay (AT-005)
        cmd1 = str(uuid.uuid4())
        body1 = {
            "command_id": cmd1,
            "account_id": account_id,
            "command_type": "OPEN_MARKET",
            "expires_at": (datetime.now(timezone.utc) + timedelta(seconds=300)).isoformat(),
            "created_by": "sim_ea",
            "payload": {
                "symbol": "EURUSD",
                "side": "BUY",
                "volume": "0.10",
                "stop_loss": "1.07900",
                "take_profit": "1.09000",
                "max_deviation_points": 20,
                "magic_number": 20260831,
            },
        }
        r = client.post(API + "/api/v1/commands", json=body1)
        first = r.status_code == 200 and r.json()["data"]["created"] is True
        r2 = client.post(API + "/api/v1/commands", json=body1)
        replay = r2.status_code == 200 and r2.json()["data"]["created"] is False
        same_id = r2.json().get("data", {}).get("command_id") == cmd1
        print(f"[commands] cmd1 create={first} replay_no_new={replay} same_id={same_id}")
        if not (first and replay and same_id):
            failures += 1

        # 10b. poll -> one command; second poll -> empty (per-account serial,
        # DELIVERED is never resent, PRD 2.5 / AT-006)
        got = poll([]).get("commands", [])
        delivered = len(got) == 1 and got[0]["command_id"] == cmd1
        second = poll([]).get("commands", [])
        not_resent = len(second) == 0
        print(f"[commands] cmd1 delivered={delivered} not_resent={not_resent}")
        if not (delivered and not_resent):
            failures += 1

        # 10c. ack (OBSERVE_ONLY) + duplicate ack (event consumer is idempotent)
        ack1 = ack(cmd1)
        ack1b = ack(cmd1)
        print(f"[commands] cmd1 ack={ack1} duplicate_ack={ack1b}")
        if ack1 != 200 or ack1b != 200:
            failures += 1

        # 10d. cmd2 short TTL, delivered but never acked -> UNKNOWN (AT-006:
        # timeout != not delivered; the cloud never resends)
        cmd2 = str(uuid.uuid4())
        client.post(API + "/api/v1/commands", json={
            "command_id": cmd2,
            "account_id": account_id,
            "command_type": "CLOSE_POSITION",
            "expires_at": (datetime.now(timezone.utc) + timedelta(seconds=8)).isoformat(),
            "created_by": "sim_ea",
            "payload": {"position_ticket": "900001"},
        })
        time.sleep(2)
        got2 = poll([]).get("commands", [])
        cmd2_delivered = len(got2) == 1 and got2[0]["command_id"] == cmd2
        print(f"[commands] cmd2 delivered={cmd2_delivered} (will NOT be acked)")
        if not cmd2_delivered:
            failures += 1

        # 10e. cmd3 queued but never polled -> EXPIRED (never delivered)
        cmd3 = str(uuid.uuid4())
        client.post(API + "/api/v1/commands", json={
            "command_id": cmd3,
            "account_id": account_id,
            "command_type": "STOP_NEW_RISK",
            "expires_at": (datetime.now(timezone.utc) + timedelta(seconds=10)).isoformat(),
            "created_by": "sim_ea",
            "payload": {},
        })

        # let the ack event flow + the sweeper run (10s tick, 15s UNKNOWN grace)
        print("[commands] waiting for ack flow / sweeper (38s)...")
        time.sleep(38)

        s1 = command_status(cmd1)
        s2 = command_status(cmd2)
        s3 = command_status(cmd3)
        print(f"[commands] statuses: cmd1={s1} cmd2={s2} cmd3={s3}")
        if not (s1 == "ACKED" and s2 == "UNKNOWN" and s3 == "EXPIRED"):
            failures += 1

        # 10f. UNKNOWN reconciled via the recently_acked echo (AT-006)
        rec = poll([cmd2])
        s2b = command_status(cmd2)
        print(f"[commands] cmd2 after echo={s2b} reconciled={rec.get('reconciled')}")
        if s2b != "ACKED":
            failures += 1

        # 10g. risk gate (slice 2, PRD 9): REJECT persists the command in
        # terminal state REJECTED with reason codes; idempotent replay returns
        # the same row; REJECTED commands are never delivered
        def put_config(mutate) -> None:
            nonlocal failures
            doc = client.get(f"{API}/api/v1/accounts/{account_id}/config").json()["data"]["config"]
            mutate(doc)
            r = client.put(f"{API}/api/v1/accounts/{account_id}/config", json=doc)
            if r.status_code != 200:
                failures += 1
                print(f"[risk] config PUT failed: {r.status_code} {r.text[:120]}")

        def reset_config() -> None:
            nonlocal failures
            # defaults (all risk thresholds OFF, PRD 9.6) + a high daily order
            # cap so repeated same-day runs never trip MAX_DAILY_ORDERS
            r = client.put(
                f"{API}/api/v1/accounts/{account_id}/config",
                json={"filters": {"max_daily_orders": 500}},
            )
            if r.status_code != 200:
                failures += 1
                print(f"[risk] config reset failed: {r.status_code}")

        def open_market(cid: str, **over) -> dict:
            payload = {
                "symbol": "EURUSD",
                "side": "BUY",
                "volume": "0.10",
                "stop_loss": "1.07900",
                "take_profit": "1.09000",
                "max_deviation_points": 20,
                "magic_number": 20260831,
            }
            payload.update(over)
            r = client.post(API + "/api/v1/commands", json={
                "command_id": cid,
                "account_id": account_id,
                "command_type": "OPEN_MARKET",
                "expires_at": (datetime.now(timezone.utc) + timedelta(seconds=120)).isoformat(),
                "created_by": "sim_ea",
                "payload": payload,
            })
            return r.json().get("data") or {}

        rejected_ids: list[str] = []

        def gate(name: str, want_decision: str, want_reason: str | None = None, **over) -> str:
            nonlocal failures
            cid = str(uuid.uuid4())
            data = open_market(cid, **over)
            rd = data.get("risk_decision") or {}
            ok_decision = rd.get("decision") == want_decision
            ok_reason = want_reason is None or rd.get("reason_codes") == [want_reason]
            ok_status = want_decision != "REJECT" or data.get("status") == "REJECTED"
            if want_reason is not None:
                rejected_ids.append(cid)
            print(f"[risk] {name}: decision={rd.get('decision')} "
                  f"reasons={rd.get('reason_codes')} status={data.get('status')}")
            if not (ok_decision and ok_reason and ok_status):
                failures += 1
            return cid

        reset_config()
        gate("allow_default", "ALLOW")
        cid_sl = gate("sl_required", "REJECT", "STOP_LOSS_REQUIRED", stop_loss=None)
        gate("spec_unknown", "REJECT", "SYMBOL_SPEC_UNKNOWN", symbol="NOPEXYZ")
        gate("volume_below_min", "REJECT", "VOLUME_BELOW_MIN", volume="0.001")
        gate("volume_not_on_step", "REJECT", "VOLUME_NOT_ON_STEP", volume="0.015")
        gate("invalid_sl", "REJECT", "INVALID_STOP_LOSS", stop_loss="1.09000")

        put_config(lambda d: d["risk"].update(per_trade_risk_fraction="0.001"))
        # EURUSD: |1.08135 - 1.07900| / 0.00001 * 1.0 * 0.10 = 23.5 > 0.001*9980
        gate("per_trade_reject", "REJECT", "PER_TRADE_RISK_EXCEEDED")
        put_config(lambda d: d["risk"].update(per_trade_risk_fraction="0.01"))
        gate("per_trade_allow", "ALLOW")  # budget 99.8 > 23.5
        put_config(lambda d: d["risk"].update(quote_max_age_seconds=1))
        time.sleep(2)
        gate("estimate_unavailable", "REJECT", "RISK_ESTIMATE_UNAVAILABLE")
        reset_config()

        # seed a losing closed deal for daily-loss + consecutive-loss tests
        post(
            "/ea/v1/deals",
            {
                "ea_version": "1.0.0",
                "terminal": terminal(),
                "account": account(),
                "batch_index": 0,
                "batch_total": 1,
                "deals": [
                    deal(700003, 600003, 900003, "EURUSD", "SELL", "OUT", "1.27123000", "-60.00"),
                ],
                "observed_at": iso_now(),
            },
        )
        time.sleep(1)
        put_config(lambda d: d["risk"].update(max_daily_loss_fraction="0.001"))
        # today: realized -63.54 + floating 13.10 -> daily_loss 50.44 > 9.98
        gate("daily_loss", "REJECT", "DAILY_LOSS_EXCEEDED")
        put_config(lambda d: d["risk"].update(max_daily_loss_fraction="0", max_consecutive_losses=2))
        gate("consecutive_losses", "REJECT", "CONSECUTIVE_LOSSES")
        reset_config()

        # account-level stop, LOCKED variant (9.5: manual confirm to resume)
        r = client.post(API + "/api/v1/risk/stop", json={
            "scope": "ACCOUNT", "account_id": account_id,
            "reason": "sim test", "locked": True, "updated_by": "sim_ea",
        })
        stopped = r.status_code == 200 and r.json().get("data", {}).get("status") == "LOCKED"
        gate("account_locked", "REJECT", "ACCOUNT_STOP_NEW_RISK")
        r409 = client.post(API + "/api/v1/risk/resume", json={
            "scope": "ACCOUNT", "account_id": account_id,
        })
        needs_confirm = r409.status_code == 409
        r = client.post(API + "/api/v1/risk/resume", json={
            "scope": "ACCOUNT", "account_id": account_id, "confirm_locked": True,
        })
        resumed = r.status_code == 200 and r.json().get("data", {}).get("status") == "NORMAL"
        print(f"[risk] account_locked set={stopped} resume_409={needs_confirm} resumed={resumed}")
        if not (stopped and needs_confirm and resumed):
            failures += 1
        gate("account_resumed_allow", "ALLOW")

        # global stop + state endpoint
        r = client.post(API + "/api/v1/risk/stop", json={
            "scope": "GLOBAL", "reason": "sim global", "updated_by": "sim_ea",
        })
        gate("global_stop", "REJECT", "GLOBAL_STOP_NEW_RISK")
        state = client.get(API + "/api/v1/risk/state").json().get("data") or {}
        state_ok = (state.get("global") or {}).get("status") == "STOP_NEW_RISK"
        r = client.post(API + "/api/v1/risk/resume", json={"scope": "GLOBAL"})
        gate("global_resumed_allow", "ALLOW")
        print(f"[risk] global state visible={state_ok}")
        if not state_ok:
            failures += 1

        # idempotent replay of a REJECTED command returns the same row
        replay = open_market(cid_sl, stop_loss=None)
        replay_ok = (
            replay.get("created") is False
            and replay.get("status") == "REJECTED"
            and (replay.get("risk_decision") or {}).get("reason_codes") == ["STOP_LOSS_REQUIRED"]
        )
        print(f"[risk] rejected_replay same_row={replay_ok}")
        if not replay_ok:
            failures += 1

        # REJECTED commands are never delivered: drain QUEUED leftovers and
        # make sure none of the rejected ids ever comes back
        delivered: list[str] = []
        for _ in range(12):
            got = poll([]).get("commands", [])
            if not got:
                break
            delivered.append(got[0]["command_id"])
            ack(got[0]["command_id"])
        leaked = [c for c in delivered if c in rejected_ids]
        print(f"[risk] rejected_never_delivered={not leaked} "
              f"(delivered={len(delivered)} rejected={len(rejected_ids)})")
        if leaked:
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
