"""Verify migration: list public tables + row counts via SSH tunnel (QP_PG_DSN)."""
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "packages" / "contracts" / "src"))
from quantpilot_contracts.envfile import load_env_file  # noqa: E402

import psycopg  # noqa: E402

load_env_file(Path(__file__).resolve().parents[1] / ".env")

with psycopg.connect(os.environ["QP_PG_DSN"]) as conn:
    rows = conn.execute(
        "SELECT table_name FROM information_schema.tables "
        "WHERE table_schema='public' ORDER BY table_name"
    ).fetchall()
    print("tables:", [r[0] for r in rows])
    for (name,) in rows:
        cnt = conn.execute(f'SELECT count(*) FROM "{name}"').fetchone()[0]
        print(f"  {name}: {cnt} rows")
    ver = conn.execute("SELECT version_num FROM alembic_version").fetchone()
    print("alembic_version:", ver[0])
