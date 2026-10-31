"""Bootstrap a named user server-side (phase-3 R2).

The first ADMIN cannot be created via the API (no ADMIN exists yet to
authorize the call), so this CLI writes users directly to PostgreSQL with
the same bcrypt cost 12 as the API's auth module. Requires the dev SSH
tunnel (scripts/dev_pg_tunnel.py) and QP_PG_DSN in .env.

Usage:
  python scripts/create_user.py alice 'S0me-Passw0rd!' --role ADMIN
  python scripts/create_user.py bob 'An0ther-Pass!' --role TRADER \
      --grant-account 11111111-2222-3333-4444-555555555555

Roles: ADMIN / TRADER / ANALYST / VIEWER. The password is never printed
or logged; only its bcrypt hash is stored.
"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

import bcrypt
import psycopg

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "packages" / "contracts" / "src"))
from quantpilot_contracts.envfile import load_env_file  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]
load_env_file(ROOT / ".env")

ROLES = ("ADMIN", "TRADER", "ANALYST", "VIEWER")


def main() -> int:
    parser = argparse.ArgumentParser(
        description="create a QuantPilot user with role and optional account grants"
    )
    parser.add_argument("username")
    parser.add_argument("password")
    parser.add_argument("--role", choices=ROLES, default="VIEWER")
    parser.add_argument("--grant-account", action="append", default=[], metavar="UUID")
    args = parser.parse_args()

    if len(args.password) < 8:
        parser.error("password must be at least 8 characters")

    dsn = os.environ.get("QP_PG_DSN")
    if not dsn:
        print("QP_PG_DSN missing; start scripts/dev_pg_tunnel.py and check .env", file=sys.stderr)
        return 1

    password_hash = bcrypt.hashpw(
        args.password.encode("utf-8"), bcrypt.gensalt(rounds=12)
    ).decode("ascii")

    try:
        with psycopg.connect(dsn) as conn:
            with conn.cursor() as cur:
                cur.execute("SELECT 1 FROM users WHERE username = %s", (args.username,))
                if cur.fetchone() is not None:
                    print(f"user {args.username!r} already exists", file=sys.stderr)
                    return 1
                cur.execute(
                    """
                    INSERT INTO users (username, password_hash, role)
                    VALUES (%s, %s, %s)
                    RETURNING id
                    """,
                    (args.username, password_hash, args.role),
                )
                user_id = cur.fetchone()[0]
                for account_id in args.grant_account:
                    cur.execute(
                        "INSERT INTO user_account_grants (user_id, account_id) VALUES (%s, %s)",
                        (user_id, account_id),
                    )
    except psycopg.errors.ForeignKeyViolation as exc:
        print(f"grant failed: account not found ({exc})", file=sys.stderr)
        return 1

    print(f"created user {args.username!r} role={args.role} id={user_id}")
    if args.grant_account:
        print(f"granted accounts: {', '.join(args.grant_account)}")
    print("login via POST /api/v1/auth/login; rotate the password after first login")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
