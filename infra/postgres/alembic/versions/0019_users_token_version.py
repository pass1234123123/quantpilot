"""Add users.token_version (R2 remediation P2-01/P2-02: revocable JWTs).

JWTs re-validate the user row on every request. Bumping token_version
(password reset, role change, disable) supersedes every previously issued
token for that user, so live JWTs stop working immediately instead of
living on until their 8h expiry. Existing tokens carry no "ver" claim
and count as version 0, matching the column default.

Revision ID: 0019
Revises: 0018
Create Date: 2026-10-31
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0019"
down_revision: Union[str, None] = "0018"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "users",
        sa.Column("token_version", sa.Integer(), nullable=False, server_default="0"),
    )


def downgrade() -> None:
    op.drop_column("users", "token_version")
