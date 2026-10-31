"""Add users / user_api_keys / user_account_grants (phase-3 R2).

Commercial auth: named users with bcrypt password hashes, four roles
(ADMIN / TRADER / ANALYST / VIEWER), per-user API keys (sha256-hashed at
rest, revocable, last-used tracking) and per-account grants for tenant
isolation. The shared service key (QP_API_KEY) keeps authenticating the
machine channel unchanged; user keys are a second, per-identity layer
resolved AFTER the service key, never instead of it.

Downgrade drops the three tables; users / keys / grants are lost by design.

Revision ID: 0017
Revises: 0016
Create Date: 2026-10-31
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0017"
down_revision: Union[str, None] = "0016"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.get_bind().execute(
        sa.text(
            """
            CREATE TABLE IF NOT EXISTS users (
                id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
                username text NOT NULL UNIQUE,
                password_hash text NOT NULL,
                role text NOT NULL
                    CHECK (role IN ('ADMIN', 'TRADER', 'ANALYST', 'VIEWER')),
                is_active boolean NOT NULL DEFAULT true,
                created_at timestamptz NOT NULL DEFAULT now()
            )
            """
        )
    )
    op.get_bind().execute(
        sa.text(
            """
            CREATE TABLE IF NOT EXISTS user_api_keys (
                id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
                user_id uuid NOT NULL REFERENCES users(id) ON DELETE CASCADE,
                key_prefix text NOT NULL,
                key_hash text NOT NULL UNIQUE,
                label text NOT NULL DEFAULT '',
                created_at timestamptz NOT NULL DEFAULT now(),
                last_used_at timestamptz,
                revoked_at timestamptz
            )
            """
        )
    )
    op.get_bind().execute(
        sa.text(
            "CREATE INDEX IF NOT EXISTS idx_user_api_keys_user ON user_api_keys (user_id)"
        )
    )
    op.get_bind().execute(
        sa.text(
            """
            CREATE TABLE IF NOT EXISTS user_account_grants (
                user_id uuid NOT NULL REFERENCES users(id) ON DELETE CASCADE,
                account_id uuid NOT NULL REFERENCES trading_accounts(id) ON DELETE CASCADE,
                granted_at timestamptz NOT NULL DEFAULT now(),
                PRIMARY KEY (user_id, account_id)
            )
            """
        )
    )
    op.get_bind().execute(
        sa.text(
            "CREATE INDEX IF NOT EXISTS idx_user_account_grants_account"
            " ON user_account_grants (account_id)"
        )
    )


def downgrade() -> None:
    op.get_bind().execute(sa.text("DROP TABLE IF EXISTS user_account_grants"))
    op.get_bind().execute(sa.text("DROP TABLE IF EXISTS user_api_keys"))
    op.get_bind().execute(sa.text("DROP TABLE IF EXISTS users"))
