"""calendar_events: MT5 built-in economic calendar per account (doc 12.2).

The EA reports a rolling window of broker-provided calendar events
(MqlCalendarValue/Event/Country, read-only). Rows are upserted by
(account_id, event_uid): a re-report of the same occurrence updates
actual/forecast/previous once values are released, never duplicates.

Value columns are the raw broker longs (see contracts CalendarEventRecord);
multiplier/digits travel alongside for display formatting. NUMERIC per
doc 21.3.

Revision ID: 0007
Revises: 0006
Create Date: 2026-10-31
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0007"
down_revision: Union[str, None] = "0006"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

TS = lambda: postgresql.TIMESTAMP(timezone=True)  # noqa: E731
UUID = lambda: postgresql.UUID(as_uuid=True)  # noqa: E731


def upgrade() -> None:
    op.create_table(
        "calendar_events",
        sa.Column("id", UUID(), primary_key=True, server_default=sa.text("gen_random_uuid()")),
        sa.Column("tenant_id", UUID(), nullable=True),
        sa.Column("account_id", UUID(), nullable=False),
        sa.Column("event_uid", sa.Text(), nullable=False),
        sa.Column("country_code", sa.Text(), nullable=False, server_default=""),
        sa.Column("currency", sa.Text(), nullable=False, server_default=""),
        sa.Column("name", sa.Text(), nullable=False),
        sa.Column("importance", sa.Text(), nullable=False, server_default="NONE"),  # NONE | LOW | MODERATE | HIGH
        sa.Column("multiplier", sa.Text(), nullable=False, server_default="NONE"),
        sa.Column("digits", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("event_time", TS(), nullable=False),
        sa.Column("actual", sa.Numeric(24, 8), nullable=True),
        sa.Column("forecast", sa.Numeric(24, 8), nullable=True),
        sa.Column("previous", sa.Numeric(24, 8), nullable=True),
        sa.Column("observed_at", TS(), nullable=False),
        sa.Column("created_at", TS(), nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", TS(), nullable=False, server_default=sa.func.now()),
        sa.Column("version", sa.Integer(), nullable=False, server_default="1"),
        sa.ForeignKeyConstraint(["account_id"], ["trading_accounts.id"], name="fk_calendar_events_account", ondelete="CASCADE"),
        sa.UniqueConstraint("account_id", "event_uid", name="uq_calendar_events_account_uid"),
    )
    op.create_index("ix_calendar_events_account_time", "calendar_events", ["account_id", "event_time"])


def downgrade() -> None:
    op.drop_index("ix_calendar_events_account_time", table_name="calendar_events")
    op.drop_table("calendar_events")
