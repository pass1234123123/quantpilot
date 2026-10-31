"""Add per-command broker execution evidence (phase-3 R1).

The EA (v1.8.0) journals every live-mutating command to disk and now acks
with structured execution tickets. The cloud stores them on the command row
so reconciliation can match orders / deals / positions by ticket instead of
parsing ack_message. Also the home of the EXECUTE_UNRESOLVED outcome: a
terminal crash mid-execution acks "outcome uncertain" and the operator
confirms the true state via the reconcile endpoints before any re-issue.

Downgrade drops the three evidence columns (ack_message still carries the
tickets textually, so nothing is irrecoverably lost).

Revision ID: 0016
Revises: 0015
Create Date: 2026-10-31
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0016"
down_revision: Union[str, None] = "0015"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    bind = op.get_bind()
    for column in ("exec_order_ticket", "exec_deal_ticket", "exec_position_ticket"):
        bind.execute(sa.text(f"ALTER TABLE commands ADD COLUMN IF NOT EXISTS {column} text"))
    bind.execute(
        sa.text(
            "CREATE INDEX IF NOT EXISTS idx_commands_unresolved"
            " ON commands (updated_at DESC)"
            " WHERE status = 'UNKNOWN' OR ack_code = 'EXECUTE_UNRESOLVED'"
        )
    )


def downgrade() -> None:
    bind = op.get_bind()
    for column in ("exec_order_ticket", "exec_deal_ticket", "exec_position_ticket"):
        bind.execute(sa.text(f"ALTER TABLE commands DROP COLUMN IF EXISTS {column}"))
    bind.execute(sa.text("DROP INDEX IF EXISTS idx_commands_unresolved"))
