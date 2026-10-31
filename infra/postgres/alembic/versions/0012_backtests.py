"""Reproducible strategy backtest jobs and immutable reports (PRD 15).

Jobs snapshot the fixed strategy instance version/config revision, requested
range and complete cost assumptions. Idempotency-Key determines the job UUID;
results store the full report in JSONB plus the minimum PRD metrics, so an API
replay never needs to rerun historical execution or create trade commands.

Revision ID: 0012
Revises: 0011
Create Date: 2026-10-31
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0012"
down_revision: Union[str, None] = "0011"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

TS = lambda: postgresql.TIMESTAMP(timezone=True)  # noqa: E731
UUID = lambda: postgresql.UUID(as_uuid=True)  # noqa: E731


def upgrade() -> None:
    op.create_table(
        "backtest_jobs",
        sa.Column("id", UUID(), primary_key=True),
        sa.Column("strategy_version_id", UUID(), nullable=False),
        sa.Column("strategy_instance_id", UUID(), nullable=False),
        sa.Column("account_id", UUID(), nullable=False),
        sa.Column("broker_symbol", sa.Text(), nullable=False),
        sa.Column("timeframe", sa.Text(), nullable=False),
        sa.Column("range_start", TS(), nullable=False),
        sa.Column("range_end", TS(), nullable=False),
        sa.Column("config", postgresql.JSONB(), nullable=False),
        sa.Column("config_revision", sa.Integer(), nullable=False),
        sa.Column("cost_model", postgresql.JSONB(), nullable=False),
        sa.Column("initial_balance", sa.Numeric(), nullable=False),
        sa.Column("volume", sa.Numeric(), nullable=False),
        sa.Column("status", sa.Text(), nullable=False, server_default="QUEUED"),
        sa.Column("idempotency_key", UUID(), nullable=False),
        sa.Column("request_json", postgresql.JSONB(), nullable=False),
        sa.Column("request_hash", sa.Text(), nullable=False),
        sa.Column("data_hash", sa.Text(), nullable=True),
        sa.Column("error", sa.Text(), nullable=False, server_default=""),
        sa.Column("created_at", TS(), nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", TS(), nullable=False, server_default=sa.func.now()),
        sa.Column("completed_at", TS(), nullable=True),
        sa.Column("version", sa.Integer(), nullable=False, server_default="1"),
        sa.CheckConstraint("range_end > range_start", name="ck_backtest_jobs_range"),
        sa.CheckConstraint("initial_balance > 0 AND volume > 0", name="ck_backtest_jobs_amounts"),
        sa.CheckConstraint("status IN ('QUEUED', 'RUNNING', 'COMPLETED', 'FAILED')",
                           name="ck_backtest_jobs_status"),
        sa.ForeignKeyConstraint(["account_id"], ["trading_accounts.id"],
                                name="fk_backtest_jobs_account", ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["strategy_instance_id"], ["strategy_instances.id"],
                                name="fk_backtest_jobs_instance", ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["strategy_version_id"], ["strategy_versions.id"],
                                name="fk_backtest_jobs_version", ondelete="RESTRICT"),
    )
    op.create_unique_constraint("uq_backtest_jobs_idempotency", "backtest_jobs", ["idempotency_key"])
    op.create_index("ix_backtest_jobs_account_created", "backtest_jobs", ["account_id", "created_at"])

    op.create_table(
        "backtest_results",
        sa.Column("id", UUID(), primary_key=True, server_default=sa.text("gen_random_uuid()")),
        sa.Column("job_id", UUID(), nullable=False),
        sa.Column("metrics_json", postgresql.JSONB(), nullable=False),
        sa.Column("report_json", postgresql.JSONB(), nullable=False),
        sa.Column("report_uri", sa.Text(), nullable=False),
        sa.Column("created_at", TS(), nullable=False, server_default=sa.func.now()),
        sa.ForeignKeyConstraint(["job_id"], ["backtest_jobs.id"],
                                name="fk_backtest_results_job", ondelete="CASCADE"),
    )
    op.create_unique_constraint("uq_backtest_results_job", "backtest_results", ["job_id"])


def downgrade() -> None:
    op.drop_constraint("uq_backtest_results_job", "backtest_results")
    op.drop_table("backtest_results")
    op.drop_index("ix_backtest_jobs_account_created", table_name="backtest_jobs")
    op.drop_constraint("uq_backtest_jobs_idempotency", "backtest_jobs")
    op.drop_table("backtest_jobs")
