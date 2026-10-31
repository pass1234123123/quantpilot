"""commands: durable trade-command store + state machine (PRD 13.3-13.5, ADR-004).

Slice-1 lifecycle (OBSERVE_ONLY: the EA acks receipt, never executes):
    QUEUED -> DELIVERED -> ACKED
    QUEUED (past expires_at)          -> EXPIRED   (sweeper, never delivered)
    DELIVERED (past expiry + grace)   -> UNKNOWN   (sweeper, AT-006:
        never resent; reconciled to ACKED via late ack events or the EA's
        recently-acked echo on the next poll)

Idempotency (AT-005, PRD 2.5/14.1): command_id is globally UNIQUE - a
duplicate POST replays the original row instead of creating a second
command. idempotency_key (caller-supplied) is unique when present
(partial unique index). Every state transition is also written to
system_events by the API for auditability (AT-011).

Revision ID: 0008
Revises: 0007
Create Date: 2026-10-31
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0008"
down_revision: Union[str, None] = "0007"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

TS = lambda: postgresql.TIMESTAMP(timezone=True)  # noqa: E731
UUID = lambda: postgresql.UUID(as_uuid=True)  # noqa: E731

COMMAND_TYPES = (
    "OPEN_MARKET", "PLACE_PENDING", "MODIFY_POSITION", "CLOSE_POSITION",
    "CANCEL_ORDER", "STOP_NEW_RISK", "RECONCILE",
)
COMMAND_STATUSES = ("QUEUED", "DELIVERED", "ACKED", "EXPIRED", "UNKNOWN")


def upgrade() -> None:
    types = ", ".join(f"'{t}'" for t in COMMAND_TYPES)
    statuses = ", ".join(f"'{s}'" for s in COMMAND_STATUSES)
    op.create_table(
        "commands",
        sa.Column("id", UUID(), primary_key=True, server_default=sa.text("gen_random_uuid()")),
        sa.Column("command_id", UUID(), nullable=False),
        sa.Column("idempotency_key", sa.Text(), nullable=True),
        sa.Column("account_id", UUID(), nullable=False),
        sa.Column("command_type", sa.Text(), nullable=False),
        sa.Column("payload", postgresql.JSONB(), nullable=False),
        sa.Column("status", sa.Text(), nullable=False, server_default="QUEUED"),
        sa.Column("expires_at", TS(), nullable=False),
        sa.Column("created_by", sa.Text(), nullable=False, server_default="api"),
        sa.Column("strategy_instance_id", sa.Text(), nullable=True),
        sa.Column("signal_id", sa.Text(), nullable=True),
        sa.Column("delivery_attempts", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("delivered_at", TS(), nullable=True),
        sa.Column("acked_at", TS(), nullable=True),
        sa.Column("ack_code", sa.Text(), nullable=True),
        sa.Column("ack_message", sa.Text(), nullable=True),
        sa.Column("reconciled", sa.Boolean(), nullable=False, server_default=sa.text("false")),
        sa.Column("unknown_at", TS(), nullable=True),
        sa.Column("expired_at", TS(), nullable=True),
        sa.Column("created_at", TS(), nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", TS(), nullable=False, server_default=sa.func.now()),
        sa.Column("version", sa.Integer(), nullable=False, server_default="0"),
        sa.ForeignKeyConstraint(["account_id"], ["trading_accounts.id"],
                                name="fk_commands_account", ondelete="CASCADE"),
        sa.CheckConstraint(f"command_type IN ({types})", name="ck_commands_command_type"),
        sa.CheckConstraint(f"status IN ({statuses})", name="ck_commands_status"),
        sa.UniqueConstraint("command_id", name="uq_commands_command_id"),
    )
    # idempotency_key unique only where present (partial unique index)
    op.create_index(
        "uq_commands_idempotency_key",
        "commands",
        ["idempotency_key"],
        unique=True,
        postgresql_where=sa.text("idempotency_key IS NOT NULL"),
    )
    # pending pickup: oldest QUEUED command of an account (PRD 2.5 per-account
    # serialization - at most one command is handed out per fetch)
    op.create_index("ix_commands_account_status_created", "commands",
                    ["account_id", "status", "created_at"])
    # sweeper scans: EXPIRED / UNKNOWN candidates by (status, expires_at)
    op.create_index("ix_commands_status_expires", "commands", ["status", "expires_at"])


def downgrade() -> None:
    op.drop_index("ix_commands_status_expires", table_name="commands")
    op.drop_index("ix_commands_account_status_created", table_name="commands")
    op.drop_index("uq_commands_idempotency_key", table_name="commands")
    op.drop_table("commands")
