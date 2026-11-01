"""Add system_events.account_id (R3 P2-01: tenant-scoped event visibility).

GET /api/v1/events previously returned the whole table to every named
user, including other tenants' command/auth activity. This migration:

* adds a nullable account_id column,
* backfills it from payload->>'account_id' (valid UUIDs referencing an
  existing trading_accounts row only - 1220/1291 production rows match),
* enforces an FK and an (account_id, occurred_at DESC) index.

Rows that stay NULL are global/system-wide facts (auth events, node
lifecycle, pipeline errors): visible to ADMIN only, deny-by-default for
everyone else at query time.

Revision ID: 0020
Revises: 0019
Create Date: 2026-11-01
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0020"
down_revision: Union[str, None] = "0019"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

_UUID_RE = (
    "^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-"
    "[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$"
)


def upgrade() -> None:
    op.add_column("system_events", sa.Column("account_id", sa.UUID(), nullable=True))
    # backfill: only payload account_ids that are well-formed UUIDs AND
    # reference an existing trading_accounts row (orphan refs stay NULL,
    # which reads as "global" and is therefore ADMIN-only - fail-closed)
    op.execute(
        f"""
        UPDATE system_events se
           SET account_id = (se.payload->>'account_id')::uuid
         WHERE se.payload ? 'account_id'
           AND (se.payload->>'account_id') ~ '{_UUID_RE}'
           AND EXISTS (SELECT 1 FROM trading_accounts ta
                        WHERE ta.id = (se.payload->>'account_id')::uuid)
        """
    )
    op.execute(
        "ALTER TABLE system_events "
        "ADD CONSTRAINT fk_system_events_account "
        "FOREIGN KEY (account_id) REFERENCES trading_accounts (id)"
    )
    op.execute(
        "CREATE INDEX ix_system_events_account_occurred "
        "ON system_events (account_id, occurred_at DESC)"
    )


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS ix_system_events_account_occurred")
    op.execute("ALTER TABLE system_events DROP CONSTRAINT IF EXISTS fk_system_events_account")
    op.drop_column("system_events", "account_id")
