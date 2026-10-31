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
import uuid
from datetime import datetime, timezone
from decimal import Decimal
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
    "risk_states",
    "risk_decisions",
    "commands",
    "symbol_bars",
    "strategies",
    "strategy_versions",
    "strategy_instances",
    "strategy_signals",
    "ai_analyses",
    "backtest_jobs",
    "backtest_results",
    "system_events",
]

failures = 0


def check(name: str, ok: bool, detail: str = "") -> None:
    global failures
    tag = "PASS" if ok else "FAIL"
    print(f"[{tag}] {name}{(' ' + detail) if detail else ''}")
    if not ok:
        failures += 1


def _utc(value) -> str:
    return datetime.fromisoformat(str(value)).astimezone(timezone.utc).isoformat()


def main() -> int:
    client = httpx.Client(
        timeout=10,
        trust_env=False,
        headers={"X-API-Key": os.environ.get("QP_API_KEY", "")},
    )

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

        # ---- strategy engine (PRD 7): catalog + instances + signals + bars --
        strategies = client.get(f"{API}/api/v1/strategies").json()["data"]
        codes = {s.get("code") for s in strategies}
        check("api.strategies",
              {"ma_cross", "rsi_momentum", "trend_following",
               "breakout", "mean_reversion"} <= codes,
              f"codes={sorted(c for c in codes if c)}")
        strat_by_code = {s.get("code"): s for s in strategies}
        for code, must_have in (
            ("breakout", {"lookback_period", "squeeze_width_max"}),
            ("mean_reversion", {"bollinger_period", "entry_z", "adx_threshold"}),
        ):
            row = strat_by_code.get(code) or {}
            props = set(((row.get("config_schema") or {}).get("properties")) or {})
            check(f"api.strategy_catalog_{code}",
                  row.get("status") == "ACTIVE" and row.get("version") == "1.0.0"
                  and must_have <= props,
                  f"status={row.get('status')} version={row.get('version')} "
                  f"props={sorted(props)}")

        instances = client.get(
            f"{API}/api/v1/accounts/{sim_id}/strategy-instances").json()["data"]
        inst_codes = {i.get("strategy_code") for i in instances}
        check("api.strategy_instances",
              {"ma_cross", "trend_following", "rsi_momentum",
               "breakout", "mean_reversion"} <= inst_codes,
              f"count={len(instances)}")
        rsi_inst = next((i for i in instances if i.get("strategy_code") == "rsi_momentum"), None)
        if rsi_inst is not None:
            check("api.instance_status_config",
                  rsi_inst.get("status") == "ENABLED"
                  and (rsi_inst.get("config_revision") or 0) >= 2
                  and (rsi_inst.get("config") or {}).get("rsi_period") == 21,
                  f"status={rsi_inst.get('status')} revision={rsi_inst.get('config_revision')} "
                  f"rsi_period={(rsi_inst.get('config') or {}).get('rsi_period')}")

        # sim_ea posts a deterministic V-path; the two edge bars (130 = ma_cross
        # fast/slow cross, 133 = trend state flip) must each yield exactly one
        # advisory BUY signal, and redeliveries must not duplicate them (7.5)
        sigs = client.get(f"{API}/api/v1/accounts/{sim_id}/signals").json()["data"]
        by_code: dict = {}
        for s in sigs:
            by_code.setdefault((s.get("signal_json") or {}).get("strategy_id"), []).append(s)
        ma_sigs = by_code.get("ma_cross", [])
        tf_sigs = by_code.get("trend_following", [])
        check("api.signals_count",
              len(sigs) == 2 and len(ma_sigs) == 1 and len(tf_sigs) == 1,
              f"total={len(sigs)} ma={len(ma_sigs)} tf={len(tf_sigs)}")
        # task-3 M30 control instances never receive bars -> must stay silent
        check("api.signals_new_strategies_quiet",
              not by_code.get("breakout") and not by_code.get("mean_reversion"),
              f"breakout={len(by_code.get('breakout') or [])} "
              f"mean_reversion={len(by_code.get('mean_reversion') or [])}")
        if ma_sigs:
            j = ma_sigs[0]["signal_json"]
            src = _utc(ma_sigs[0].get("source_bar_time"))
            check("api.signal_ma_cross",
                  j.get("direction") == "BUY"
                  and j.get("reason_codes") == ["MA_FAST_CROSSED_ABOVE"]
                  and j.get("signal_type") == "ENTRY"
                  and src == "2026-01-06T08:30:00+00:00"
                  and Decimal(j["proposed_stop_loss"]) < Decimal(j["reference_price"])
                  < Decimal(j["proposed_take_profit"]),
                  f"dir={j.get('direction')} reasons={j.get('reason_codes')} @{src}")
            print(f"       signal: ma_cross BUY strength={j.get('strength')} "
                  f"ref={j.get('reference_price')}")
        if tf_sigs:
            j = tf_sigs[0]["signal_json"]
            src = _utc(tf_sigs[0].get("source_bar_time"))
            check("api.signal_trend_following",
                  j.get("direction") == "BUY"
                  and j.get("reason_codes") == ["EMA_TREND_UP", "ADX_ABOVE_THRESHOLD"]
                  and src == "2026-01-06T09:15:00+00:00"
                  and Decimal(j["proposed_stop_loss"]) < Decimal(j["reference_price"])
                  < Decimal(j["proposed_take_profit"]),
                  f"dir={j.get('direction')} reasons={j.get('reason_codes')} @{src}")
            print(f"       signal: trend_following BUY strength={j.get('strength')} "
                  f"ref={j.get('reference_price')}")

        bars = client.get(f"{API}/api/v1/accounts/{sim_id}/bars",
                          params={"symbol": "EURUSD", "timeframe": "M15", "limit": 2000})
        bars_data = bars.json().get("data", []) if bars.status_code == 200 else []
        times = [datetime.fromisoformat(str(b["bar_time"])) for b in bars_data]
        ordered = all(times[i] < times[i + 1] for i in range(len(times) - 1))
        check("api.bars", len(bars_data) == 135 and ordered,
              f"status={bars.status_code} count={len(bars_data)} oldest_first={ordered}")

        # ---- strategy backtest (PRD 15): durable report + cost assumptions --
        ma_inst = next((i for i in instances if i.get("strategy_code") == "ma_cross"), None)
        if ma_inst is not None and bars_data:
            key = uuid.uuid5(
                uuid.NAMESPACE_URL,
                f"quantpilot-e2e-backtest:{sim_id}:{ma_inst['instance_id']}",
            )
            job_id = uuid.uuid5(uuid.NAMESPACE_URL, f"quantpilot-backtest-v1:{key}")
            response = client.get(f"{API}/api/v1/backtests/{job_id}")
            job = response.json().get("data") or {}
            metrics = job.get("metrics") or {}
            report = job.get("report") or {}
            required_metrics = {
                "net_profit", "max_drawdown", "profit_factor", "win_rate",
                "expectancy", "sharpe", "sortino", "recovery_factor",
                "exposure", "average_holding_minutes", "trade_count", "cost_breakdown",
            }
            check("api.backtest_report",
                  response.status_code == 200 and job.get("status") == "COMPLETED"
                  and job.get("strategy_code") == "ma_cross"
                  and job.get("config_revision") == ma_inst.get("config_revision")
                  and required_metrics <= set(metrics)
                  and (report.get("data") or {}).get("bar_count") == 135
                  and len((report.get("data") or {}).get("sha256", "")) == 64
                  and len(report.get("equity_curve") or []) == 135,
                  f"status={response.status_code} job={job.get('status')} "
                  f"trades={metrics.get('trade_count')}")
            trades = report.get("trades") or []
            if trades:
                first = trades[0]
                check("api.backtest_next_open_and_costs",
                      _utc(first.get("source_bar_time")) == "2026-01-06T08:30:00+00:00"
                      and _utc(first.get("entry_time")) == "2026-01-06T08:45:00+00:00"
                      and Decimal(first["entry_price"])
                          == Decimal(bars_data[131]["open"]) + Decimal("0.00007")
                      and Decimal(first["costs"]["commission"]) > 0
                      and Decimal(metrics["cost_breakdown"]["spread"]) > 0
                      and Decimal(metrics["cost_breakdown"]["slippage"]) > 0,
                      f"entry={first.get('entry_price')} costs={first.get('costs')}")
            else:
                check("api.backtest_next_open_and_costs", False, "no closed trades")
        else:
            check("api.backtest_report", False, "ma_cross instance or bars missing")

        # ---- AI analysis engine (PRD 8, ADVISORY): analyses are advisory ----
        # display artifacts: strict doc-8.4 shape inside output_json, the OFF
        # fallback is configured but the run mode restored to ADVISORY
        analyses = client.get(f"{API}/api/v1/accounts/{sim_id}/ai/analyses",
                              params={"symbol": "EURUSD", "timeframe": "M15",
                                      "limit": 10}).json().get("data", [])
        newest_doc = (analyses[0].get("output_json") if analyses else {}) or {}
        check("api.ai_analyses",
              bool(analyses)
              and newest_doc.get("market_regime") in
                  ("TREND_UP", "TREND_DOWN", "RANGE", "HIGH_VOL", "UNKNOWN")
              and 0 <= float(newest_doc.get("regime_confidence", -1)) <= 1
              and newest_doc.get("model_provider") == "statistical-regime"
              and newest_doc.get("signal_decision") in ("ALLOW", "REVIEW")
              and isinstance(newest_doc.get("strategy_scores"), list)
              and str(newest_doc.get("input_snapshot_id", "")).startswith("sha256:"),
              f"count={len(analyses)} regime={newest_doc.get('market_regime')}")
        if analyses:
            print(f"       analysis: regime={newest_doc.get('market_regime')} "
                  f"decision={newest_doc.get('signal_decision')} "
                  f"conf={newest_doc.get('regime_confidence')}")

        ai_cfg = client.get(f"{API}/api/v1/accounts/{sim_id}/config").json()["data"]["config"]
        check("api.ai_mode_advisory",
              (ai_cfg.get("ai") or {}).get("mode") == "ADVISORY",
              f"mode={(ai_cfg.get('ai') or {}).get('mode')}")


        # ---- risk engine (slice 2, PRD 9): stop states + gate audit -------
        st = client.get(f"{API}/api/v1/risk/state")
        st_data = st.json().get("data") or {}
        check("api.risk_state",
              st.status_code == 200 and (st_data.get("global") or {}).get("status") == "NORMAL",
              f"status={st.status_code} global={(st_data.get('global') or {}).get('status')}")
        acc_state = client.get(f"{API}/api/v1/risk/state", params={"account_id": sim_id})
        acc_st = acc_state.json().get("data") or {}
        check("api.risk_state_account",
              acc_state.status_code == 200 and acc_st.get("status") == "NORMAL",
              f"status={acc_state.status_code} state={acc_st.get('status')}")

        # PRD 9.5 six-state breaker: every non-NORMAL state is settable and
        # visible; /stop must refuse de-escalation (resume is the only exit)
        for target in ("WARNING", "CANCEL_PENDING", "MANAGE_EXISTING"):
            r = client.post(f"{API}/api/v1/risk/stop", json={
                "scope": "ACCOUNT", "account_id": sim_id, "status": target,
                "reason": "e2e 9.5 states", "updated_by": "e2e_verify",
            })
            shown = client.get(f"{API}/api/v1/risk/state",
                               params={"account_id": sim_id}).json()["data"]
            check(f"api.risk_state_{target.lower()}",
                  r.status_code == 200 and shown.get("status") == target,
                  f"set={r.status_code} shown={shown.get('status')}")
        r409 = client.post(f"{API}/api/v1/risk/stop", json={
            "scope": "ACCOUNT", "account_id": sim_id, "status": "WARNING",
            "reason": "try weaken", "updated_by": "e2e_verify",
        })
        check("api.risk_stop_refuses_de_escalation", r409.status_code == 409,
              f"status={r409.status_code} body={r409.text[:120]}")
        rr = client.post(f"{API}/api/v1/risk/resume", json={
            "scope": "ACCOUNT", "account_id": sim_id, "updated_by": "e2e_verify",
        })
        back = client.get(f"{API}/api/v1/risk/state",
                          params={"account_id": sim_id}).json()["data"]
        check("api.risk_state_resumed_normal",
              rr.status_code == 200 and back.get("status") == "NORMAL",
              f"resume={rr.status_code} status={back.get('status')}")

        rej = client.get(f"{API}/api/v1/commands",
                         params={"account_id": sim_id, "status": "REJECTED"}).json()["data"]
        check("api.commands_rejected", len(rej) >= 5, f"rejected={len(rej)}")
        if rej:
            one = client.get(f"{API}/api/v1/commands/{rej[0]['command_id']}").json()["data"]
            rd = one.get("risk_decision") or {}
            check("api.risk_decision_attached",
                  rd.get("decision") == "REJECT" and bool(rd.get("reason_codes")),
                  f"decision={rd.get('decision')} reasons={rd.get('reason_codes')}")
            print(f"       rejected command: {one.get('command_type')} "
                  f"reasons={rd.get('reason_codes')}")

        decs = client.get(f"{API}/api/v1/risk/decisions",
                          params={"account_id": sim_id}).json()["data"]
        check("api.risk_decisions", len(decs) >= 10, f"decisions={len(decs)}")
        allows = [d for d in decs if d.get("decision") == "ALLOW"]
        check("api.risk_decisions_allow", len(allows) >= 4, f"allows={len(allows)}")

        cfg = client.get(f"{API}/api/v1/accounts/{sim_id}/config").json()["data"]["config"]
        check("api.config_risk_section",
              cfg.get("risk", {}).get("per_trade_risk_fraction") == "0"
              and cfg.get("risk", {}).get("require_stop_loss") is True,
              f"per_trade={cfg.get('risk', {}).get('per_trade_risk_fraction')}")

    # ---- isolated strategy automation account (PRD 3.2/9.3/13.4) ----------
    auto = next((a for a in accounts if a.get("login_ref") == "10005678"), None)
    check("automation.account_present", auto is not None)
    auto_id = auto["account_id"] if auto else None
    if auto:
        auto_cfg = client.get(f"{API}/api/v1/accounts/{auto_id}/config").json()["data"]["config"]
        check("automation.safe_config_restored",
              (auto_cfg.get("automation") or {}).get("command_generation") is False
              and (auto_cfg.get("risk") or {}).get("per_trade_risk_fraction") == "0",
              f"automation={auto_cfg.get('automation')} risk={(auto_cfg.get('risk') or {}).get('per_trade_risk_fraction')}")

        auto_rows = client.get(f"{API}/api/v1/commands", params={
            "account_id": auto_id, "limit": 500,
        }).json().get("data", [])
        auto_rows = [row for row in auto_rows if row.get("created_by") == "signal-aggregator"]
        allowed = next((row for row in auto_rows if row.get("status") != "REJECTED"), None)
        rejected = next((row for row in auto_rows if row.get("status") == "REJECTED"), None)

        def expected_command(bar_time: str) -> str:
            key = ":".join((
                "quantpilot-signal-group-v1", auto_id, "EURUSD", "M15", bar_time, "BUY",
            ))
            return str(uuid.uuid5(uuid.NAMESPACE_URL, key))

        expected_ids = {
            expected_command("2026-01-06T09:30:00+00:00"),
            expected_command("2026-01-06T09:00:00+00:00"),
        }
        check("automation.deterministic_replay",
              len(auto_rows) == 2 and {row["command_id"] for row in auto_rows} == expected_ids,
              f"commands={len(auto_rows)} ids={[row['command_id'] for row in auto_rows]}")
        if allowed:
            strong_signal = str(uuid.uuid5(
                uuid.NAMESPACE_URL, f"quantpilot-e2e:{auto_id}:134:trend_following:BUY",
            ))
            volume = Decimal(allowed["payload"]["volume"])
            detail = client.get(f"{API}/api/v1/commands/{allowed['command_id']}").json()["data"]
            check("automation.aggregated_allowed",
                  allowed.get("status") == "ACKED"
                  and allowed.get("signal_id") == strong_signal
                  and volume > 0 and volume == volume.quantize(Decimal("0.01"))
                  and (detail.get("risk_decision") or {}).get("decision") == "ALLOW",
                  f"status={allowed.get('status')} volume={volume} risk={detail.get('risk_decision')}")
        else:
            check("automation.aggregated_allowed", False, "ALLOW command missing")
        if rejected:
            detail = client.get(f"{API}/api/v1/commands/{rejected['command_id']}").json()["data"]
            check("automation.rejected_terminal",
                  rejected.get("delivered_at") is None
                  and rejected.get("delivery_attempts") == 0
                  and (detail.get("risk_decision") or {}).get("reason_codes")
                      == ["ACCOUNT_STOP_NEW_RISK"],
                  f"status={rejected.get('status')} risk={detail.get('risk_decision')}")
        else:
            check("automation.rejected_terminal", False, "REJECTED command missing")

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

        # ---- 4. Risk gate invariants (PRD 9, doc 12.2) ---------------------
        rejected = conn.execute(
            "SELECT count(*) FROM commands WHERE status = 'REJECTED'"
        ).fetchone()[0]
        check("pg.commands_rejected", rejected >= 5, f"rejected={rejected}")

        # every REJECT decision pairs with a REJECTED command (no REJECT
        # command ever reaches QUEUED/DELIVERED, and no ALLOW lands REJECTED)
        mismatched = conn.execute(
            """
            SELECT count(*) FROM risk_decisions d
              JOIN commands c ON c.command_id = d.command_id
             WHERE (d.decision = 'REJECT' AND c.status <> 'REJECTED')
                OR (d.decision = 'ALLOW' AND c.status = 'REJECTED')
            """
        ).fetchone()[0]
        check("pg.risk_decision_command_consistent", mismatched == 0,
              f"mismatches={mismatched}")

        orphan_decisions = conn.execute(
            """
            SELECT count(*) FROM risk_decisions d
              LEFT JOIN commands c ON c.command_id = d.command_id
             WHERE c.command_id IS NULL
            """
        ).fetchone()[0]
        check("pg.risk_decisions_have_commands", orphan_decisions == 0,
              f"orphans={orphan_decisions}")

        # REJECTED is terminal: never delivered, no attempts, never acked
        leaked_rejected = conn.execute(
            """
            SELECT count(*) FROM commands
             WHERE status = 'REJECTED'
               AND (delivered_at IS NOT NULL OR delivery_attempts > 0
                    OR acked_at IS NOT NULL)
            """
        ).fetchone()[0]
        check("pg.rejected_never_delivered", leaked_rejected == 0,
              f"leaked={leaked_rejected}")

        risk_events = conn.execute(
            "SELECT count(*) FROM system_events WHERE event_type LIKE 'risk.%'"
        ).fetchone()[0]
        check("pg.risk_events", risk_events >= 4, f"events={risk_events}")

        dup_decisions = conn.execute(
            """
            SELECT count(*) FROM (
                SELECT command_id FROM risk_decisions
                 GROUP BY command_id HAVING count(*) > 1
            ) d
            """
        ).fetchone()[0]
        check("pg.risk_decisions_unique", dup_decisions == 0,
              f"duplicate_decisions={dup_decisions}")

        # ---- 5. Strategy engine invariants (PRD 7.4/7.5) --------------------
        # one advisory signal per (instance, source bar) even after batch
        # redeliveries; poisoned bars are dropped and reported, not retried
        dup_signals = conn.execute(
            """
            SELECT count(*) FROM (
                SELECT strategy_instance_id, source_bar_time FROM strategy_signals
                 GROUP BY strategy_instance_id, source_bar_time HAVING count(*) > 1
            ) d
            """
        ).fetchone()[0]
        check("pg.signals_dedup", dup_signals == 0, f"duplicates={dup_signals}")

        orphan_signals = conn.execute(
            """
            SELECT count(*) FROM strategy_signals s
             LEFT JOIN strategy_instances i ON i.id = s.strategy_instance_id
             WHERE i.id IS NULL
            """
        ).fetchone()[0]
        check("pg.signals_have_instances", orphan_signals == 0,
              f"orphans={orphan_signals}")

        bar_rows = conn.execute(
            "SELECT count(*) FROM symbol_bars WHERE timeframe = 'M15'"
        ).fetchone()[0]
        check("pg.symbol_bars", bar_rows >= 135, f"rows={bar_rows}")

        dropped = conn.execute(
            "SELECT count(*) FROM system_events WHERE event_type = 'ea.bars_dropped'"
        ).fetchone()[0]
        check("pg.bars_dropped_event", dropped >= 1, f"events={dropped}")

        # ---- 6. AI engine invariants (PRD 8): input-hash idempotency + FKs --
        dup_analyses = conn.execute(
            """
            SELECT count(*) FROM (
                SELECT account_id, broker_symbol, timeframe, input_hash FROM ai_analyses
                 GROUP BY account_id, broker_symbol, timeframe, input_hash HAVING count(*) > 1
            ) d
            """
        ).fetchone()[0]
        check("pg.ai_analyses_dedup", dup_analyses == 0, f"duplicates={dup_analyses}")

        orphan_analyses = conn.execute(
            """
            SELECT count(*) FROM ai_analyses a
             LEFT JOIN trading_accounts t ON t.id = a.account_id
             WHERE t.id IS NULL
            """
        ).fetchone()[0]
        check("pg.ai_analyses_have_accounts", orphan_analyses == 0,
              f"orphans={orphan_analyses}")

        # ---- 7. Backtest invariants (PRD 15): replay-safe and isolated -----
        dup_jobs = conn.execute(
            """
            SELECT count(*) FROM (
                SELECT idempotency_key FROM backtest_jobs
                 GROUP BY idempotency_key HAVING count(*) > 1
            ) d
            """
        ).fetchone()[0]
        check("pg.backtests_dedup", dup_jobs == 0, f"duplicates={dup_jobs}")
        incomplete = conn.execute(
            """
            SELECT count(*) FROM backtest_jobs j
             LEFT JOIN backtest_results r ON r.job_id = j.id
             WHERE j.status = 'COMPLETED'
               AND (r.id IS NULL OR j.data_hash IS NULL OR length(j.data_hash) <> 64)
            """
        ).fetchone()[0]
        check("pg.backtests_complete_reports", incomplete == 0, f"incomplete={incomplete}")
        leaked = conn.execute(
            "SELECT count(*) FROM commands WHERE created_by LIKE 'backtest%'"
        ).fetchone()[0]
        check("pg.backtests_create_no_commands", leaked == 0, f"commands={leaked}")

        # ---- 8. Strategy automation invariants (PRD 3.2/7.5/9.3) -----------
        if auto_id:
            automation_rows = conn.execute(
                """
                SELECT c.command_id::text, c.status, d.decision, d.risk_snapshot
                  FROM commands c
                  JOIN risk_decisions d ON d.command_id = c.command_id
                 WHERE c.account_id = %s AND c.created_by = 'signal-aggregator'
                 ORDER BY c.created_at
                """,
                (auto_id,),
            ).fetchall()
            check("pg.automation_commands_and_decisions_unique",
                  len(automation_rows) == 2
                  and len({row[0] for row in automation_rows}) == 2,
                  f"rows={len(automation_rows)}")
            allow_row = next((row for row in automation_rows if row[2] == "ALLOW"), None)
            if allow_row:
                snapshot = allow_row[3]
                ai = snapshot.get("ai") or {}
                expected_signals = sorted(
                    str(uuid.uuid5(
                        uuid.NAMESPACE_URL, f"quantpilot-e2e:{auto_id}:134:{code}:BUY",
                    ))
                    for code in ("ma_cross", "trend_following")
                )
                check("pg.automation_contributors_and_ai_context",
                      snapshot.get("signal_ids") == expected_signals
                      and bool(ai.get("analysis_id"))
                      and ai.get("model_provider") == "statistical-regime"
                      and ai.get("signal_decision") == "REVIEW"
                      and _utc(ai.get("source_bar_time")) == "2026-01-06T09:30:00+00:00"
                      and (snapshot.get("thresholds") or {}).get("per_trade_risk_fraction") == "0.01",
                      f"signals={snapshot.get('signal_ids')} ai={ai}")
                check("pg.automation_ai_is_advisory",
                      allow_row[1] == "ACKED" and allow_row[2] == "ALLOW"
                      and ai.get("signal_decision") == "REVIEW",
                      f"command={allow_row[1]} risk={allow_row[2]} ai={ai.get('signal_decision')}")
            else:
                check("pg.automation_contributors_and_ai_context", False, "ALLOW decision missing")
                check("pg.automation_ai_is_advisory", False, "ALLOW decision missing")

            conflict_ids = sorted((
                str(uuid.uuid5(uuid.NAMESPACE_URL, f"quantpilot-e2e:{auto_id}:133:ma_cross:BUY")),
                str(uuid.uuid5(uuid.NAMESPACE_URL, f"quantpilot-e2e:{auto_id}:133:trend_following:SELL")),
            ))
            conflict = conn.execute(
                """
                SELECT payload FROM system_events
                 WHERE event_type = 'signal.conflict'
                   AND payload->>'account_id' = %s
                   AND payload->>'source_bar_time' = '2026-01-06T09:15:00+00:00'
                 ORDER BY occurred_at DESC LIMIT 1
                """,
                (auto_id,),
            ).fetchone()
            check("pg.automation_conflict_audited",
                  conflict is not None and (conflict[0].get("signal_ids") == conflict_ids),
                  f"event={conflict[0] if conflict else None}")

            sizing_failure = conn.execute(
                """
                SELECT payload FROM system_events
                 WHERE event_type = 'signal.command_failed'
                   AND payload->>'account_id' = %s
                   AND payload->>'source_bar_time' = '2026-01-06T09:30:00+00:00'
                   AND payload->>'reason' = 'RISK_NOT_CONFIGURED'
                 ORDER BY occurred_at DESC LIMIT 1
                """,
                (auto_id,),
            ).fetchone()
            check("pg.automation_zero_risk_audited", sizing_failure is not None,
                  f"event={sizing_failure[0] if sizing_failure else None}")

    return finish()


def finish() -> int:
    print(f"\ne2e failures: {failures}")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
