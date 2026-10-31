"""PRD 9.5 six-state circuit breaker: extend risk_states.status.

NORMAL -> WARNING -> STOP_NEW_RISK -> CANCEL_PENDING -> MANAGE_EXISTING
      -> LOCKED

Previously only the subset ('NORMAL', 'STOP_NEW_RISK', 'LOCKED') was
accepted. WARNING is an early warning that does not block new risk at the
cloud gate; CANCEL_PENDING / MANAGE_EXISTING are manually entered breaker
states (their EA-side order actions ship with the execution slices).

Revision ID: 0013
Revises: 0012
Create Date: 2026-10-31
"""
from typing import Sequence, Union

from alembic import op

revision: str = "0013"
down_revision: Union[str, None] = "0012"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

SIX = ("NORMAL", "WARNING", "STOP_NEW_RISK", "CANCEL_PENDING",
       "MANAGE_EXISTING", "LOCKED")
THREE = ("NORMAL", "STOP_NEW_RISK", "LOCKED")


def upgrade() -> None:
    op.drop_constraint("ck_risk_states_status", "risk_states", type_="check")
    op.create_check_constraint(
        "ck_risk_states_status", "risk_states",
        f"status IN {SIX}",
    )


def downgrade() -> None:
    # fold states that the old subset does not know back into it:
    # WARNING never blocked anything -> NORMAL; the two escalated states
    # still block new risk -> STOP_NEW_RISK (LOCKED already existed)
    op.execute(
        "UPDATE risk_states SET status = CASE"
        "   WHEN status = 'WARNING' THEN 'NORMAL'"
        "   ELSE 'STOP_NEW_RISK'"
        " END"
        " WHERE status IN ('WARNING', 'CANCEL_PENDING', 'MANAGE_EXISTING')"
    )
    op.drop_constraint("ck_risk_states_status", "risk_states", type_="check")
    op.create_check_constraint(
        "ck_risk_states_status", "risk_states",
        f"status IN {THREE}",
    )
