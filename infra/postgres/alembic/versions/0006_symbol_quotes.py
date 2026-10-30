"""symbol_quotes: latest spread observation per (account, symbol) (doc 12.2).

The EA reports the current SYMBOL_SPREAD / bid / ask alongside each spec
refresh (hourly). Per the "no tick-by-tick market data in the cloud" design
(doc 4.4), we persist ONLY the latest observation per account+symbol: an
upsert keyed on (account_id, broker_symbol), never a history of quotes.

Money columns are NUMERIC (never float, doc 21.3).

Revision ID: 0006
Revises: 0005
Create Date: 2026-10-31
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0006"
down_revision: Union[str, None] = "0005"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

TS = lambda: postgresql.TIMESTAMP(timezone=True)  # noqa: E731
UUID = lambda: postgresql.UUID(as_uuid=True)  # noqa: E731


def upgrade() -> None:
    op.create_table(
        "symbol_quotes",
        sa.Column("id", UUID(), primary_key=True, server_default=sa.text("gen_random_uuid()")),
        sa.Column("tenant_id", UUID(), nullable=True),
        sa.Column("account_id", UUID(), nullable=False),
        sa.Column("broker_symbol", sa.Text(), nullable=False),
        # SYMBOL_SPREAD in points at observation (integer points, MT5 API)
        sa.Column("spread_points", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("bid", sa.Numeric(20, 8), nullable=True),
        sa.Column("ask", sa.Numeric(20, 8), nullable=True),
        sa.Column("observed_at", TS(), nullable=False),
        sa.Column("created_at", TS(), nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", TS(), nullable=False, server_default=sa.func.now()),
        sa.ForeignKeyConstraint(["account_id"], ["trading_accounts.id"], name="fk_symbol_quotes_account", ondelete="CASCADE"),
        sa.UniqueConstraint("account_id", "broker_symbol", name="uq_symbol_quotes_account_symbol"),
    )


def downgrade() -> None:
    op.drop_table("symbol_quotes")
