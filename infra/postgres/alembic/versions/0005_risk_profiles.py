"""risk_profiles: versioned per-account trading config (doc 12.2).

Stores the cloud-side filter/expert configuration handed to (and, in this
slice, only observed by) the EA. Writes are append-only versions: saving a
new config supersedes the ACTIVE row and inserts version = max+1, so the
history of every configuration change is preserved (doc 21.1 auditability).

config_json is JSONB - the API validates its shape with Pydantic models
(extra="forbid") before writing; the DB keeps the canonical document.

Revision ID: 0005
Revises: 0004
Create Date: 2026-10-31
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0005"
down_revision: Union[str, None] = "0004"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

TS = lambda: postgresql.TIMESTAMP(timezone=True)  # noqa: E731
UUID = lambda: postgresql.UUID(as_uuid=True)  # noqa: E731


def upgrade() -> None:
    op.create_table(
        "risk_profiles",
        sa.Column("id", UUID(), primary_key=True, server_default=sa.text("gen_random_uuid()")),
        sa.Column("tenant_id", UUID(), nullable=True),
        sa.Column("account_id", UUID(), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("config_json", postgresql.JSONB(), nullable=False),
        sa.Column("status", sa.Text(), nullable=False, server_default="ACTIVE"),  # ACTIVE | SUPERSEDED
        sa.Column("created_at", TS(), nullable=False, server_default=sa.func.now()),
        sa.ForeignKeyConstraint(["account_id"], ["trading_accounts.id"], name="fk_risk_profiles_account", ondelete="CASCADE"),
        sa.UniqueConstraint("account_id", "version", name="uq_risk_profiles_account_version"),
    )
    op.create_index("ix_risk_profiles_account_status", "risk_profiles", ["account_id", "status"])


def downgrade() -> None:
    op.drop_index("ix_risk_profiles_account_status", table_name="risk_profiles")
    op.drop_table("risk_profiles")
