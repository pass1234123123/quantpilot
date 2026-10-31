"""End-to-end slice verification: EA -> Agent -> NATS -> API -> PostgreSQL.

Checks the observable state after synthetic (or real) EA events have flowed:
  1. Cloud API endpoints (PRD 13.2): nodes / terminals / accounts /
     account snapshot / account symbols
  2. PostgreSQL row counts through the dev SSH tunnel (QP_PG_DSN)

Usage: python scripts/e2e_verify.py  (run after scripts/sim_ea.py)
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

import httpx
import psycopg

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "packages" / "contracts" / "src"))
from quantpilot_contracts.envfile import load_env_file  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]
load_env_file(ROOT / ".env")

API = os.environ.get("QP_API_BASE_URL", "http://127.0.0.1:8000")
PG_DSN = os.environ["QP_PG_DSN"]

TABLES = [
    "trading_nodes",
    "mt5_terminals",
    "trading_accounts",
    "account_snapshots",
    "symbols",
    "broker_symbols",
    "symbol_specs",
    "symbol_quotes",
    "positions",
    "deals",
    "orders",
    "calendar_events",
    "risk_profiles",
    "commands",
    "system_events",
]

failures = 0


def check(name: str, ok: bool, detail: str = "") -> None:
    global failures
    tag = "PASS" if ok else "FAIL"
    print(f"[{tag}] {name}{(' ' + detail) if detail else ''}")
    if not ok:
        failures += 1


def main() -> int:
    client = httpx.Client(timeout=10, trust_env=False)

    # ---- 1. API endpoints -------------------------------------------------
    r = client.get(f"{API}/healthz")
    check("api.healthz", r.status_code == 200, f"status={r.status_code}")

    nodes = client.get(f"{API}/api/v1/nodes").json()["data"]
    check("api.nodes", len(nodes) >= 1, f"count={len(nodes)}")
    node = nodes[0] if nodes else {}
    print(f"       node: id={node.get('node_id')} status={node.get('status')} "
          f"terminals={node.get('terminals')} accounts={node.get('accounts')}")

    terminals = client.get(f"{API}/api/v1/terminals").json()["data"]
    check("api.terminals", len(terminals) >= 1, f"count={len(terminals)}")
    if terminals:
        t = terminals[0]
        print(f"       terminal: path_hash={str(t.get('terminal_path_hash'))[:16]}... "
              f"build={t.get('terminal_build')} company={t.get('terminal_company')}")

    accounts = client.get(f"{API}/api/v1/accounts").json()["data"]
    check("api.accounts", len(accounts) >= 1, f"count={len(accounts)}")
    if not accounts:
        print("       no accounts -> snapshot/symbols checks skipped")
        return finish()

    acc = accounts[0]
    account_id = acc["account_id"]
    print(f"       account: login_ref={acc.get('login_ref')} server={acc.get('server')} "
          f"currency={acc.get('currency')} status={acc.get('status')}")

    snap = client.get(f"{API}/api/v1/accounts/{account_id}/snapshot")
    snap_ok = snap.status_code == 200
    check("api.snapshot", snap_ok, f"status={snap.status_code}")
    if snap_ok:
        s = snap.json()["data"]
        print(f"       snapshot: balance={s.get('balance')} equity={s.get('equity')} "
              f"open_positions={s.get('open_positions')} captured_at={s.get('captured_at')}")

    hist = client.get(f"{API}/api/v1/accounts/{account_id}/snapshots?limit=500")
    hist_data = hist.json().get("data", [])
    check("api.snapshots_history", hist.status_code == 200 and len(hist_data) >= 1,
          f"status={hist.status_code} count={len(hist_data)}")

    syms = client.get(f"{API}/api/v1/accounts/{account_id}/symbols")
    syms_data = syms.json().get("data", [])
    check("api.symbols", len(syms_data) >= 1, f"count={len(syms_data)}")
    for srow in syms_data[:5]:
        print(f"       symbol: {srow.get('broker_symbol')} canonical={srow.get('canonical_instrument')} "
              f"spread={srow.get('spread_points')}pt @{str(srow.get('quote_observed_at'))[:19]}")

    poses = client.get(f"{API}/api/v1/accounts/{account_id}/positions")
    poses_data = poses.json().get("data", [])
    check("api.positions", poses.status_code == 200 and len(poses_data) >= 1,
          f"status={poses.status_code} count={len(poses_data)}")
    for prow in poses_data[:5]:
        print(f"       position: ticket={prow.get('broker_position_ticket')} {prow.get('broker_symbol')} "
              f"{prow.get('direction')} profit={prow.get('profit')} status={prow.get('status')}")

    # ---- deals / orders / performance on the sim account (deterministic) ----
    sim = next((a for a in accounts if a.get("login_ref") == "10001234"), None)
    check("sim.account_present", sim is not None)
    if sim:
        sim_id = sim["account_id"]
        # symbol_quotes projection: distinct non-zero spreads from sim_ea
        ssyms = client.get(f"{API}/api/v1/accounts/{sim_id}/symbols")
        ssyms_data = ssyms.json().get("data", [])
        expected = {"EURUSD": 12, "GBPUSD": 15, "XAUUSD": 25}
        got = {s.get("broker_symbol"): s.get("spread_points") for s in ssyms_data}
        spread_ok = all(got.get(n) == p for n, p in expected.items())
        quoted_at = all(s.get("quote_observed_at") for s in ssyms_data if s.get("broker_symbol") in expected)
        check("api.symbols_spread", spread_ok and quoted_at, f"spreads={got}")
        for srow in [s for s in ssyms_data if s.get("broker_symbol") in expected][:3]:
            print(f"       quote: {srow.get('broker_symbol')} spread={srow.get('spread_points')}pt "
                  f"bid={srow.get('bid')} ask={srow.get('ask')} @{str(srow.get('quote_observed_at'))[:19]}")

        deals = client.get(f"{API}/api/v1/accounts/{sim_id}/deals")
        deals_data = deals.json().get("data", [])
        check("api.deals", deals.status_code == 200 and len(deals_data) >= 2,
              f"status={deals.status_code} count={len(deals_data)}")
        for drow in deals_data[:5]:
            print(f"       deal: ticket={drow.get('broker_deal_ticket')} {drow.get('broker_symbol')} "
                  f"{drow.get('direction')}/{drow.get('entry')} profit={drow.get('profit')}")

        orders = client.get(f"{API}/api/v1/accounts/{sim_id}/orders")
        orders_data = orders.json().get("data", [])
        check("api.orders", orders.status_code == 200 and len(orders_data) >= 1,
              f"status={orders.status_code} count={len(orders_data)}")
        removed = [o for o in orders_data if o.get("broker_ticket") == "800001" and o.get("status") == "REMOVED"]
        check("api.orders_state_sync", len(removed) == 1,
              "ticket 800001 REMOVED after empty snapshot" if removed else "ticket 800001 not REMOVED")
        for orow in orders_data[:5]:
            print(f"       order: ticket={orow.get('broker_ticket')} {orow.get('broker_symbol')} "
                  f"{orow.get('order_kind')} status={orow.get('status')}")

        perf = client.get(f"{API}/api/v1/accounts/{sim_id}/performance")
        perf_ok = perf.status_code == 200
        check("api.performance", perf_ok, f"status={perf.status_code}")
        if perf_ok:
            pdata = perf.json()["data"]
            stats = pdata.get("stats", {})
            check("api.performance_stats",
                  stats.get("closed_trades", 0) >= 1 and stats.get("all_deals", 0) >= 2,
                  f"closed={stats.get('closed_trades')} all={stats.get('all_deals')} "
                  f"net={stats.get('net_profit')}")
            check("api.performance_daily", len(pdata.get("daily", [])) == 30,
                  f"buckets={len(pdata.get('daily', []))}")

        # calendar: sim_ea posts 3 events (1 past with actual, 2 future)
        cal = client.get(f"{API}/api/v1/accounts/{sim_id}/calendar")
        cal_ok = cal.status_code == 200
        cal_data = cal.json().get("data", []) if cal_ok else []
        fields_ok = all(
            e.get("event_uid") and e.get("name") and e.get("event_time") and e.get("importance")
            for e in cal_data
        )
        has_future = any(e.get("event_uid") == "sim-cal-002" for e in cal_data)
        check("api.calendar", cal_ok and fields_ok and len(cal_data) >= 3 and has_future,
              f"status={cal.status_code} count={len(cal_data)}")
        for crow in cal_data[:3]:
            print(f"       calendar: {crow.get('currency')} {crow.get('name')} "
                  f"importance={crow.get('importance')} @{str(crow.get('event_time'))[:16]}")
        past = next((e for e in cal_data if e.get("event_uid") == "sim-cal-001"), None)
        check("api.calendar_past_actual", past is not None and past.get("actual") is not None,
              f"actual={past.get('actual') if past else '-'}")

        # config: PUT must bump the version; lookup must resolve the sim identity
        cfg0 = client.get(f"{API}/api/v1/accounts/{sim_id}/config")
        cfg0_ok = cfg0.status_code == 200 and (cfg0.json().get("data") or {}).get("config", {}).get("filters")
        check("api.config_get", cfg0_ok is not False,
              f"status={cfg0.status_code} version={(cfg0.json().get('data') or {}).get('version') if cfg0.status_code == 200 else '-'}")
        if cfg0_ok:
            before = cfg0.json()["data"]
            doc = before["config"]
            doc["filters"]["max_positions"] = 7
            put = client.put(f"{API}/api/v1/accounts/{sim_id}/config", json=doc)
            check("api.config_put", put.status_code == 200, f"status={put.status_code}")
            if put.status_code == 200:
                after = put.json()["data"]
                check("api.config_version_bump",
                      after.get("version") == before.get("version", 0) + 1,
                      f"{before.get('version')} -> {after.get('version')}")
                check("api.config_value", after["config"]["filters"]["max_positions"] == 7)
                again = client.get(f"{API}/api/v1/accounts/{sim_id}/config").json()["data"]
                check("api.config_persisted",
                      again.get("version") == after.get("version")
                      and again["config"]["filters"]["max_positions"] == 7)

        import hashlib
        sim_hash = "sha256:" + hashlib.sha256(b"C:\\sim\\terminal-data").hexdigest()
        lk = client.post(f"{API}/api/v1/config/lookup", json={
            "data_path_hash": sim_hash, "login_ref": "10001234", "server": "MetaQuotes-Demo-Sim",
        })
        lk_data = lk.json().get("data") or {} if lk.status_code == 200 else {}
        check("api.config_lookup",
              lk.status_code == 200 and lk_data.get("resolved") is True,
              f"status={lk.status_code} resolved={lk_data.get('resolved')} "
              f"version={lk_data.get('version')}")

    # ---- 2. PostgreSQL row counts ------------------------------------------
    with psycopg.connect(PG_DSN) as conn:
        for table in TABLES:
            n = conn.execute(f"SELECT count(*) FROM {table}").fetchone()[0]
            ok = n >= 1
            check(f"pg.{table}", ok, f"rows={n}")

        # ---- 3. Command channel invariants (AT-005/AT-006, PRD 13.5) -------
        dup = conn.execute(
            """
            SELECT count(*) FROM (
                SELECT command_id FROM commands GROUP BY command_id HAVING count(*) > 1
            ) d
            """
        ).fetchone()[0]
        check("pg.commands_unique", dup == 0, f"duplicate_command_ids={dup}")

        acked = conn.execute(
            "SELECT count(*) FROM commands WHERE status = 'ACKED'"
        ).fetchone()[0]
        check("pg.commands_acked", acked >= 1, f"acked={acked}")

        reconciled = conn.execute(
            "SELECT count(*) FROM commands WHERE status = 'ACKED' AND reconciled"
        ).fetchone()[0]
        check("pg.commands_reconciled", reconciled >= 1,
              f"reconciled_from_unknown={reconciled}")

        expired = conn.execute(
            "SELECT count(*) FROM commands WHERE status = 'EXPIRED'"
        ).fetchone()[0]
        check("pg.commands_expired", expired >= 1, f"expired={expired}")

        cmd_events = conn.execute(
            "SELECT count(*) FROM system_events WHERE event_type LIKE 'command.%'"
        ).fetchone()[0]
        check("pg.command_events", cmd_events >= 5, f"events={cmd_events}")

    return finish()


def finish() -> int:
    print(f"\ne2e failures: {failures}")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
