"""Add account_snapshots.ea_run_mode (phase-3 P2: single mode definition).

The EA reports run_mode (OBSERVE_ONLY | DRY_RUN | LIVE, derived from its
DryRunMode / LiveTrading inputs) in every heartbeat; the cloud stores it
verbatim so the UI and docs display the REAL mode instead of hardcoding
OBSERVE_ONLY. NULL = not reported (pre-P2 EA builds, spoofed labels);
the UI must render that as unknown, never guess.

Revision ID: 0018
Revises: 0017
Create Date: 2026-10-31
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0018"
down_revision: Union[str, None] = "0017"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("account_snapshots", sa.Column("ea_run_mode", sa.Text(), nullable=True))


def downgrade() -> None:
    op.drop_column("account_snapshots", "ea_run_mode")
