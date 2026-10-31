"""Seed the breakout + mean_reversion strategies (PRD 7.3 #4/#5, doc 12.2).

Seed-only migration following the 0010 pattern: upsert the two catalog
rows and append their 1.0.0 py-builtin versions with the config schema
mirroring the Pydantic models in services/api .../strategies/{breakout,
mean_reversion}.py (the API validates instance configs against the
implementation, the schema here drives the catalog/UI).

Downgrade deletes the two seeded strategies AND everything bound to
them (instances, their signals) - destructive by nature, run it only
when removing the strategies for good.

Revision ID: 0014
Revises: 0013
Create Date: 2026-10-31
"""
import json
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0014"
down_revision: Union[str, None] = "0013"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

STRATEGY_SEEDS = [
    (
        "breakout",
        "Breakout",
        "Close breaks the prior lookback-bar high/low range out of a "
        "volatility-compressed base; ATR-based stops (doc 7.3 #4).",
        {
            "type": "object",
            "additionalProperties": False,
            "properties": {
                "lookback_period": {"type": "integer", "minimum": 5, "maximum": 200},
                "squeeze_width_max": {"type": "string"},
                "atr_period": {"type": "integer", "minimum": 2, "maximum": 100},
                "sl_atr_multiple": {"type": "string"},
                "tp_atr_multiple": {"type": "string"},
            },
        },
    ),
    (
        "mean_reversion",
        "Mean Reversion",
        "Bollinger z-score stretch that starts reverting, blocked while "
        "ADX reports a strong trend; ATR-based stops (doc 7.3 #5).",
        {
            "type": "object",
            "additionalProperties": False,
            "properties": {
                "bollinger_period": {"type": "integer", "minimum": 5, "maximum": 200},
                "std_multiple": {"type": "string"},
                "entry_z": {"type": "string"},
                "adx_period": {"type": "integer", "minimum": 2, "maximum": 100},
                "adx_threshold": {"type": "integer", "minimum": 10, "maximum": 50},
                "atr_period": {"type": "integer", "minimum": 2, "maximum": 100},
                "sl_atr_multiple": {"type": "string"},
                "tp_atr_multiple": {"type": "string"},
            },
        },
    ),
]


def upgrade() -> None:
    for code, name, description, schema in STRATEGY_SEEDS:
        strategy_id = op.get_bind().execute(
            sa.text(
                "INSERT INTO strategies (code, name, description, status)"
                " VALUES (:code, :name, :description, 'ACTIVE')"
                " ON CONFLICT (code) DO UPDATE SET name = EXCLUDED.name"
                " RETURNING id"
            ),
            {"code": code, "name": name, "description": description},
        ).scalar()
        op.get_bind().execute(
            sa.text(
                "INSERT INTO strategy_versions (strategy_id, version, artifact_hash, config_schema)"
                " VALUES (:strategy_id, '1.0.0', 'py-builtin', :schema)"
                " ON CONFLICT (strategy_id, version) DO NOTHING"
            ),
            {"strategy_id": str(strategy_id), "schema": json.dumps(schema)},
        )


def downgrade() -> None:
    op.get_bind().execute(
        sa.text(
            "DELETE FROM strategy_instances WHERE strategy_version_id IN ("
            " SELECT v.id FROM strategy_versions v"
            "  JOIN strategies s ON v.strategy_id = s.id"
            " WHERE s.code IN ('breakout', 'mean_reversion'))"
        )
    )
    op.get_bind().execute(
        sa.text(
            "DELETE FROM strategy_versions WHERE strategy_id IN ("
            " SELECT id FROM strategies WHERE code IN ('breakout', 'mean_reversion'))"
        )
    )
    op.get_bind().execute(
        sa.text("DELETE FROM strategies WHERE code IN ('breakout', 'mean_reversion')")
    )
