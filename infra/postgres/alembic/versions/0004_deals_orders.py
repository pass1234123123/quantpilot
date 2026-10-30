"""deals + orders: closed-trade history and pending-order projections (doc 12.2).

deals are immutable broker facts synced incrementally from the EA history
(HistorySelect/HistoryDealGet, read-only). Idempotent on
(account_id, broker_deal_ticket) per doc 12.3 index.

orders is the pending-order projection, state-synced like positions: tickets
present in an EA snapshot are ACTIVE, missing ones are marked REMOVED.

Money/volume columns are NUMERIC (never float, doc 21.3).

Revision ID: 0004
Revises: 0003
Create Date: 2026-10-31
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0004"
down_revision: Union[str, None] = "0003"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

TS = lambda: postgresql.TIMESTAMP(timezone=True)  # noqa: E731
UUID = lambda: postgresql.UUID(as_uuid=True)  # noqa: E731


def upgrade() -> None:
    op.create_table(
        "deals",
        sa.Column("id", UUID(), primary_key=True, server_default=sa.text("gen_random_uuid()")),
        sa.Column("tenant_id", UUID(), nullable=True),
        sa.Column("account_id", UUID(), nullable=False),
        sa.Column("broker_deal_ticket", sa.Text(), nullable=False),
        sa.Column("broker_order_ticket", sa.Text(), nullable=False, server_default=""),
        sa.Column("broker_position_ticket", sa.Text(), nullable=False, server_default=""),
        sa.Column("broker_symbol", sa.Text(), nullable=False),
        sa.Column("direction", sa.Text(), nullable=False),  # BUY | SELL
        sa.Column("entry", sa.Text(), nullable=False),       # IN | OUT | INOUT | OUT_BY
        sa.Column("volume", sa.Numeric(20, 8), nullable=False),
        sa.Column("price", sa.Numeric(20, 8), nullable=False),
        sa.Column("commission", sa.Numeric(20, 8), nullable=True),
        sa.Column("swap", sa.Numeric(20, 8), nullable=True),
        sa.Column("profit", sa.Numeric(20, 8), nullable=True),
        sa.Column("deal_time", TS(), nullable=False),
        sa.Column("magic", sa.BigInteger(), nullable=True),
        sa.Column("comment", sa.Text(), nullable=True),
        sa.Column("created_at", TS(), nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", TS(), nullable=False, server_default=sa.func.now()),
        sa.ForeignKeyConstraint(["account_id"], ["trading_accounts.id"], name="fk_deals_account", ondelete="CASCADE"),
        # doc 12.3: INDEX (deals.account_id, broker_deal_ticket) - the unique
        # constraint below provides exactly this and makes replay idempotent.
        sa.UniqueConstraint("account_id", "broker_deal_ticket", name="uq_deals_account_ticket"),
    )
    op.create_index("ix_deals_account_time", "deals", ["account_id", "deal_time"])
    op.create_index("ix_deals_account_position", "deals", ["account_id", "broker_position_ticket"])

    op.create_table(
        "orders",
        sa.Column("id", UUID(), primary_key=True, server_default=sa.text("gen_random_uuid()")),
        sa.Column("tenant_id", UUID(), nullable=True),
        sa.Column("account_id", UUID(), nullable=False),
        sa.Column("broker_ticket", sa.Text(), nullable=False),
        sa.Column("broker_symbol", sa.Text(), nullable=False),
        sa.Column("direction", sa.Text(), nullable=False),   # BUY | SELL
        sa.Column("order_kind", sa.Text(), nullable=False),  # BUY_LIMIT | SELL_STOP | ...
        sa.Column("volume", sa.Numeric(20, 8), nullable=False),
        sa.Column("price", sa.Numeric(20, 8), nullable=False),
        sa.Column("sl", sa.Numeric(20, 8), nullable=True),
        sa.Column("tp", sa.Numeric(20, 8), nullable=True),
        sa.Column("time_setup", TS(), nullable=True),
        sa.Column("time_expiration", TS(), nullable=True),
        sa.Column("status", sa.Text(), nullable=False, server_default="ACTIVE"),  # ACTIVE | REMOVED
        sa.Column("last_reported_at", TS(), nullable=True),
        sa.Column("removed_at", TS(), nullable=True),
        sa.Column("created_at", TS(), nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", TS(), nullable=False, server_default=sa.func.now()),
        sa.Column("version", sa.Integer(), nullable=False, server_default="1"),
        sa.ForeignKeyConstraint(["account_id"], ["trading_accounts.id"], name="fk_orders_account", ondelete="CASCADE"),
        sa.UniqueConstraint("account_id", "broker_ticket", name="uq_orders_account_ticket"),
    )
    op.create_index("ix_orders_account_status", "orders", ["account_id", "status"])


def downgrade() -> None:
    op.drop_index("ix_orders_account_status", table_name="orders")
    op.drop_table("orders")
    op.drop_index("ix_deals_account_position", table_name="deals")
    op.drop_index("ix_deals_account_time", table_name="deals")
    op.drop_table("deals")
