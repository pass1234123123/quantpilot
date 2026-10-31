"""Risk engine execution side: gate state + decision audit (PRD 9, doc 12.2).

- risk_states: durable STOP_NEW_RISK circuit state, GLOBAL and per account
  (doc 3.3 "不可丢失的风控状态变更", 9.5 state machine subset). Absence of an
  ACCOUNT row means NORMAL; the GLOBAL row is seeded here.
- risk_decisions: one audit row per gated command (doc 12.2), decision
  ALLOW/REJECT (+REDUCE/REVIEW reserved for later slices, 9.1), reason codes
  and the numeric snapshot the decision was computed from.
- commands.status gains REJECTED: the cloud-side risk gate refuses the
  command before any delivery. REJECTED is terminal - the sweeper never
  touches it and /commands/pending never hands it out (PRD 9.1: the risk
  engine has final refusal authority over new risk).

Revision ID: 0009
Revises: 0008
Create Date: 2026-10-31
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0009"
down_revision: Union[str, None] = "0008"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

TS = lambda: postgresql.TIMESTAMP(timezone=True)  # noqa: E731
UUID = lambda: postgresql.UUID(as_uuid=True)  # noqa: E731

COMMAND_STATUSES = ("QUEUED", "DELIVERED", "ACKED", "EXPIRED", "UNKNOWN", "REJECTED")


def upgrade() -> None:
    op.create_table(
        "risk_states",
        sa.Column("id", UUID(), primary_key=True, server_default=sa.text("gen_random_uuid()")),
        sa.Column("scope", sa.Text(), nullable=False),
        sa.Column("account_id", UUID(), nullable=True),
        # NORMAL | STOP_NEW_RISK | LOCKED (9.5 subset; LOCKED needs manual unlock)
        sa.Column("status", sa.Text(), nullable=False, server_default="NORMAL"),
        sa.Column("reason", sa.Text(), nullable=False, server_default=""),
        sa.Column("updated_by", sa.Text(), nullable=False, server_default="api"),
        sa.Column("created_at", TS(), nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", TS(), nullable=False, server_default=sa.func.now()),
        sa.Column("version", sa.Integer(), nullable=False, server_default="1"),
        sa.CheckConstraint("scope IN ('GLOBAL', 'ACCOUNT')", name="ck_risk_states_scope"),
        sa.CheckConstraint(
            "(scope = 'GLOBAL' AND account_id IS NULL) OR (scope = 'ACCOUNT' AND account_id IS NOT NULL)",
            name="ck_risk_states_scope_account",
        ),
        sa.CheckConstraint("status IN ('NORMAL', 'STOP_NEW_RISK', 'LOCKED')", name="ck_risk_states_status"),
        sa.ForeignKeyConstraint(["account_id"], ["trading_accounts.id"],
                                name="fk_risk_states_account", ondelete="CASCADE"),
    )
    # one row per scope+account; partial unique indexes because NULLs are
    # distinct under plain UNIQUE in PostgreSQL
    op.create_index("uq_risk_states_global", "risk_states", ["scope"],
                    unique=True, postgresql_where=sa.text("account_id IS NULL"))
    op.create_index("uq_risk_states_account", "risk_states", ["account_id"],
                    unique=True, postgresql_where=sa.text("account_id IS NOT NULL"))
    op.create_index("ix_risk_states_status", "risk_states", ["status"])
    op.execute(
        "INSERT INTO risk_states (scope, account_id, status) VALUES ('GLOBAL', NULL, 'NORMAL')"
    )

    op.create_table(
        "risk_decisions",
        sa.Column("id", UUID(), primary_key=True, server_default=sa.text("gen_random_uuid()")),
        sa.Column("account_id", UUID(), nullable=False),
        sa.Column("command_id", UUID(), nullable=False),
        sa.Column("signal_id", sa.Text(), nullable=True),
        sa.Column("decision", sa.Text(), nullable=False),
        sa.Column("reason_codes", postgresql.JSONB(), nullable=False),
        sa.Column("risk_snapshot", postgresql.JSONB(), nullable=False),
        sa.Column("config_version", sa.Integer(), nullable=False),
        sa.Column("decided_at", TS(), nullable=False, server_default=sa.func.now()),
        sa.CheckConstraint("decision IN ('ALLOW', 'REJECT', 'REDUCE', 'REVIEW')",
                           name="ck_risk_decisions_decision"),
        sa.ForeignKeyConstraint(["account_id"], ["trading_accounts.id"],
                                name="fk_risk_decisions_account", ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["command_id"], ["commands.command_id"],
                                name="fk_risk_decisions_command", ondelete="CASCADE"),
    )
    # one gate evaluation per command (the gate runs once at creation;
    # idempotent replays return the existing row, AT-005)
    op.create_unique_constraint("uq_risk_decisions_command", "risk_decisions", ["command_id"])
    op.create_index("ix_risk_decisions_account_decided", "risk_decisions",
                    ["account_id", "decided_at"])

    statuses = ", ".join(f"'{s}'" for s in COMMAND_STATUSES)
    op.drop_constraint("ck_commands_status", "commands")
    op.create_check_constraint("ck_commands_status", "commands", f"status IN ({statuses})")


def downgrade() -> None:
    old_statuses = ("QUEUED", "DELIVERED", "ACKED", "EXPIRED", "UNKNOWN")
    statuses = ", ".join(f"'{s}'" for s in old_statuses)
    op.drop_constraint("ck_commands_status", "commands")
    op.create_check_constraint("ck_commands_status", "commands", f"status IN ({statuses})")
    op.drop_index("ix_risk_decisions_account_decided", table_name="risk_decisions")
    op.drop_constraint("uq_risk_decisions_command", "risk_decisions")
    op.drop_table("risk_decisions")
    op.drop_index("ix_risk_states_status", table_name="risk_states")
    op.drop_index("uq_risk_states_account", table_name="risk_states")
    op.drop_index("uq_risk_states_global", table_name="risk_states")
    op.drop_table("risk_states")
