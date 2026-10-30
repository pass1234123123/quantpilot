"""positions: open-position projection synced from the EA (doc 12.2/12.3).

MT5 remains the authoritative source for positions; this table is a
rebuildable projection (doc 4.4). Rows are upserted by
(account_id, broker_position_ticket) on every EA snapshot; tickets missing
from a snapshot are marked CLOSED by the consumer (state-based sync, so
JetStream redelivery is naturally idempotent).

Money/volume columns are NUMERIC (never float, doc 21.3).

Revision ID: 0003
Revises: 0002
Create Date: 2026-10-30
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0003"
down_revision: Union[str, None] = "0002"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

TS = lambda: postgresql.TIMESTAMP(timezone=True)  # noqa: E731
UUID = lambda: postgresql.UUID(as_uuid=True)  # noqa: E731


def upgrade() -> None:
    op.create_table(
        "positions",
        sa.Column("id", UUID(), primary_key=True, server_default=sa.text("gen_random_uuid()")),
        sa.Column("tenant_id", UUID(), nullable=True),
        sa.Column("account_id", UUID(), nullable=False),
        sa.Column("broker_position_ticket", sa.Text(), nullable=False),
        sa.Column("broker_symbol", sa.Text(), nullable=False),
        sa.Column("direction", sa.Text(), nullable=False),  # BUY | SELL
        sa.Column("volume", sa.Numeric(20, 8), nullable=False),
        sa.Column("price_open", sa.Numeric(20, 8), nullable=False),
        sa.Column("current_price", sa.Numeric(20, 8), nullable=True),
        sa.Column("sl", sa.Numeric(20, 8), nullable=True),
        sa.Column("tp", sa.Numeric(20, 8), nullable=True),
        # last observed floating profit/swap (not realized P/L - that needs deals)
        sa.Column("profit", sa.Numeric(20, 8), nullable=True),
        sa.Column("swap", sa.Numeric(20, 8), nullable=True),
        sa.Column("open_time", TS(), nullable=True),
        sa.Column("status", sa.Text(), nullable=False, server_default="OPEN"),  # OPEN | CLOSED
        sa.Column("last_reported_at", TS(), nullable=True),
        sa.Column("closed_at", TS(), nullable=True),
        sa.Column("created_at", TS(), nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", TS(), nullable=False, server_default=sa.func.now()),
        sa.Column("version", sa.Integer(), nullable=False, server_default="1"),
        sa.ForeignKeyConstraint(["account_id"], ["trading_accounts.id"], name="fk_positions_account", ondelete="CASCADE"),
        # doc 12.3: INDEX (positions.account_id, broker_position_ticket) - the
        # unique constraint below provides exactly this index.
        sa.UniqueConstraint("account_id", "broker_position_ticket", name="uq_positions_account_ticket"),
    )
    op.create_index("ix_positions_account_status", "positions", ["account_id", "status"])


def downgrade() -> None:
    op.drop_index("ix_positions_account_status", table_name="positions")
    op.drop_table("positions")
