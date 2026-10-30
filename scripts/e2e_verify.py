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
    "positions",
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
              f"spec_hash={str(srow.get('spec_hash'))[:16]}...")

    poses = client.get(f"{API}/api/v1/accounts/{account_id}/positions")
    poses_data = poses.json().get("data", [])
    check("api.positions", poses.status_code == 200 and len(poses_data) >= 1,
          f"status={poses.status_code} count={len(poses_data)}")
    for prow in poses_data[:5]:
        print(f"       position: ticket={prow.get('broker_position_ticket')} {prow.get('broker_symbol')} "
              f"{prow.get('direction')} profit={prow.get('profit')} status={prow.get('status')}")

    # ---- 2. PostgreSQL row counts ------------------------------------------
    with psycopg.connect(PG_DSN) as conn:
        for table in TABLES:
            n = conn.execute(f"SELECT count(*) FROM {table}").fetchone()[0]
            ok = n >= 1
            check(f"pg.{table}", ok, f"rows={n}")

    return finish()


def finish() -> int:
    print(f"\ne2e failures: {failures}")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
