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
import psycopg

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "packages" / "contracts" / "src"))
from quantpilot_contracts.envfile import load_env_file  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]
load_env_file(ROOT / ".env")

AGENT = f"http://{os.environ.get('QP_AGENT_HOST','127.0.0.1')}:{os.environ.get('QP_AGENT_PORT','8787')}"
API = os.environ.get("QP_API_BASE_URL", "http://127.0.0.1:8000")
TOKEN = os.environ["QP_AGENT_TOKEN"]
PG_DSN = os.environ["QP_PG_DSN"]

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
    # trust_env=False: never route 127.0.0.1 through a system proxy;
    # X-API-Key: cloud API auth (direct /api/v1 calls below)
    client = httpx.Client(
        timeout=10,
        trust_env=False,
        headers={"X-API-Key": os.environ.get("QP_API_KEY", "")},
    )
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

    def poll(recently: list[str], acct: dict | None = None) -> dict:
        r = client.post(
            AGENT + "/ea/v1/commands/poll",
            json={
                "ea_version": "1.0.0",
                "terminal": terminal(),
                "account": acct or account(),
                "recently_acked": recently,
                "observed_at": iso_now(),
            },
            headers=HEADERS,
        )
        return r.json() if r.status_code == 200 else {}

    def ack(cid: str, code: str = "OBSERVE_ONLY_ACK",
            msg: str = "received; not executed (OBSERVE_ONLY slice 1)",
            acct: dict | None = None) -> int:
        r = client.post(
            AGENT + "/ea/v1/commands/ack",
            json={
                "ea_version": "1.0.0",
                "terminal": terminal(),
                "account": acct or account(),
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
        # the same breach must have auto-escalated the durable 9.5 account
        # state to STOP_NEW_RISK (>=100% of the daily loss limit); assert
        # it, then resume so the next gate case starts from NORMAL again
        st = client.get(API + "/api/v1/risk/state",
                        params={"account_id": account_id}).json().get("data") or {}
        auto_stopped = st.get("status") == "STOP_NEW_RISK"
        print(f"[risk] auto_escalation: status={st.get('status')} reason={st.get('reason')}")
        if not auto_stopped:
            failures += 1
        resumed = client.post(API + "/api/v1/risk/resume", json={
            "scope": "ACCOUNT", "account_id": account_id, "updated_by": "sim_ea",
        })
        if resumed.status_code != 200:
            failures += 1
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

    # 13. strategy engine (PRD 7): closed bars -> advisory signals.
    # Deterministic V-path (120 bars down, then up); batch endpoints land
    # exactly on each strategy's edge bar (located empirically with the
    # engine's window semantics): ma_cross BUY at bar 130, trend_following
    # BUY at bar 133. Deterministic bar times make re-runs idempotent.
    def utc_iso(value) -> str:
        return datetime.fromisoformat(str(value)).astimezone(timezone.utc).isoformat()

    accounts = client.get(API + "/api/v1/accounts").json()["data"]
    sim_acc = next((a for a in accounts if a.get("login_ref") == "10001234"), None)
    if sim_acc is None:
        print("[signals] FAIL: sim account not registered")
        failures += 1
    else:
        account_id = sim_acc["account_id"]

        # 13a. bind instances; 409 = already bound from a previous run
        def bind_instance(code: str, timeframe: str) -> None:
            nonlocal failures
            r = client.post(
                f"{API}/api/v1/accounts/{account_id}/strategy-instances",
                json={"strategy_code": code, "broker_symbol": "EURUSD",
                      "timeframe": timeframe, "config": {}},
            )
            ok = r.status_code in (200, 409)
            print(f"[signals] bind {code}/{timeframe}: {'OK' if ok else 'FAIL'} {r.text[:100]}")
            if not ok:
                failures += 1

        bind_instance("ma_cross", "M15")
        bind_instance("trend_following", "M15")
        bind_instance("rsi_momentum", "M30")  # no M30 bars ever: control instance

        instances = client.get(
            f"{API}/api/v1/accounts/{account_id}/strategy-instances"
        ).json()["data"]
        rsi = next((i for i in instances if i["strategy_code"] == "rsi_momentum"), None)
        if rsi is None:
            print("[signals] FAIL: rsi_momentum instance missing")
            failures += 1
        else:
            # status toggle + config update appends a new revision (7.6)
            r1 = client.post(
                f"{API}/api/v1/accounts/{account_id}/strategy-instances/{rsi['instance_id']}/status",
                json={"status": "DISABLED"})
            r2 = client.post(
                f"{API}/api/v1/accounts/{account_id}/strategy-instances/{rsi['instance_id']}/status",
                json={"status": "ENABLED"})
            r3 = client.put(
                f"{API}/api/v1/accounts/{account_id}/strategy-instances/{rsi['instance_id']}/config",
                json={"config": {"rsi_period": 21}})
            toggled = r1.status_code == 200 and r2.status_code == 200 and r3.status_code == 200
            print(f"[signals] rsi status/config: {'OK' if toggled else 'FAIL'} "
                  f"({r1.status_code}/{r2.status_code}/{r3.status_code})")
            if not toggled:
                failures += 1

        # 13b. deterministic V-path, M15 bars 0..134
        start = Decimal("1.20000")
        down, up, wick = Decimal("0.00150"), Decimal("0.00220"), Decimal("0.00010")
        closes = [start]
        for _ in range(120):
            closes.append(closes[-1] - down)
        for _ in range(14):
            closes.append(closes[-1] + up)
        base = datetime(2026, 1, 5, tzinfo=timezone.utc)
        sim_bars: list[dict] = []
        prev_close = closes[0]
        for i, close in enumerate(closes):
            open_ = prev_close if i > 0 else close
            sim_bars.append({
                "bar_time": (base + timedelta(minutes=15 * i)).isoformat(),
                "open": str(open_),
                "high": str(max(open_, close) + wick),
                "low": str(min(open_, close) - wick),
                "close": str(close),
                "tick_volume": 100 + i,
            })
            prev_close = close

        def post_bars(chosen: list[dict], batch_index: int, batch_total: int, tag: str,
                      acct: dict | None = None) -> None:
            nonlocal failures
            r = client.post(
                AGENT + "/ea/v1/bars",
                json={
                    "ea_version": "1.0.0", "terminal": terminal(), "account": acct or account(),
                    "broker_symbol": "EURUSD", "timeframe": "M15",
                    "batch_index": batch_index, "batch_total": batch_total,
                    "bars": chosen, "observed_at": iso_now(),
                },
                headers=HEADERS,
            )
            ok = r.status_code == 200
            print(f"[bars] {tag}: {'OK' if ok else 'FAIL'} ({len(chosen)} bars) {r.text[:100]}")
            if not ok:
                failures += 1

        post_bars(sim_bars[:100], 0, 2, "backfill part 1")
        post_bars(sim_bars[100:131], 1, 2, "backfill part 2 (edge: ma_cross @130)")
        post_bars(sim_bars[131:134], 0, 1, "incremental (edge: trend_following @133)")
        post_bars(sim_bars[131:134], 0, 1, "redelivery (dedup: no new signal)")
        post_bars(
            [sim_bars[134],
             {"bar_time": sim_bars[134]["bar_time"], "open": "1.05000",
              "high": "1.04000", "low": "1.06000", "close": "1.05000", "tick_volume": 1}],
            0, 1, "poisoned bar dropped")

        # 13c. exactly one advisory signal per strategy (7.4 shape, 7.5 dedup)
        time.sleep(3)
        sigs = client.get(f"{API}/api/v1/accounts/{account_id}/signals").json()["data"]
        by_code: dict[str, list[dict]] = {}
        for s in sigs:
            by_code.setdefault(s["signal_json"]["strategy_id"], []).append(s)
        ma_sigs, tf_sigs = by_code.get("ma_cross", []), by_code.get("trend_following", [])
        count_ok = len(sigs) == 2 and len(ma_sigs) == 1 and len(tf_sigs) == 1
        print(f"[signals] count={len(sigs)} ma={len(ma_sigs)} tf={len(tf_sigs)} "
              f"{'OK' if count_ok else 'FAIL'}")
        if not count_ok:
            failures += 1
        for label, group, edge_bar, reasons in (
            ("ma_cross", ma_sigs, sim_bars[130], ["MA_FAST_CROSSED_ABOVE"]),
            ("trend_following", tf_sigs, sim_bars[133], ["EMA_TREND_UP", "ADX_ABOVE_THRESHOLD"]),
        ):
            if not group:
                continue
            j = group[0]["signal_json"]
            stops_ok = (Decimal(j["proposed_stop_loss"]) < Decimal(j["reference_price"])
                        < Decimal(j["proposed_take_profit"]))
            ok = (
                j["direction"] == "BUY"
                and j["reason_codes"] == reasons
                and utc_iso(group[0]["source_bar_time"]) == edge_bar["bar_time"]
                and 0 <= j["strength"] <= 1
                and stops_ok
            )
            print(f"[signals] {label} BUY: {'OK' if ok else 'FAIL'} "
                  f"strength={j['strength']} ref={j['reference_price']} "
                  f"reasons={j['reason_codes']} @{edge_bar['bar_time']}")
            if not ok:
                failures += 1

        # 13d. bars query: oldest-first, deterministic count
        rows = client.get(f"{API}/api/v1/accounts/{account_id}/bars",
                          params={"symbol": "EURUSD", "timeframe": "M15", "limit": 2000}).json()["data"]
        times = [datetime.fromisoformat(str(b["bar_time"])) for b in rows]
        ordered = all(times[i] < times[i + 1] for i in range(len(times) - 1))
        bars_ok = len(rows) == 135 and ordered
        print(f"[bars] query count={len(rows)} oldest_first={ordered} "
              f"{'OK' if bars_ok else 'FAIL'}")
        if not bars_ok:
            failures += 1

        # 13e. Reproducible backtest (PRD 15): same fixed ma_cross instance,
        # next-bar-open fills, explicit costs, immutable idempotent report.
        ma_instance = next(
            (i for i in instances if i["strategy_code"] == "ma_cross"), None,
        )
        if ma_instance is None:
            print("[backtest] FAIL: ma_cross instance missing")
            failures += 1
        else:
            backtest_key = uuid.uuid5(
                uuid.NAMESPACE_URL,
                f"quantpilot-e2e-backtest:{account_id}:{ma_instance['instance_id']}",
            )
            backtest_body = {
                "strategy_instance_id": ma_instance["instance_id"],
                "range_start": sim_bars[0]["bar_time"],
                "range_end": sim_bars[-1]["bar_time"],
                "initial_balance": "10000",
                "volume": "0.01",
                "cost_model": {
                    "spread_per_side": "0.00005",
                    "slippage_per_side": "0.00002",
                    "commission_per_lot_per_side": "3.5",
                    "swap_per_lot_per_bar": "0",
                    "contract_size": "100000",
                    "quote_to_account_rate": "1",
                },
            }
            before_commands = client.get(
                f"{API}/api/v1/commands",
                params={"account_id": account_id, "limit": 500},
            ).json().get("data", [])
            r = client.post(
                f"{API}/api/v1/backtests", json=backtest_body,
                headers={"Idempotency-Key": str(backtest_key)},
            )
            job = r.json().get("data") or {}
            metrics = job.get("metrics") or {}
            report = job.get("report") or {}
            trades = report.get("trades") or []
            backtest_ok = (
                r.status_code == 200 and job.get("status") == "COMPLETED"
                and (report.get("data") or {}).get("bar_count") == 135
                and metrics.get("trade_count", 0) >= 1
                and Decimal((metrics.get("cost_breakdown") or {}).get("commission", "0")) > 0
                and (report.get("assumptions") or {}).get("entry")
                    == "NEXT_BAR_OPEN_FULL_FILL"
                and len(report.get("equity_curve") or []) == 135
            )
            print(f"[backtest] run: {'OK' if backtest_ok else 'FAIL'} "
                  f"status={r.status_code} job={job.get('status')} "
                  f"trades={metrics.get('trade_count')}")
            if not backtest_ok:
                failures += 1
            if trades:
                first = trades[0]
                next_open_ok = (
                    utc_iso(first.get("source_bar_time")) == sim_bars[130]["bar_time"]
                    and utc_iso(first.get("entry_time")) == sim_bars[131]["bar_time"]
                    and Decimal(first["entry_price"])
                        == Decimal(sim_bars[131]["open"]) + Decimal("0.00007")
                )
                print(f"[backtest] no-lookahead fill: {'OK' if next_open_ok else 'FAIL'} "
                      f"entry={first.get('entry_price')} @{first.get('entry_time')}")
                if not next_open_ok:
                    failures += 1
            replay = client.post(
                f"{API}/api/v1/backtests", json=backtest_body,
                headers={"Idempotency-Key": str(backtest_key)},
            )
            replay_job = replay.json().get("data") or {}
            replay_ok = (
                replay.status_code == 200 and replay_job.get("job_id") == job.get("job_id")
                and (replay_job.get("report") or {}).get("data") == report.get("data")
            )
            print(f"[backtest] idempotent replay: {'OK' if replay_ok else 'FAIL'} "
                  f"job={replay_job.get('job_id')}")
            if not replay_ok:
                failures += 1
            conflict = client.post(
                f"{API}/api/v1/backtests", json={**backtest_body, "volume": "0.02"},
                headers={"Idempotency-Key": str(backtest_key)},
            )
            conflict_ok = conflict.status_code == 409
            print(f"[backtest] key/body conflict: {'OK' if conflict_ok else 'FAIL'} "
                  f"status={conflict.status_code}")
            if not conflict_ok:
                failures += 1
            after_commands = client.get(
                f"{API}/api/v1/commands",
                params={"account_id": account_id, "limit": 500},
            ).json().get("data", [])
            isolated = len(after_commands) == len(before_commands)
            print(f"[backtest] no trade commands: {'OK' if isolated else 'FAIL'} "
                  f"commands={len(before_commands)}->{len(after_commands)}")
            if not isolated:
                failures += 1

        # 14. AI analysis engine (PRD 8, ADVISORY first release): manual
        # trigger on the newest closed bar, strict doc-8.4 shape, the V-path
        # tail classifies HIGH_VOL (ATR/close 0.00205 > 0.0015), input_hash
        # idempotency and the OFF fallback (8.5). Analyses never touch trade
        # permission (8.1/8.5) - no command wiring here.
        r1 = client.post(f"{API}/api/v1/accounts/{account_id}/ai/analyze",
                        json={"broker_symbol": "EURUSD", "timeframe": "M15"})
        d1 = r1.json().get("data") or {}
        analysis = d1.get("analysis") or {}
        scores = {s["strategy_id"]: s for s in analysis.get("strategy_scores", [])}
        ma_sc, tf_sc = scores.get("ma_cross", {}), scores.get("trend_following", {})
        shape_ok = (
            r1.status_code == 200
            and d1.get("ai_mode") == "ADVISORY"
            and analysis.get("model_provider") == "statistical-regime"
            and analysis.get("model_id") == "regime-rules-v1"
            and analysis.get("prompt_version") == "market-eval-v1"
            and analysis.get("market_regime") == "HIGH_VOL"
            and 0.5 <= float(analysis.get("regime_confidence", 0)) <= 1
            and analysis.get("signal_decision") == "REVIEW"
            and analysis.get("risk_flags") == ["VOLATILITY_ELEVATED"]
            and str(analysis.get("input_snapshot_id", "")).startswith("sha256:")
            and analysis.get("source_bar_time") == sim_bars[134]["bar_time"]
            and analysis.get("cost") == "0"
        )
        print(f"[ai] manual analyze: {'OK' if shape_ok else 'FAIL'} "
              f"regime={analysis.get('market_regime')} "
              f"conf={analysis.get('regime_confidence')} @{analysis.get('source_bar_time')}")
        if not shape_ok:
            failures += 1

        # scores cover the two M15 instances only (rsi_momentum is M30, no bars)
        score_ok = (
            ma_sc.get("score") == 0.30 and ma_sc.get("reason_codes") == ["VOLATILITY_ELEVATED"]
            and tf_sc.get("score") == 0.35 and tf_sc.get("reason_codes") == ["VOLATILITY_ELEVATED"]
            and "rsi_momentum" not in scores
        )
        print(f"[ai] strategy scores: {'OK' if score_ok else 'FAIL'} "
              f"ma={ma_sc.get('score')} tf={tf_sc.get('score')}")
        if not score_ok:
            failures += 1

        # 14b. input-hash idempotency: same snapshot -> no new row
        before = client.get(f"{API}/api/v1/accounts/{account_id}/ai/analyses",
                            params={"symbol": "EURUSD", "timeframe": "M15",
                                    "limit": 500}).json()["data"]
        client.post(f"{API}/api/v1/accounts/{account_id}/ai/analyze",
                   json={"broker_symbol": "EURUSD", "timeframe": "M15"})
        after = client.get(f"{API}/api/v1/accounts/{account_id}/ai/analyses",
                           params={"symbol": "EURUSD", "timeframe": "M15",
                                   "limit": 500}).json()["data"]
        dup_ok = len(before) >= 1 and len(after) == len(before)
        print(f"[ai] redelivery dedup: {'OK' if dup_ok else 'FAIL'} "
              f"{len(before)} -> {len(after)}")
        if not dup_ok:
            failures += 1

        # 14c. OFF mode (8.5): no analysis runs; config PUT restores ADVISORY
        cfg = client.get(f"{API}/api/v1/accounts/{account_id}/config").json()["data"]
        doc = cfg["config"]
        doc["ai"] = {"mode": "OFF"}
        client.put(f"{API}/api/v1/accounts/{account_id}/config", json=doc)
        r2 = client.post(f"{API}/api/v1/accounts/{account_id}/ai/analyze",
                        json={"broker_symbol": "EURUSD", "timeframe": "M15"})
        d2 = r2.json().get("data") or {}
        off_ok = r2.status_code == 200 and d2.get("ai_mode") == "OFF" and d2.get("analysis") is None
        print(f"[ai] mode OFF: {'OK' if off_ok else 'FAIL'} "
              f"mode={d2.get('ai_mode')} analysis={d2.get('analysis')}")
        if not off_ok:
            failures += 1
        doc["ai"] = {"mode": "ADVISORY"}
        restored = client.put(f"{API}/api/v1/accounts/{account_id}/config", json=doc)
        print(f"[ai] mode ADVISORY restored: {'OK' if restored.status_code == 200 else 'FAIL'}")
        if restored.status_code != 200:
            failures += 1

        # 15. Strategy -> AI context -> aggregation -> risk -> durable command.
        # A separate account keeps the original 135 bars / two engine signals
        # deterministic. Instances stay DISABLED; SQL fixtures isolate the
        # automation contract from strategy signal-generation edge timing.
        auto_acct = {**account(), "login_ref": "10005678"}
        post("/ea/v1/register", {
            "ea_version": "1.0.0", "terminal": terminal(), "account": auto_acct,
            "observed_at": iso_now(),
        })
        post("/ea/v1/heartbeat", {
            **hb, "account": auto_acct, "open_positions": 0, "observed_at": iso_now(),
        })
        post("/ea/v1/symbols", {
            "ea_version": "1.0.0", "terminal": terminal(), "account": auto_acct,
            "batch_index": 0, "batch_total": 1, "symbols": [sim_symbols()[0]],
            "observed_at": iso_now(),
        })
        auto_acc = None
        for _ in range(20):
            accounts = client.get(API + "/api/v1/accounts").json()["data"]
            auto_acc = next((a for a in accounts if a.get("login_ref") == "10005678"), None)
            if auto_acc is not None:
                break
            time.sleep(0.25)
        if auto_acc is None:
            print("[automation] FAIL: isolated account not registered")
            failures += 1
        else:
            auto_id = auto_acc["account_id"]

            def auto_check(name: str, ok: bool, detail: str = "") -> None:
                nonlocal failures
                print(f"[automation] {name}: {'OK' if ok else 'FAIL'} {detail}")
                if not ok:
                    failures += 1

            def auto_config(enabled: bool, risk_fraction: str) -> None:
                r = client.put(f"{API}/api/v1/accounts/{auto_id}/config", json={
                    "filters": {"max_daily_orders": 500},
                    "risk": {"per_trade_risk_fraction": risk_fraction},
                    "ai": {"mode": "ADVISORY"},
                    "automation": {"command_generation": enabled},
                })
                if r.status_code != 200:
                    auto_check("config_put", False, f"status={r.status_code} {r.text[:100]}")

            for code in ("ma_cross", "trend_following"):
                r = client.post(f"{API}/api/v1/accounts/{auto_id}/strategy-instances", json={
                    "strategy_code": code, "broker_symbol": "EURUSD",
                    "timeframe": "M15", "config": {},
                })
                auto_check(f"bind_{code}", r.status_code in (200, 409), f"status={r.status_code}")
            auto_instances = client.get(
                f"{API}/api/v1/accounts/{auto_id}/strategy-instances"
            ).json().get("data", [])
            instance_by_code = {i["strategy_code"]: i for i in auto_instances}
            for instance in auto_instances:
                r = client.post(
                    f"{API}/api/v1/accounts/{auto_id}/strategy-instances/"
                    f"{instance['instance_id']}/status",
                    json={"status": "DISABLED"},
                )
                auto_check(f"disable_{instance['strategy_code']}", r.status_code == 200)

            fixture_now = datetime.now(timezone.utc)
            valid_until = fixture_now + timedelta(seconds=300)
            fixture_ids: dict[tuple[int, str], str] = {}
            fixtures = (
                (134, "ma_cross", "BUY", 0.40),
                (134, "trend_following", "BUY", 0.90),
                (133, "ma_cross", "BUY", 0.70),
                (133, "trend_following", "SELL", 0.80),
                (132, "ma_cross", "BUY", 0.75),
            )
            with psycopg.connect(PG_DSN) as conn:
                for bar_index, code, direction, strength in fixtures:
                    source_bar = datetime.fromisoformat(sim_bars[bar_index]["bar_time"])
                    signal_id = str(uuid.uuid5(
                        uuid.NAMESPACE_URL,
                        f"quantpilot-e2e:{auto_id}:{bar_index}:{code}:{direction}",
                    ))
                    fixture_ids[(bar_index, code)] = signal_id
                    reference = Decimal(sim_bars[bar_index]["close"])
                    buy = direction == "BUY"
                    doc = {
                        "signal_id": signal_id,
                        "strategy_id": code,
                        "symbol": "EURUSD",
                        "timeframe": "M15",
                        "signal_type": "ENTRY",
                        "direction": direction,
                        "strength": strength,
                        "generated_at": fixture_now.isoformat(),
                        "market_data_at": source_bar.isoformat(),
                        "reference_price": str(reference),
                        "proposed_stop_loss": str(reference + Decimal("-0.005" if buy else "0.005")),
                        "proposed_take_profit": str(reference + Decimal("0.010" if buy else "-0.010")),
                        "valid_until": valid_until.isoformat(),
                        "features_ref": "e2e.automation.fixture",
                        "reason_codes": ["E2E_AUTOMATION_FIXTURE"],
                    }
                    conn.execute(
                        """
                        INSERT INTO strategy_signals
                            (signal_id, account_id, strategy_instance_id, signal_json,
                             source_bar_time, generated_at, expires_at)
                        VALUES (%s, %s, %s, CAST(%s AS jsonb), %s, %s, %s)
                        ON CONFLICT (signal_id) DO UPDATE SET
                            signal_json = EXCLUDED.signal_json,
                            generated_at = EXCLUDED.generated_at,
                            expires_at = EXCLUDED.expires_at
                        """,
                        (signal_id, auto_id, instance_by_code[code]["instance_id"],
                         json.dumps(doc), source_bar, fixture_now, valid_until),
                    )

            def auto_commands() -> list[dict]:
                rows = client.get(f"{API}/api/v1/commands", params={
                    "account_id": auto_id, "limit": 500,
                }).json().get("data", [])
                return [row for row in rows if row.get("created_by") == "signal-aggregator"]

            baseline_ids = {row["command_id"] for row in auto_commands()}

            # OFF dominates even with a non-zero risk budget.
            auto_config(False, "0.01")
            post_bars(sim_bars[:100], 0, 3, "automation backfill 1", acct=auto_acct)
            post_bars(sim_bars[100:131], 1, 3, "automation backfill 2", acct=auto_acct)
            post_bars(sim_bars[131:], 2, 3, "automation OFF", acct=auto_acct)
            time.sleep(3)
            off_ids = {row["command_id"] for row in auto_commands()}
            auto_check("default_off", off_ids == baseline_ids,
                       f"commands={len(off_ids)}")

            # Explicit ON is still safe when the sizing budget is zero.
            auto_config(True, "0")
            post_bars([sim_bars[134]], 0, 1, "automation zero risk", acct=auto_acct)
            time.sleep(2)
            zero_risk_ids = {row["command_id"] for row in auto_commands()}
            auto_check("zero_risk_no_command", zero_risk_ids == baseline_ids,
                       f"commands={len(zero_risk_ids)}")

            # Two BUY signals become one command; the strongest is primary.
            auto_config(True, "0.01")
            post_bars([sim_bars[134]], 0, 1, "automation aggregate", acct=auto_acct)
            time.sleep(2)
            post_bars([sim_bars[134]], 0, 1, "automation aggregate replay", acct=auto_acct)
            time.sleep(2)
            rows = auto_commands()
            aggregated_ids = {row["command_id"] for row in rows}
            allowed_rows = [row for row in rows if row.get("status") != "REJECTED"]
            allowed = allowed_rows[0] if len(allowed_rows) == 1 else None
            aggregate_ok = (
                baseline_ids <= aggregated_ids
                and len(aggregated_ids - baseline_ids) <= 1
                and allowed is not None
                and allowed.get("signal_id") == fixture_ids[(134, "trend_following")]
                and Decimal(allowed["payload"]["volume"]) > 0
            )
            auto_check("same_direction_aggregated_once", aggregate_ok,
                       f"commands={len(rows)} signal={allowed.get('signal_id') if allowed else '-'}")
            if allowed:
                detail = client.get(f"{API}/api/v1/commands/{allowed['command_id']}").json().get("data", {})
                auto_check("aggregated_risk_allow",
                           (detail.get("risk_decision") or {}).get("decision") == "ALLOW",
                           f"risk={detail.get('risk_decision')}")

            # Opposite directions on one source bar never create a command.
            post_bars([sim_bars[133]], 0, 1, "automation conflict", acct=auto_acct)
            time.sleep(2)
            conflict_ids = {row["command_id"] for row in auto_commands()}
            auto_check("opposite_direction_conflict", conflict_ids == aggregated_ids,
                       f"commands={len(conflict_ids)}")

            # A cloud stop persists an automated REJECTED command, never QUEUED.
            stopped = client.post(f"{API}/api/v1/risk/stop", json={
                "scope": "ACCOUNT", "account_id": auto_id,
                "reason": "automation e2e", "locked": False, "updated_by": "sim_ea",
            })
            auto_check("account_stop_set", stopped.status_code == 200)
            post_bars([sim_bars[132]], 0, 1, "automation risk reject", acct=auto_acct)
            time.sleep(2)
            resumed = client.post(f"{API}/api/v1/risk/resume", json={
                "scope": "ACCOUNT", "account_id": auto_id,
            })
            auto_check("account_stop_resumed", resumed.status_code == 200)
            rows = auto_commands()
            risk_ids = {row["command_id"] for row in rows}
            rejected = next((row for row in rows if row.get("status") == "REJECTED"), None)
            reject_detail = (
                client.get(f"{API}/api/v1/commands/{rejected['command_id']}").json().get("data", {})
                if rejected else {}
            )
            auto_check(
                "risk_rejected_durable",
                aggregated_ids <= risk_ids
                and len(risk_ids - aggregated_ids) <= 1
                and rejected is not None
                and (reject_detail.get("risk_decision") or {}).get("reason_codes")
                    == ["ACCOUNT_STOP_NEW_RISK"],
                f"commands={len(rows)} risk={reject_detail.get('risk_decision')}",
            )

            # Drain the isolated account: only the ALLOW row may be delivered.
            delivered_auto: list[str] = []
            for _ in range(4):
                got = poll([], acct=auto_acct).get("commands", [])
                if not got:
                    break
                cid = got[0]["command_id"]
                delivered_auto.append(cid)
                ack(cid, acct=auto_acct)
            time.sleep(2)
            leaked = rejected is not None and rejected["command_id"] in delivered_auto
            auto_check("rejected_never_delivered", not leaked, f"delivered={delivered_auto}")
            if allowed:
                auto_check("allowed_observe_only_ack", command_status(allowed["command_id"]) == "ACKED",
                           f"status={command_status(allowed['command_id'])}")
            auto_config(False, "0")

    print(f"\nagent-side failures: {failures}")
    return failures


if __name__ == "__main__":
    sys.exit(main())
