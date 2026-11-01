"""R3 acceptance P2-04: REAL-database cross-user authorization tests.

The mocked suites (test_r2_remediation / test_r3_remediation) prove the
decision logic; this module proves the SAME guarantees against the real
PostgreSQL schema with real bcrypt, real SQL and real durable rows:

    user A   TRADER   -> granted account A only
    user B   TRADER   -> granted account B only
    user C   VIEWER   -> granted account A only
    user D   ADMIN    -> everything

node1/terminal1 host BOTH accounts; node2/terminal2 host none (pure
infrastructure -> must be invisible to non-admins, R3 P2-01).

Covers the 17 mandated scenarios from the R3 instructions (executed on
real data, not mocks):

 #1  A reads account A only
 #2  A cannot read account B
 #3  A cannot place orders on account B
 #4  A cannot read account B's commands
 #5  A cannot query account B's backtests
 #6  A cannot modify account B's risk state
 #7  A cannot read account B's audit logs
 #8  VIEWER cannot create trade commands
 #9  VIEWER cannot modify risk config
 #10 B cannot see A's login events
 #11 regular users cannot read global nodes/terminals
 #12 disabling a user kills live JWTs immediately
 #13 changing a role kills old JWTs immediately
 #14 revoking a personal API key kills it immediately
 #15 the service key cannot reach business routes
 #16 the service key still drives the agent/EA machine lane
 #17 ungranted account ids cannot bypass via query/body/URL/linked objects

Gated behind QP_RUN_DB_TESTS (needs the dev PostgreSQL tunnel up):

    $env:QP_RUN_DB_TESTS="1"
    .\\.venv\\Scripts\\python -m unittest discover -s services\\api\\tests -p "test_r3_integration.py" -v
"""

import os
import time
import unittest
from unittest.mock import patch
from uuid import uuid4

from fastapi.testclient import TestClient

from quantpilot_api import auth, db

PASSWORD = "r3it-Integration-Password-1"
PREFIX = f"r3it{int(time.time())}"
DB_GATE = os.environ.get("QP_RUN_DB_TESTS") and os.environ.get("QP_PG_DSN")


def _one(sql: str, params: tuple = ()) -> dict:
    row = db.query_one(sql, params)
    assert row is not None, sql
    return row


def _command_body(account_id: str) -> dict:
    return {
        "command_id": str(uuid4()),
        "account_id": account_id,
        "command_type": "RECONCILE",
        "expires_at": "2026-12-31T00:00:00+00:00",
        "payload": {},
    }


@unittest.skipUnless(DB_GATE, "set QP_RUN_DB_TESTS=1 with the dev DB tunnel up")
class CrossTenantIntegrationTests(unittest.TestCase):
    """All 17 scenarios, one real DB, zero mocks on the request path."""

    @classmethod
    def setUpClass(cls) -> None:
        cls.env = patch.dict(
            os.environ,
            {
                "QP_API_KEY": f"{PREFIX}-svc-key",
                "QP_JWT_SECRET": f"{PREFIX}-jwt-secret",
                "QP_LOGIN_THROTTLE_REDIS_URL": "",  # memory_only throttle; no redis needed
                "QP_LOGIN_THROTTLE_MODE": "memory_only",
            },
        )
        cls.env.start()
        cls.addClassCleanup(cls.env.stop)
        cls.addClassCleanup(cls._teardown)
        import quantpilot_api.app as app_module

        cls.client = TestClient(app_module.app)

        # ---- infrastructure: node1/terminal1 host both accounts;
        # node2/terminal2 host NOTHING (pure infra, admin-only visibility)
        cls.node1 = _one(
            "INSERT INTO trading_nodes (device_id, hostname, os_info, agent_version, last_seen_at)"
            " VALUES (%s, %s, %s, %s, now()) RETURNING id::text AS id",
            (f"{PREFIX}-node1", f"{PREFIX}-host1", "windows", "0.1.0"),
        )["id"]
        cls.node2 = _one(
            "INSERT INTO trading_nodes (device_id, hostname, os_info, agent_version, last_seen_at)"
            " VALUES (%s, %s, %s, %s, now()) RETURNING id::text AS id",
            (f"{PREFIX}-node2", f"{PREFIX}-host2", "windows", "0.1.0"),
        )["id"]
        cls.term1 = _one(
            "INSERT INTO mt5_terminals (node_id, terminal_path_hash, last_seen_at)"
            " VALUES (%s, %s, now()) RETURNING id::text AS id",
            (cls.node1, f"{PREFIX}-term1"),
        )["id"]
        cls.term2 = _one(
            "INSERT INTO mt5_terminals (node_id, terminal_path_hash, last_seen_at)"
            " VALUES (%s, %s, now()) RETURNING id::text AS id",
            (cls.node2, f"{PREFIX}-term2"),
        )["id"]

        # ---- accounts A and B, both on terminal1 (isolation must come
        # from grants, never from terminal topology)
        cls.acct_a = _one(
            "INSERT INTO trading_accounts (terminal_id, login_ref, server, last_seen_at)"
            " VALUES (%s, %s, %s, now()) RETURNING id::text AS id",
            (cls.term1, f"{PREFIX}-login-a", "QuantPilot-Demo"),
        )["id"]
        cls.acct_b = _one(
            "INSERT INTO trading_accounts (terminal_id, login_ref, server, last_seen_at)"
            " VALUES (%s, %s, %s, now()) RETURNING id::text AS id",
            (cls.term1, f"{PREFIX}-login-b", "QuantPilot-Demo"),
        )["id"]

        # ---- users A/B/C/D with real bcrypt hashes
        password_hash = auth.hash_password(PASSWORD).decode("ascii")

        def _user(name: str, role: str) -> str:
            return _one(
                "INSERT INTO users (username, password_hash, role)"
                " VALUES (%s, %s, %s) RETURNING id::text AS id",
                (name, password_hash, role),
            )["id"]

        cls.user_a = _user(f"{PREFIX}-userA", "TRADER")
        cls.user_b = _user(f"{PREFIX}-userB", "TRADER")
        cls.user_c = _user(f"{PREFIX}-userC", "VIEWER")
        cls.user_d = _user(f"{PREFIX}-userD", "ADMIN")
        for user_id, account_id in (
            (cls.user_a, cls.acct_a),
            (cls.user_b, cls.acct_b),
            (cls.user_c, cls.acct_a),
        ):
            db.execute(
                "INSERT INTO user_account_grants (user_id, account_id) VALUES (%s, %s)",
                (user_id, account_id),
            )

        # ---- account-B-owned research objects (linked-object bypass
        # targets: instance -> account, job -> account)
        sv = _one("SELECT id::text AS id FROM strategy_versions ORDER BY created_at LIMIT 1")["id"]
        cls.instance_b = _one(
            "INSERT INTO strategy_instances (account_id, strategy_version_id, broker_symbol,"
            " timeframe, config) VALUES (%s, %s, %s, %s, CAST(%s AS jsonb))"
            " RETURNING id::text AS id",
            (cls.acct_b, sv, "EURUSD", "M15", "{}"),
        )["id"]
        cls.job_b = _one(
            "INSERT INTO backtest_jobs (id, strategy_version_id, strategy_instance_id,"
            " account_id, broker_symbol, timeframe, range_start, range_end, config,"
            " config_revision, cost_model, initial_balance, volume, idempotency_key,"
            " request_json, request_hash)"
            " VALUES (%s, %s, %s, %s, %s, %s, now() - interval '30 days', now(),"
            " CAST(%s AS jsonb), 0, CAST(%s AS jsonb), 10000, 1, %s, CAST(%s AS jsonb), %s)"
            " RETURNING id::text AS id",
            (str(uuid4()), sv, cls.instance_b, cls.acct_b, "EURUSD", "M15",
             "{}", "{}", str(uuid4()), "{}", f"{PREFIX}-hash"),
        )["id"]

        # ---- durable commands, one per account
        cls.cmd_b = _one(
            "INSERT INTO commands (command_id, account_id, command_type, payload, status,"
            " expires_at) VALUES (%s, %s, %s, CAST(%s AS jsonb), 'ACKED',"
            " now() + interval '1 hour') RETURNING command_id::text AS command_id",
            (str(uuid4()), cls.acct_b, "RECONCILE", "{}"),
        )["command_id"]
        cls.cmd_a = _one(
            "INSERT INTO commands (command_id, account_id, command_type, payload, status,"
            " expires_at) VALUES (%s, %s, %s, CAST(%s AS jsonb), 'ACKED',"
            " now() + interval '1 hour') RETURNING command_id::text AS command_id",
            (str(uuid4()), cls.acct_a, "RECONCILE", "{}"),
        )["command_id"]

        # ---- an audit row on account B written by a B-side actor
        db.execute(
            "INSERT INTO audit_logs (account_id, actor, action, object_type)"
            " VALUES (%s, %s, %s, %s)",
            (cls.acct_b, f"{PREFIX}-actorB", "RISK_STOP", "risk_state"),
        )

    @classmethod
    def _teardown(cls) -> None:
        users = [u for u in (getattr(cls, k, None) for k in
                 ("user_a", "user_b", "user_c", "user_d")) if u]
        accts = [a for a in (getattr(cls, k, None) for k in ("acct_a", "acct_b")) if a]
        terms = [t for t in (getattr(cls, k, None) for k in ("term1", "term2")) if t]

        def _del(sql: str, params: tuple) -> None:
            try:
                db.execute(sql, params)
            except Exception as exc:  # noqa: BLE001 - cleanup must not mask results
                print(f"[r3it cleanup] {sql[:60]}...: {exc}")

        _del(
            "DELETE FROM system_events WHERE payload::text LIKE %s"
            " OR payload->>'user_id' = ANY(%s) OR account_id::text = ANY(%s)",
            (f"%{PREFIX}%", users, accts),
        )
        _del(
            "DELETE FROM audit_logs WHERE actor LIKE %s OR account_id::text = ANY(%s)",
            (f"{PREFIX}%", accts),
        )
        _del(
            "DELETE FROM risk_states WHERE account_id::text = ANY(%s) AND updated_by LIKE %s",
            (accts, f"{PREFIX}%"),
        )
        _del("DELETE FROM commands WHERE account_id::text = ANY(%s)", (accts,))
        _del("DELETE FROM backtest_jobs WHERE account_id::text = ANY(%s)", (accts,))
        _del("DELETE FROM strategy_instances WHERE account_id::text = ANY(%s)", (accts,))
        if users:
            _del("DELETE FROM user_api_keys WHERE user_id::text = ANY(%s)", (users,))
            _del("DELETE FROM user_account_grants WHERE user_id::text = ANY(%s)", (users,))
            _del("DELETE FROM users WHERE id::text = ANY(%s)", (users,))
        if accts:
            _del("DELETE FROM trading_accounts WHERE id::text = ANY(%s)", (accts,))
        if terms:
            _del("DELETE FROM mt5_terminals WHERE id::text = ANY(%s)", (terms,))
        _del("DELETE FROM trading_nodes WHERE device_id LIKE %s", (f"{PREFIX}%",))
        db.close_pool()

    # ------------------------------------------------------------------
    # helpers
    # ------------------------------------------------------------------

    def _login(self, username: str) -> dict:
        resp = self.client.post(
            "/api/v1/auth/login", json={"username": username, "password": PASSWORD}
        )
        self.assertEqual(resp.status_code, 200, resp.text)
        return {"Authorization": f"Bearer {resp.json()['data']['token']}"}

    def _svc(self) -> dict:
        return {"X-API-Key": os.environ["QP_API_KEY"]}

    def _count(self, sql: str, params: tuple) -> int:
        return int(db.query_one(sql, params)["n"])

    # ------------------------------------------------------------------
    # scenarios #1 + #2
    # ------------------------------------------------------------------

    def test_01_account_list_and_detail_isolation(self) -> None:
        a = self._login(f"{PREFIX}-userA")
        resp = self.client.get("/api/v1/accounts", headers=a)
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(
            {row["account_id"] for row in resp.json()["data"]}, {self.acct_a}
        )
        # granted account: past the gate (404 = no snapshots yet, NOT 403)
        allowed = self.client.get(f"/api/v1/accounts/{self.acct_a}/snapshot", headers=a)
        self.assertEqual(allowed.status_code, 404, allowed.text)
        # foreign account: the URL-path grant check fires (#2, #17-URL)
        denied = self.client.get(f"/api/v1/accounts/{self.acct_b}/snapshot", headers=a)
        self.assertEqual(denied.status_code, 403)
        self.assertEqual(denied.json()["error"]["code"], "ACCOUNT_NOT_GRANTED")
        # VIEWER C is equally confined to account A
        c = self._login(f"{PREFIX}-userC")
        resp = self.client.get("/api/v1/accounts", headers=c)
        self.assertEqual({r["account_id"] for r in resp.json()["data"]}, {self.acct_a})
        # ADMIN D sees both (plus any pre-existing dev accounts)
        d = self._login(f"{PREFIX}-userD")
        resp = self.client.get("/api/v1/accounts", headers=d)
        self.assertLessEqual(
            {self.acct_a, self.acct_b}, {r["account_id"] for r in resp.json()["data"]}
        )

    # ------------------------------------------------------------------
    # scenario #3
    # ------------------------------------------------------------------

    def test_02_cannot_command_foreign_account(self) -> None:
        before = self._count(
            "SELECT count(*) AS n FROM commands WHERE account_id::text = %s", (self.acct_b,)
        )
        a = self._login(f"{PREFIX}-userA")
        resp = self.client.post(
            "/api/v1/commands", json=_command_body(self.acct_b), headers=a
        )
        self.assertEqual(resp.status_code, 403, resp.text)
        self.assertEqual(resp.json()["error"]["code"], "FORBIDDEN")
        # nothing landed in the durable channel for the foreign account
        after = self._count(
            "SELECT count(*) AS n FROM commands WHERE account_id::text = %s", (self.acct_b,)
        )
        self.assertEqual(after, before)

    # ------------------------------------------------------------------
    # scenario #4 (+ #17 query/URL channels)
    # ------------------------------------------------------------------

    def test_03_command_read_isolation(self) -> None:
        a = self._login(f"{PREFIX}-userA")
        # URL: foreign command by id
        resp = self.client.get(f"/api/v1/commands/{self.cmd_b}", headers=a)
        self.assertEqual(resp.status_code, 403, resp.text)
        # query param: explicit foreign account filter
        resp = self.client.get(f"/api/v1/commands?account_id={self.acct_b}", headers=a)
        self.assertEqual(resp.status_code, 403, resp.text)
        # unfiltered list: narrowed silently to granted accounts
        resp = self.client.get("/api/v1/commands", headers=a)
        self.assertEqual(resp.status_code, 200)
        ids = {row["command_id"] for row in resp.json()["data"]}
        self.assertIn(self.cmd_a, ids)
        self.assertNotIn(self.cmd_b, ids)
        # positive: A reads its own command by id
        resp = self.client.get(f"/api/v1/commands/{self.cmd_a}", headers=a)
        self.assertEqual(resp.status_code, 200, resp.text)
        # ADMIN sees the foreign command + filter
        d = self._login(f"{PREFIX}-userD")
        resp = self.client.get(
            f"/api/v1/commands?account_id={self.acct_b}", headers=d
        )
        self.assertEqual(resp.status_code, 200)
        self.assertIn(self.cmd_b, {r["command_id"] for r in resp.json()["data"]})

    # ------------------------------------------------------------------
    # scenario #5 (+ #17 linked-object channel)
    # ------------------------------------------------------------------

    def test_04_backtest_isolation(self) -> None:
        a = self._login(f"{PREFIX}-userA")
        # URL: foreign job by id
        resp = self.client.get(f"/api/v1/backtests/{self.job_b}", headers=a)
        self.assertEqual(resp.status_code, 403, resp.text)
        # query param: foreign account filter
        resp = self.client.get(f"/api/v1/backtests?account_id={self.acct_b}", headers=a)
        self.assertEqual(resp.status_code, 403, resp.text)
        # linked object: body.strategy_instance_id resolves to account B
        before = self._count(
            "SELECT count(*) AS n FROM backtest_jobs WHERE strategy_instance_id::text = %s",
            (self.instance_b,),
        )
        resp = self.client.post(
            "/api/v1/backtests",
            json={
                "strategy_instance_id": self.instance_b,
                "range_start": "2026-10-01T00:00:00+00:00",
                "range_end": "2026-10-31T00:00:00+00:00",
                "initial_balance": "10000",
                "volume": "1",
                "cost_model": {},
            },
            headers={**a, "Idempotency-Key": str(uuid4())},
        )
        self.assertEqual(resp.status_code, 403, resp.text)
        after = self._count(
            "SELECT count(*) AS n FROM backtest_jobs WHERE strategy_instance_id::text = %s",
            (self.instance_b,),
        )
        self.assertEqual(after, before)
        # positive: A's own account filter is allowed (empty result set)
        resp = self.client.get(f"/api/v1/backtests?account_id={self.acct_a}", headers=a)
        self.assertEqual(resp.status_code, 200, resp.text)
        # ADMIN reaches the foreign job
        d = self._login(f"{PREFIX}-userD")
        resp = self.client.get(f"/api/v1/backtests/{self.job_b}", headers=d)
        self.assertEqual(resp.status_code, 200, resp.text)

    # ------------------------------------------------------------------
    # scenario #6 + R3 P2-02 identity stamping
    # ------------------------------------------------------------------

    def test_05_risk_state_isolation_and_principal_identity(self) -> None:
        a = self._login(f"{PREFIX}-userA")
        # foreign account: refused, nothing written
        resp = self.client.post(
            "/api/v1/risk/stop",
            json={"scope": "ACCOUNT", "account_id": self.acct_b, "reason": f"{PREFIX} stop"},
            headers=a,
        )
        self.assertEqual(resp.status_code, 403, resp.text)
        self.assertEqual(
            self._count(
                "SELECT count(*) AS n FROM risk_states WHERE account_id::text = %s",
                (self.acct_b,),
            ),
            0,
        )
        # forged updated_by in the body is rejected outright (extra=forbid)
        forged = self.client.post(
            "/api/v1/risk/stop",
            json={
                "scope": "ACCOUNT",
                "account_id": self.acct_a,
                "reason": f"{PREFIX} stop",
                "updated_by": "forged-operator",
            },
            headers=a,
        )
        self.assertEqual(forged.status_code, 422, forged.text)
        # own account: allowed; identity comes from the principal (P2-02)
        resp = self.client.post(
            "/api/v1/risk/stop",
            json={"scope": "ACCOUNT", "account_id": self.acct_a, "reason": f"{PREFIX} stop"},
            headers=a,
        )
        self.assertEqual(resp.status_code, 200, resp.text)
        row = db.query_one(
            "SELECT status, updated_by FROM risk_states WHERE account_id::text = %s",
            (self.acct_a,),
        )
        assert row is not None
        self.assertEqual(row["status"], "STOP_NEW_RISK")
        self.assertEqual(row["updated_by"], f"{PREFIX}-userA")
        audit_row = db.query_one(
            "SELECT actor FROM audit_logs WHERE account_id::text = %s AND action = 'RISK_STOP'",
            (self.acct_a,),
        )
        assert audit_row is not None
        self.assertEqual(audit_row["actor"], f"{PREFIX}-userA")
        event_row = db.query_one(
            "SELECT payload->>'updated_by' AS updated_by FROM system_events"
            " WHERE account_id::text = %s AND event_type = 'risk.stop_set'",
            (self.acct_a,),
        )
        assert event_row is not None
        self.assertEqual(event_row["updated_by"], f"{PREFIX}-userA")
        # resume back to NORMAL (also stamps the principal identity)
        resp = self.client.post(
            "/api/v1/risk/resume",
            json={"scope": "ACCOUNT", "account_id": self.acct_a},
            headers=a,
        )
        self.assertEqual(resp.status_code, 200, resp.text)
        audit_resume = db.query_one(
            "SELECT actor FROM audit_logs WHERE account_id::text = %s AND action = 'RISK_RESUME'",
            (self.acct_a,),
        )
        assert audit_resume is not None
        self.assertEqual(audit_resume["actor"], f"{PREFIX}-userA")

    # ------------------------------------------------------------------
    # scenario #7
    # ------------------------------------------------------------------

    def test_06_audit_log_isolation(self) -> None:
        a = self._login(f"{PREFIX}-userA")
        resp = self.client.get(f"/api/v1/audit-logs?account_id={self.acct_b}", headers=a)
        self.assertEqual(resp.status_code, 403, resp.text)
        # unfiltered view: narrowed, no account-B rows, no B-side actor
        resp = self.client.get("/api/v1/audit-logs", headers=a)
        self.assertEqual(resp.status_code, 200)
        rows = resp.json()["data"]
        self.assertTrue(all(r["account_id"] != self.acct_b for r in rows))
        self.assertNotIn(f"{PREFIX}-actorB", {r.get("actor") for r in rows})
        # ADMIN sees the account-B audit trail
        d = self._login(f"{PREFIX}-userD")
        resp = self.client.get(f"/api/v1/audit-logs?account_id={self.acct_b}", headers=d)
        self.assertEqual(resp.status_code, 200)
        self.assertIn(f"{PREFIX}-actorB", {r.get("actor") for r in resp.json()["data"]})

    # ------------------------------------------------------------------
    # scenarios #8 + #9
    # ------------------------------------------------------------------

    def test_07_viewer_write_denied(self) -> None:
        c = self._login(f"{PREFIX}-userC")
        # #8: VIEWER cannot create trade commands - even on a GRANTED account
        resp = self.client.post(
            "/api/v1/commands", json=_command_body(self.acct_a), headers=c
        )
        self.assertEqual(resp.status_code, 403, resp.text)
        self.assertEqual(resp.json()["error"]["code"], "ROLE_FORBIDDEN")
        # #9: VIEWER cannot modify risk state (account or global)
        resp = self.client.post(
            "/api/v1/risk/stop",
            json={"scope": "ACCOUNT", "account_id": self.acct_a, "reason": f"{PREFIX} stop"},
            headers=c,
        )
        self.assertEqual(resp.status_code, 403, resp.text)
        resp = self.client.post(
            "/api/v1/risk/stop", json={"scope": "GLOBAL", "reason": f"{PREFIX} stop"}, headers=c
        )
        self.assertEqual(resp.status_code, 403, resp.text)
        # #9: VIEWER cannot change account config either
        resp = self.client.put(f"/api/v1/accounts/{self.acct_a}/config", json={}, headers=c)
        self.assertEqual(resp.status_code, 403, resp.text)
        self.assertEqual(
            self._count(
                "SELECT count(*) AS n FROM risk_states WHERE updated_by = %s",
                (f"{PREFIX}-userC",),
            ),
            0,
        )
        # VIEWER read stays allowed on granted accounts
        resp = self.client.get(f"/api/v1/commands?account_id={self.acct_a}", headers=c)
        self.assertEqual(resp.status_code, 200, resp.text)
        # TRADER cannot touch the GLOBAL breaker either (cross-tenant)
        b = self._login(f"{PREFIX}-userB")
        resp = self.client.post(
            "/api/v1/risk/stop", json={"scope": "GLOBAL", "reason": f"{PREFIX} stop"}, headers=b
        )
        self.assertEqual(resp.status_code, 403, resp.text)

    # ------------------------------------------------------------------
    # scenario #10 (+ R3 P2-01 event scoping)
    # ------------------------------------------------------------------

    def test_08_login_events_hidden_from_other_users(self) -> None:
        self._login(f"{PREFIX}-userA")  # fresh login event for A
        b = self._login(f"{PREFIX}-userB")
        resp = self.client.get("/api/v1/events", headers=b)
        self.assertEqual(resp.status_code, 200)
        rows = resp.json()["data"]
        # B has zero account-linked events: every login/global event is
        # invisible and no risk event ever touched account B
        self.assertEqual(rows, [], rows)
        self.assertFalse(
            any(r["event_type"] == "auth.login_succeeded" for r in rows)
        )
        # ADMIN sees A's login event (global events are admin-only)
        d = self._login(f"{PREFIX}-userD")
        resp = self.client.get("/api/v1/events?limit=100", headers=d)
        self.assertEqual(resp.status_code, 200)
        logins = [
            r for r in resp.json()["data"]
            if r["event_type"] == "auth.login_succeeded"
            and r["payload"].get("username") == f"{PREFIX}-userA"
        ]
        self.assertTrue(logins, "ADMIN must see user A's login events")

    # ------------------------------------------------------------------
    # scenario #11 (+ R3 P2-01 infra scoping)
    # ------------------------------------------------------------------

    def test_09_global_infrastructure_admin_only(self) -> None:
        a = self._login(f"{PREFIX}-userA")
        resp = self.client.get("/api/v1/nodes", headers=a)
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(
            {r["device_id"] for r in resp.json()["data"]}, {f"{PREFIX}-node1"}
        )
        resp = self.client.get("/api/v1/terminals", headers=a)
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(
            {r["terminal_id"] for r in resp.json()["data"]}, {self.term1}
        )
        # VIEWER with a grant sees the linked node, never the orphan one
        c = self._login(f"{PREFIX}-userC")
        resp = self.client.get("/api/v1/nodes", headers=c)
        self.assertEqual({r["device_id"] for r in resp.json()["data"]}, {f"{PREFIX}-node1"})
        # ADMIN sees both, including the account-less infrastructure node
        # (plus any pre-existing dev nodes/terminals)
        d = self._login(f"{PREFIX}-userD")
        resp = self.client.get("/api/v1/nodes", headers=d)
        self.assertLessEqual(
            {f"{PREFIX}-node1", f"{PREFIX}-node2"},
            {r["device_id"] for r in resp.json()["data"]},
        )
        resp = self.client.get("/api/v1/terminals", headers=d)
        self.assertLessEqual(
            {self.term1, self.term2}, {r["terminal_id"] for r in resp.json()["data"]}
        )

    # ------------------------------------------------------------------
    # scenario #12
    # ------------------------------------------------------------------

    def test_10_disable_kills_jwt_immediately(self) -> None:
        a = self._login(f"{PREFIX}-userA")
        self.assertEqual(self.client.get("/api/v1/auth/me", headers=a).status_code, 200)
        # non-admin cannot disable users (ADMIN_ONLY)
        b = self._login(f"{PREFIX}-userB")
        denied = self.client.patch(
            f"/api/v1/auth/users/{self.user_a}", json={"is_active": False}, headers=b
        )
        self.assertEqual(denied.status_code, 403, denied.text)
        d = self._login(f"{PREFIX}-userD")
        try:
            resp = self.client.patch(
                f"/api/v1/auth/users/{self.user_a}", json={"is_active": False}, headers=d
            )
            self.assertEqual(resp.status_code, 200, resp.text)
            # the live JWT dies on the very next request
            self.assertEqual(self.client.get("/api/v1/auth/me", headers=a).status_code, 401)
        finally:
            restore = self.client.patch(
                f"/api/v1/auth/users/{self.user_a}", json={"is_active": True}, headers=d
            )
            self.assertEqual(restore.status_code, 200, restore.text)
        # re-login works again after the restore
        a2 = self._login(f"{PREFIX}-userA")
        self.assertEqual(self.client.get("/api/v1/auth/me", headers=a2).status_code, 200)

    # ------------------------------------------------------------------
    # scenario #13
    # ------------------------------------------------------------------

    def test_11_role_change_kills_old_jwt_immediately(self) -> None:
        b = self._login(f"{PREFIX}-userB")
        me = self.client.get("/api/v1/auth/me", headers=b)
        self.assertEqual(me.status_code, 200)
        self.assertEqual(me.json()["data"]["role"], "TRADER")
        d = self._login(f"{PREFIX}-userD")
        try:
            resp = self.client.patch(
                f"/api/v1/auth/users/{self.user_b}", json={"role": "ANALYST"}, headers=d
            )
            self.assertEqual(resp.status_code, 200, resp.text)
            # old JWT dead (token_version bumped + role re-read per request)
            self.assertEqual(self.client.get("/api/v1/auth/me", headers=b).status_code, 401)
        finally:
            restore = self.client.patch(
                f"/api/v1/auth/users/{self.user_b}", json={"role": "TRADER"}, headers=d
            )
            self.assertEqual(restore.status_code, 200, restore.text)
        b2 = self._login(f"{PREFIX}-userB")
        me2 = self.client.get("/api/v1/auth/me", headers=b2)
        self.assertEqual(me2.json()["data"]["role"], "TRADER")

    # ------------------------------------------------------------------
    # scenario #14 (personal API key lifecycle)
    # ------------------------------------------------------------------

    def test_12_api_key_revocation_is_immediate(self) -> None:
        a = self._login(f"{PREFIX}-userA")
        resp = self.client.post(
            "/api/v1/auth/api-keys", json={"label": f"{PREFIX} key"}, headers=a
        )
        self.assertEqual(resp.status_code, 200, resp.text)
        data = resp.json()["data"]
        self.assertTrue(data["key"].startswith("qpk_"))
        key_headers = {"X-API-Key": data["key"]}
        # the key works as a full user credential ...
        me = self.client.get("/api/v1/auth/me", headers=key_headers)
        self.assertEqual(me.status_code, 200)
        self.assertEqual(me.json()["data"]["username"], f"{PREFIX}-userA")
        # ... and inherits the SAME account scoping (no privilege widening)
        resp = self.client.get("/api/v1/accounts", headers=key_headers)
        self.assertEqual({r["account_id"] for r in resp.json()["data"]}, {self.acct_a})
        # revoke -> dead on the very next request
        resp = self.client.delete(f"/api/v1/auth/api-keys/{data['key_id']}", headers=a)
        self.assertEqual(resp.status_code, 200, resp.text)
        self.assertEqual(
            self.client.get("/api/v1/auth/me", headers=key_headers).status_code, 401
        )
        # the JWT is a SEPARATE credential and still valid
        self.assertEqual(self.client.get("/api/v1/auth/me", headers=a).status_code, 200)
        # key metadata shows revoked
        resp = self.client.get("/api/v1/auth/api-keys", headers=a)
        revoked = {r["key_id"]: r for r in resp.json()["data"]}[data["key_id"]]
        self.assertIsNotNone(revoked["revoked_at"])

    # ------------------------------------------------------------------
    # scenarios #15 + #16
    # ------------------------------------------------------------------

    def test_13_service_key_lanes(self) -> None:
        # #15: the shared service key is NOT a user on business routes
        for path in ("/api/v1/commands", "/api/v1/nodes", "/api/v1/events"):
            resp = self.client.get(path, headers=self._svc())
            self.assertEqual(resp.status_code, 403, path)
            self.assertEqual(resp.json()["error"]["code"], "USER_IDENTITY_REQUIRED")
        # #16: the machine lane still works against the real DB
        resp = self.client.post(
            "/api/v1/nodes/register",
            json={"device_id": f"{PREFIX}-node3", "hostname": f"{PREFIX}-host3"},
            headers=self._svc(),
        )
        self.assertEqual(resp.status_code, 200, resp.text)
        self.assertEqual(resp.json()["data"]["device_id"], f"{PREFIX}-node3")
        # and it still reaches the EA command channel (422 = pydantic
        # validation of an empty machine payload, i.e. route reachable)
        resp = self.client.post("/api/v1/commands/pending", json={}, headers=self._svc())
        self.assertEqual(resp.status_code, 422, resp.text)

    # ------------------------------------------------------------------
    # scenario #17: no bypass channel left open
    # ------------------------------------------------------------------

    def test_14_no_bypass_via_query_body_url_or_linked_objects(self) -> None:
        a = self._login(f"{PREFIX}-userA")
        # query parameters
        for path in (
            f"/api/v1/commands?account_id={self.acct_b}",
            f"/api/v1/backtests?account_id={self.acct_b}",
            f"/api/v1/audit-logs?account_id={self.acct_b}",
        ):
            resp = self.client.get(path, headers=a)
            self.assertEqual(resp.status_code, 403, path)
        # request bodies
        resp = self.client.post(
            "/api/v1/commands", json=_command_body(self.acct_b), headers=a
        )
        self.assertEqual(resp.status_code, 403)
        resp = self.client.post(
            "/api/v1/risk/stop",
            json={"scope": "ACCOUNT", "account_id": self.acct_b, "reason": f"{PREFIX} stop"},
            headers=a,
        )
        self.assertEqual(resp.status_code, 403)
        resp = self.client.post(
            "/api/v1/backtests",
            json={
                "strategy_instance_id": self.instance_b,
                "range_start": "2026-10-01T00:00:00+00:00",
                "range_end": "2026-10-31T00:00:00+00:00",
            },
            headers={**a, "Idempotency-Key": str(uuid4())},
        )
        self.assertEqual(resp.status_code, 403)
        # URL paths
        for path in (
            f"/api/v1/accounts/{self.acct_b}/snapshot",
            f"/api/v1/commands/{self.cmd_b}",
            f"/api/v1/backtests/{self.job_b}",
        ):
            resp = self.client.get(path, headers=a)
            self.assertEqual(resp.status_code, 403, path)
        # DB-linked objects: command -> account, instance -> account
        resp = self.client.get(f"/api/v1/commands/{self.cmd_b}/reconcile", headers=a)
        self.assertEqual(resp.status_code, 403, resp.text)
        # after all attempts, account B is untouched
        self.assertEqual(
            self._count(
                "SELECT count(*) AS n FROM commands WHERE account_id::text = %s",
                (self.acct_b,),
            ),
            1,
        )
        self.assertEqual(
            self._count(
                "SELECT count(*) AS n FROM risk_states WHERE account_id::text = %s",
                (self.acct_b,),
            ),
            0,
        )
        self.assertEqual(
            self._count(
                "SELECT count(*) AS n FROM audit_logs WHERE account_id::text = %s"
                " AND actor = %s",
                (self.acct_b, f"{PREFIX}-userA"),
            ),
            0,
        )


if __name__ == "__main__":
    unittest.main()
