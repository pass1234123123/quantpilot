"""Shared helpers for R2-remediation route tests (NOT a test module).

The R2 remediation changed the auth model for route tests:

* the service key is a MACHINE lane only (P1-01) - business routes need a
  named user;
* the Bearer lane re-reads the user row on every request (P2-01/P2-02),
  so token-authenticated tests must stand in for auth.db.

`bearer(...)` patches os.environ + auth.db for one request and yields the
Authorization header to use. `granted_accounts` narrows account_granted to
that iterable of account ids (None = everything granted, i.e. ADMIN-like
visibility for grant checks).
"""

from __future__ import annotations

import os
from contextlib import contextmanager
from typing import Iterable
from unittest.mock import MagicMock, patch

SECRET_ENV = {"QP_API_KEY": "svc-key", "QP_JWT_SECRET": "jwt-secret"}


def user_row(role: str = "ADMIN", is_active: bool = True, token_version: int = 0,
             username: str = "root") -> dict:
    return {
        "username": username,
        "role": role,
        "is_active": is_active,
        "token_version": token_version,
    }


def auth_db_mock(role: str = "ADMIN", is_active: bool = True, token_version: int = 0,
                 granted_accounts: Iterable | None = None,
                 username: str = "root",
                 db_role: str | None = None, db_token_version: int | None = None) -> MagicMock:
    """MagicMock standing in for auth.db inside the auth module.

    The token is minted with `role`/`token_version`; the users row the
    middleware re-reads on every request reports `db_role`/`db_token_version`
    (defaulting to the token's values). Diverge them to simulate a role
    change or a token_version bump that must kill the old token (P2-01/02).
    """
    mock = MagicMock()
    granted = None if granted_accounts is None else {str(a) for a in granted_accounts}
    eff_role = role if db_role is None else db_role
    eff_ver = token_version if db_token_version is None else db_token_version

    def query_one(sql, params=None, **_):
        if "FROM users" in sql:
            return user_row(eff_role, is_active, eff_ver, username)
        if "user_account_grants" in sql:
            if granted is None:
                return {"one": 1}
            account = str(params[1]) if params and len(params) > 1 else None
            return {"one": 1} if account in granted else None
        if "user_api_keys" in sql:
            return {
                "key_id": "k-1",
                "user_id": "u-1",
                "revoked_at": None,
                "username": username,
                "role": eff_role,
                "is_active": is_active,
            }
        return None

    mock.query_one.side_effect = query_one
    return mock


@contextmanager
def bearer(role: str = "ADMIN", is_active: bool = True, token_version: int = 0,
           granted_accounts: Iterable | None = None, username: str = "root",
           db_role: str | None = None, db_token_version: int | None = None):
    """Authenticate one request as a named user; yields the headers dict."""
    import quantpilot_api.auth as auth

    mock = auth_db_mock(role, is_active, token_version, granted_accounts, username,
                        db_role, db_token_version)
    with patch.dict(os.environ, SECRET_ENV), patch.object(auth, "db", mock):
        token = auth.issue_token("u-1", username, role, token_version=token_version)
        yield {"Authorization": f"Bearer {token}"}


@contextmanager
def service_key():
    """Authenticate one request as the machine lane (service key)."""
    with patch.dict(os.environ, {"QP_API_KEY": "svc-key"}):
        yield {"X-API-Key": "svc-key"}
