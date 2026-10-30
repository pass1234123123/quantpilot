"""API service configuration from environment / .env."""

from __future__ import annotations

import os

from quantpilot_contracts.envfile import load_env_file

load_env_file(os.environ.get("QP_ENV_FILE", ".env"))


def pg_dsn() -> str:
    dsn = os.environ.get("QP_PG_DSN", "")
    if not dsn:
        raise RuntimeError("QP_PG_DSN is required (start scripts/dev_pg_tunnel.py first)")
    return dsn


def nats_url() -> str:
    return os.environ["QP_NATS_URL"]


def api_host() -> str:
    return os.environ.get("QP_API_HOST", "127.0.0.1")


def api_port() -> int:
    return int(os.environ.get("QP_API_PORT", "8000"))
