"""AI analysis engine: ai_analyses store (PRD 8, doc 12.2).

- ai_analyses: one row per AI evaluation of (account, symbol, timeframe) at
  a source bar. Core columns follow doc 12.2 (model_provider, model_id,
  prompt_version, input_hash, output_json); output_json holds the strict
  doc 8.4 analysis structure. Governance extras (8.6): created_at /
  expires_at / duration_ms (cost applies to LLM providers only; the
  built-in statistical provider costs nothing and records 0).
- UNIQUE (account, symbol, timeframe, input_hash): re-delivered bar batches
  reproduce the same feature snapshot -> same hash -> the second insert is a
  no-op (at-least-once stays effectively-once, same policy as signals 7.5).

First-release scope (doc 8.5): mode OFF/ADVISORY only. Analyses are advisory
display artifacts - the FILTER mode wiring (AI may veto signals) is a later
slice, so nothing here influences trade permission.

Revision ID: 0011
Revises: 0010
Create Date: 2026-10-31
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0011"
down_revision: Union[str, None] = "0010"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

TS = lambda: postgresql.TIMESTAMP(timezone=True)  # noqa: E731
UUID = lambda: postgresql.UUID(as_uuid=True)  # noqa: E731


def upgrade() -> None:
    op.create_table(
        "ai_analyses",
        sa.Column("id", UUID(), primary_key=True, server_default=sa.text("gen_random_uuid()")),
        sa.Column("analysis_id", UUID(), nullable=False),
        sa.Column("account_id", UUID(), nullable=False),
        sa.Column("broker_symbol", sa.Text(), nullable=False),
        sa.Column("timeframe", sa.Text(), nullable=False),
        # doc 8.4/8.6 governance identity
        sa.Column("model_provider", sa.Text(), nullable=False),
        sa.Column("model_id", sa.Text(), nullable=False),
        sa.Column("prompt_version", sa.Text(), nullable=False),
        # sha256 over the canonical feature snapshot (doc 8.4 input_snapshot_id)
        sa.Column("input_hash", sa.Text(), nullable=False),
        # full doc 8.4 analysis structure
        sa.Column("output_json", postgresql.JSONB(), nullable=False),
        sa.Column("source_bar_time", TS(), nullable=False),
        sa.Column("created_at", TS(), nullable=False, server_default=sa.func.now()),
        sa.Column("expires_at", TS(), nullable=True),
        sa.Column("duration_ms", sa.Integer(), nullable=False, server_default="0"),
        sa.CheckConstraint("timeframe IN ('M5', 'M15', 'M30', 'H1', 'H4')",
                           name="ck_ai_analyses_timeframe"),
        sa.ForeignKeyConstraint(["account_id"], ["trading_accounts.id"],
                                name="fk_ai_analyses_account", ondelete="CASCADE"),
    )
    op.create_unique_constraint("uq_ai_analyses_analysis", "ai_analyses", ["analysis_id"])
    op.create_unique_constraint("uq_ai_analyses_input", "ai_analyses",
                                ["account_id", "broker_symbol", "timeframe", "input_hash"])
    op.create_index("ix_ai_analyses_account_created", "ai_analyses",
                    ["account_id", "created_at"])


def downgrade() -> None:
    op.drop_index("ix_ai_analyses_account_created", table_name="ai_analyses")
    op.drop_constraint("uq_ai_analyses_input", "ai_analyses")
    op.drop_constraint("uq_ai_analyses_analysis", "ai_analyses")
    op.drop_table("ai_analyses")
