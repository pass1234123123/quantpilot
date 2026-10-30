"""Rename broker_symbols.selectable -> selected.

The deployed MT5 build exposes SYMBOL_SELECT (is the symbol selected in
Market Watch), not SYMBOL_SELECTABLE. The contract field (and therefore the
EA payload, writers and API queries) is named "selected"; the column must
match (doc 21.3: no fabricated API surface).

Revision ID: 0002
Revises: 0001
Create Date: 2026-10-30
"""
from typing import Sequence, Union

from alembic import op

revision: str = "0002"
down_revision: Union[str, None] = "0001"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.alter_column("broker_symbols", "selectable", new_column_name="selected")


def downgrade() -> None:
    op.alter_column("broker_symbols", "selected", new_column_name="selectable")
