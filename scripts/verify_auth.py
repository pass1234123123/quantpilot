"""Live auth acceptance (phase-3 R2) against a RUNNING API + PostgreSQL.

Automates the cloud-side parts of acceptance items v2 #1 (API auth: wrong /
missing credentials, password rotation), #2 (account isolation) and #12
(security: bcrypt at rest, plaintext key shown once, immediate revocation,
auth audit trail).

Fixtures are throwaway users (qp_admin_check / qp_analyst_check /
qp_viewer_check) created - or ROTATED - directly in PostgreSQL with fresh
random passwords each run, so nothing secret is ever committed. The API
reads the users table live, which is exactly what production would do.

Usage: python scripts/verify_auth.py   (API on :8000, PG tunnel up)
"""

from __future__ import annotations

import os
import secrets
import sys
import uuid
from pathlib import Path

import bcrypt
import httpx
import psycopg

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "packages" / "contracts" / "src"))
from quantpilot_contracts.envfile import load_env_file  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]
load_env_file(ROOT / ".env")

API = os.environ.get("QP_API_BASE_URL", "http://127.0.0.1:8000")
PG_DSN = os.environ["QP_PG_DSN"]
SVC_KEY = os.environ["QP_API_KEY"]

failures = 0


def check(name: str, ok: bool, detail: str = "") -> None:
    global failures
    tag = "PASS" if ok else "FAIL"
    print(f"[{tag}] {name}{(' ' + detail) if detail else ''}")
    if not ok:
        failures += 1


def _upsert_user(cur, username: str, role: str, password: str) -> tuple:
    """Create the fixture user or rotate its password; -> (user_id, state)."""
    cur.execute("SELECT id FROM users WHERE username = %s", (username,))
    row = cur.fetchone()
    ph = bcrypt.hashpw(password.encode("utf-8"), bcrypt.gensalt(rounds=12)).decode("ascii")
    if row is None:
        cur.execute(
            "INSERT INTO users (username, password_hash, role) VALUES (%s, %s, %s) RETURNING id",
            (username, ph, role),
        )
        return cur.fetchone()[0], "created"
    cur.execute(
        "UPDATE users SET password_hash = %s, role = %s, is_active = true WHERE id = %s RETURNING id",
        (ph, role, row[0]),
    )
    return cur.fetchone()[0], "rotated"


def _login(client: httpx.Client, username: str, password: str):
    resp = client.post(
        f"{API}/api/v1/auth/login", json={"username": username, "password": password}
    )
    if resp.status_code != 200:
        return resp, None
    return resp, resp.json()["data"]["token"]


def main() -> int:
    svc = httpx.Client(timeout=10, trust_env=False, headers={"X-API-Key": SVC_KEY})

    # ---- fixtures: throwaway users, fresh random passwords every run -----
    admin_pw = secrets.token_urlsafe(12)
    analyst_pw = secrets.token_urlsafe(12)
    viewer_pw = secrets.token_urlsafe(12)
    with psycopg.connect(PG_DSN) as conn:
        with conn.cursor() as cur:
            admin_id, admin_state = _upsert_user(cur, "qp_admin_check", "ADMIN", admin_pw)
            analyst_id, analyst_state = _upsert_user(cur, "qp_analyst_check", "ANALYST", analyst_pw)
            viewer_id, viewer_state = _upsert_user(cur, "qp_viewer_check", "VIEWER", viewer_pw)
            # analyst + viewer start WITHOUT grants; grants arrive via the API below
            for uid in (analyst_id, viewer_id):
                cur.execute("DELETE FROM user_account_grants WHERE user_id = %s", (uid,))
    print(f"       fixtures: admin={admin_state} analyst={analyst_state} viewer={viewer_state}")

    # ---- sanity ----------------------------------------------------------
    check("api.healthz", svc.get(f"{API}/healthz").status_code == 200)

    accounts = svc.get(f"{API}/api/v1/accounts").json()["data"]
    if not accounts:
        check("fixtures.account_available", False, "no trading accounts in cloud")
        print(f"\nauth acceptance failures: {failures}")
        return 1
    granted_acc = accounts[0]["account_id"]
    other_acc = str(uuid.uuid4())  # granted to nobody
    print(f"       granted account: {granted_acc}")

    # ---- lane 2: login / JWT ---------------------------------------------
    resp, admin_token = _login(svc, "qp_admin_check", admin_pw)
    check("auth.login_admin", resp.status_code == 200, f"status={resp.status_code}")
    admin = httpx.Client(timeout=10, trust_env=False, headers={"Authorization": f"Bearer {admin_token}"})

    me = admin.get(f"{API}/api/v1/auth/me").json()["data"]
    check("auth.me_admin", me.get("role") == "ADMIN", f"role={me.get('role')}")

    resp = svc.post(
        f"{API}/api/v1/auth/login", json={"username": "qp_admin_check", "password": "wrong"}
    )
    check(
        "auth.login_wrong_password_401",
        resp.status_code == 401 and resp.json()["error"]["code"] == "INVALID_CREDENTIALS",
        f"status={resp.status_code}",
    )
    resp = svc.get(f"{API}/api/v1/nodes", headers={"Authorization": "Bearer garbage"})
    check("auth.bearer_garbage_401", resp.status_code == 401, f"status={resp.status_code}")
    anon = httpx.Client(timeout=10, trust_env=False)  # no default headers at all
    resp = anon.get(f"{API}/api/v1/nodes")
    check("auth.no_credentials_401", resp.status_code == 401, f"status={resp.status_code}")

    # ---- analyst: role matrix + isolation before any grant ---------------
    resp, analyst_token = _login(svc, "qp_analyst_check", analyst_pw)
    analyst = httpx.Client(
        timeout=10, trust_env=False, headers={"Authorization": f"Bearer {analyst_token}"}
    )
    check("auth.login_analyst", resp.status_code == 200)
    resp = analyst.post(f"{API}/api/v1/commands/pending", json={})
    check(
        "auth.machine_route_service_only",
        resp.status_code == 403 and resp.json()["error"]["code"] == "SERVICE_KEY_ONLY",
        f"status={resp.status_code}",
    )
    listed = analyst.get(f"{API}/api/v1/accounts").json()["data"]
    check("isolation.analyst_accounts_empty", listed == [], f"count={len(listed)}")
    resp = analyst.get(f"{API}/api/v1/accounts/{granted_acc}/positions")
    check(
        "isolation.analyst_account_403",
        resp.status_code == 403 and resp.json()["error"]["code"] == "ACCOUNT_NOT_GRANTED",
        f"status={resp.status_code}",
    )
    resp = analyst.post(f"{API}/api/v1/backtests", json={})  # no Idempotency-Key -> 422
    check(
        "rbac.analyst_backtest_gate_passes",
        resp.status_code == 422,  # reached the handler (not 403)
        f"status={resp.status_code}",
    )
    resp = analyst.post(f"{API}/api/v1/commands", json={})
    check(
        "rbac.analyst_command_403",
        resp.status_code == 403 and resp.json()["error"]["code"] == "ROLE_FORBIDDEN",
        f"status={resp.status_code}",
    )

    # ---- admin: user management + grant assignment ------------------------
    users = admin.get(f"{API}/api/v1/auth/users").json()["data"]
    viewer_row = next((u for u in users if u["username"] == "qp_viewer_check"), None)
    check(
        "admin.users_lists_fixtures",
        viewer_row is not None and viewer_row["role"] == "VIEWER",
        f"found={viewer_row is not None}",
    )
    resp = admin.patch(
        f"{API}/api/v1/auth/users/{analyst_id}", json={"account_ids": [granted_acc]}
    )
    check("admin.grant_replace_via_patch", resp.status_code == 200, f"status={resp.status_code}")
    listed = analyst.get(f"{API}/api/v1/accounts").json()["data"]
    check(
        "isolation.grant_effective_immediately",
        [a["account_id"] for a in listed] == [granted_acc],
        f"count={len(listed)}",
    )

    # live coverage of POST /auth/users (also produces the auth.user_created
    # audit event the chain check below requires); unique name per run
    tmp_user = f"qp_tmp_{secrets.token_hex(3)}"
    resp = admin.post(
        f"{API}/api/v1/auth/users",
        json={"username": tmp_user, "password": "throwaway-123", "role": "VIEWER"},
    )
    check("admin.create_user_via_api", resp.status_code == 200, f"status={resp.status_code}")

    # ---- viewer: strictly read-only --------------------------------------
    resp, viewer_token = _login(svc, "qp_viewer_check", viewer_pw)
    viewer = httpx.Client(
        timeout=10, trust_env=False, headers={"Authorization": f"Bearer {viewer_token}"}
    )
    check("auth.login_viewer", resp.status_code == 200)
    resp = admin.patch(f"{API}/api/v1/auth/users/{viewer_id}", json={"account_ids": [granted_acc]})
    check("admin.grant_viewer", resp.status_code == 200, f"status={resp.status_code}")
    listed = viewer.get(f"{API}/api/v1/accounts").json()["data"]
    check(
        "isolation.viewer_accounts_only_granted",
        [a["account_id"] for a in listed] == [granted_acc],
        f"count={len(listed)}",
    )
    resp = viewer.get(f"{API}/api/v1/accounts/{granted_acc}/positions")
    check("isolation.viewer_granted_positions_200", resp.status_code == 200)
    resp = viewer.get(f"{API}/api/v1/accounts/{other_acc}/positions")
    check(
        "isolation.viewer_ungranted_account_403",
        resp.status_code == 403 and resp.json()["error"]["code"] == "ACCOUNT_NOT_GRANTED",
        f"status={resp.status_code}",
    )
    resp = viewer.post(f"{API}/api/v1/commands", json={})
    check(
        "rbac.viewer_command_403",
        resp.status_code == 403 and resp.json()["error"]["code"] == "ROLE_FORBIDDEN",
        f"status={resp.status_code}",
    )
    resp = viewer.get(f"{API}/api/v1/auth/users")
    check(
        "rbac.viewer_users_403",
        resp.status_code == 403 and resp.json()["error"]["code"] == "ADMIN_ONLY",
        f"status={resp.status_code}",
    )

    # ---- lane 3: personal API keys ----------------------------------------
    resp = viewer.post(f"{API}/api/v1/auth/api-keys", json={"label": "acceptance-run"})
    ok = resp.status_code == 200
    plaintext = resp.json()["data"]["key"] if ok else ""
    key_id = resp.json()["data"]["key_id"] if ok else ""
    check("apikeys.create", ok and plaintext.startswith("qpk_"), f"prefix={plaintext[:8]}")
    keys = viewer.get(f"{API}/api/v1/auth/api-keys").json()["data"]
    leaked = [k for k in keys if "key" in k and k.get("key") not in (None, "")]
    metadata_only = all(k.get("key_prefix") and not k.get("key_hash") for k in keys)
    check("apikeys.list_metadata_only", metadata_only and not leaked, f"count={len(keys)}")

    userkey = httpx.Client(timeout=10, trust_env=False, headers={"X-API-Key": plaintext})
    me = userkey.get(f"{API}/api/v1/auth/me").json().get("data") or {}
    check("apikeys.user_key_me", me.get("role") == "VIEWER", f"role={me.get('role')}")
    keys = viewer.get(f"{API}/api/v1/auth/api-keys").json()["data"]
    used = next((k for k in keys if k["key_id"] == key_id), {})
    check("apikeys.last_used_stamped", used.get("last_used_at") is not None)

    resp = viewer.delete(f"{API}/api/v1/auth/api-keys/{key_id}")
    check("apikeys.revoke", resp.status_code == 200, f"status={resp.status_code}")
    resp = userkey.get(f"{API}/api/v1/auth/me")
    check(
        "apikeys.revoked_key_401_immediately",
        resp.status_code == 401,
        f"status={resp.status_code}",
    )
    resp = viewer.delete(f"{API}/api/v1/auth/api-keys/{key_id}")
    check("apikeys.revoke_idempotent_404", resp.status_code == 404, f"status={resp.status_code}")

    # ---- credential rotation ---------------------------------------------
    new_pw = secrets.token_urlsafe(12)
    resp = admin.patch(f"{API}/api/v1/auth/users/{viewer_id}", json={"password": new_pw})
    check("rotation.password_changed", resp.status_code == 200, f"status={resp.status_code}")
    resp, _ = _login(svc, "qp_viewer_check", viewer_pw)
    check("rotation.old_password_401", resp.status_code == 401, f"status={resp.status_code}")
    resp, _ = _login(svc, "qp_viewer_check", new_pw)
    check("rotation.new_password_200", resp.status_code == 200, f"status={resp.status_code}")

    # ---- audit trail ------------------------------------------------------
    events = svc.get(f"{API}/api/v1/events?limit=200").json()["data"]
    types = {e["event_type"] for e in events}
    needed = {"auth.login_succeeded", "auth.login_failed", "auth.user_created", "auth.api_key_created"}
    check("events.auth_audit_chain", needed.issubset(types), f"missing={needed - types}")

    # ---- cleanup: throwaway API-created users ------------------------------
    with psycopg.connect(PG_DSN) as conn:
        with conn.cursor() as cur:
            cur.execute("DELETE FROM users WHERE username LIKE 'qp_tmp_%'")

    print(f"\nauth acceptance failures: {failures}")
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
