"""Add audit_logs (phase-2 task 6): best-effort operation audit trail.

Every state-changing cloud operation (config saves, instance updates,
command/backtest creation, AI triggers) appends one row here. The writer
(audit.record) is best-effort: a write failure never breaks the audited
operation.

Downgrade drops the table; audit history is lost by design on downgrade.

Revision ID: 0015
Revises: 0014
Create Date: 2026-10-31
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0015"
down_revision: Union[str, None] = "0014"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.get_bind().execute(
        sa.text(
            """
            CREATE TABLE IF NOT EXISTS audit_logs (
                id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
                account_id uuid NOT NULL REFERENCES trading_accounts(id),
                actor text NOT NULL,
                action text NOT NULL,
                object_type text NOT NULL,
                object_id text,
                payload jsonb,
                created_at timestamptz NOT NULL DEFAULT now()
            )
            """
        )
    )
    op.get_bind().execute(
        sa.text(
            "CREATE INDEX IF NOT EXISTS idx_audit_logs_account_created"
            " ON audit_logs (account_id, created_at DESC)"
        )
    )
    op.get_bind().execute(
        sa.text("CREATE INDEX IF NOT EXISTS idx_audit_logs_action ON audit_logs (action)")
    )


def downgrade() -> None:
    op.get_bind().execute(sa.text("DROP TABLE IF EXISTS audit_logs"))
