"""Strategy engine: bars store + strategies/versions/instances/signals (PRD 7).

- symbol_bars: closed OHLCV bars per account+symbol+timeframe (doc 5.1 market
  snapshot / 7.2 strategy input). Upsert key (account, symbol, timeframe,
  bar_time) makes EA backfill, new-bar updates and JetStream redeliveries
  idempotent.
- strategies / strategy_versions: immutable strategy catalog (doc 7.6).
  Built-in Python strategies are seeded with their config schema; versions
  are append-only rows bound to an implementation artifact marker.
- strategy_instances: one binding of (account, strategy version, symbol,
  timeframe, config revision). Instance configs are validated against the
  version's config schema; live versions are never auto-replaced (7.6).
- strategy_signals: output of the strategy engine (doc 7.4 structure inside
  signal_json). One row max per (instance, source bar) - re-delivered bar
  batches never duplicate signals.

Signals are advisory: the strategy engine never submits orders (PRD 7.1);
the signal -> risk gate -> command path is a later slice.

Revision ID: 0010
Revises: 0009
Create Date: 2026-10-31
"""
import json
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0010"
down_revision: Union[str, None] = "0009"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

TS = lambda: postgresql.TIMESTAMP(timezone=True)  # noqa: E731
UUID = lambda: postgresql.UUID(as_uuid=True)  # noqa: E731

TIMEFRAMES = ("M5", "M15", "M30", "H1", "H4")

STRATEGY_SEEDS = [
    (
        "ma_cross",
        "Moving Average Cross",
        "Fast/slow moving-average crossover on closed bars (doc 7.3 #2).",
        {
            "type": "object",
            "additionalProperties": False,
            "properties": {
                "fast_period": {"type": "integer", "minimum": 2, "maximum": 200},
                "slow_period": {"type": "integer", "minimum": 3, "maximum": 400},
                "ma_type": {"enum": ["EMA", "SMA"]},
                "atr_period": {"type": "integer", "minimum": 2, "maximum": 100},
                "sl_atr_multiple": {"type": "string"},
                "tp_atr_multiple": {"type": "string"},
            },
        },
    ),
    (
        "rsi_momentum",
        "RSI/MACD Momentum",
        "MACD histogram flip with an RSI momentum band filter (doc 7.3 #3).",
        {
            "type": "object",
            "additionalProperties": False,
            "properties": {
                "macd_fast": {"type": "integer", "minimum": 2, "maximum": 100},
                "macd_slow": {"type": "integer", "minimum": 3, "maximum": 200},
                "macd_signal": {"type": "integer", "minimum": 2, "maximum": 100},
                "rsi_period": {"type": "integer", "minimum": 2, "maximum": 100},
                "rsi_buy_min": {"type": "integer", "minimum": 50, "maximum": 90},
                "rsi_buy_max": {"type": "integer", "minimum": 50, "maximum": 95},
                "rsi_sell_max": {"type": "integer", "minimum": 5, "maximum": 50},
                "rsi_sell_min": {"type": "integer", "minimum": 1, "maximum": 50},
                "atr_period": {"type": "integer", "minimum": 2, "maximum": 100},
                "sl_atr_multiple": {"type": "string"},
                "tp_atr_multiple": {"type": "string"},
            },
        },
    ),
    (
        "trend_following",
        "Trend Following",
        "EMA trend direction gated by ADX strength; ATR-based stops (doc 7.3 #1).",
        {
            "type": "object",
            "additionalProperties": False,
            "properties": {
                "ema_period": {"type": "integer", "minimum": 2, "maximum": 400},
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
    op.create_table(
        "symbol_bars",
        sa.Column("id", UUID(), primary_key=True, server_default=sa.text("gen_random_uuid()")),
        sa.Column("account_id", UUID(), nullable=False),
        sa.Column("broker_symbol", sa.Text(), nullable=False),
        sa.Column("timeframe", sa.Text(), nullable=False),
        sa.Column("bar_time", TS(), nullable=False),
        sa.Column("open", sa.Numeric(), nullable=False),
        sa.Column("high", sa.Numeric(), nullable=False),
        sa.Column("low", sa.Numeric(), nullable=False),
        sa.Column("close", sa.Numeric(), nullable=False),
        sa.Column("tick_volume", sa.BigInteger(), nullable=False, server_default="0"),
        sa.Column("created_at", TS(), nullable=False, server_default=sa.func.now()),
        sa.CheckConstraint("timeframe IN ('M5', 'M15', 'M30', 'H1', 'H4')",
                           name="ck_symbol_bars_timeframe"),
        sa.CheckConstraint("high >= low AND low <= open AND low <= close AND high >= open AND high >= close",
                           name="ck_symbol_bars_ohlc"),
        sa.ForeignKeyConstraint(["account_id"], ["trading_accounts.id"],
                                name="fk_symbol_bars_account", ondelete="CASCADE"),
    )
    op.create_unique_constraint("uq_symbol_bars_key", "symbol_bars",
                                ["account_id", "broker_symbol", "timeframe", "bar_time"])
    op.create_index("ix_symbol_bars_lookup", "symbol_bars",
                    ["account_id", "broker_symbol", "timeframe", "bar_time"])

    op.create_table(
        "strategies",
        sa.Column("id", UUID(), primary_key=True, server_default=sa.text("gen_random_uuid()")),
        sa.Column("code", sa.Text(), nullable=False),
        sa.Column("name", sa.Text(), nullable=False),
        sa.Column("description", sa.Text(), nullable=False, server_default=""),
        sa.Column("status", sa.Text(), nullable=False, server_default="ACTIVE"),
        sa.Column("created_at", TS(), nullable=False, server_default=sa.func.now()),
        sa.CheckConstraint("status IN ('ACTIVE', 'DISABLED')", name="ck_strategies_status"),
    )
    op.create_unique_constraint("uq_strategies_code", "strategies", ["code"])

    op.create_table(
        "strategy_versions",
        sa.Column("id", UUID(), primary_key=True, server_default=sa.text("gen_random_uuid()")),
        sa.Column("strategy_id", UUID(), nullable=False),
        sa.Column("version", sa.Text(), nullable=False),
        # implementation binding: built-in Python strategy marker for now;
        # external artifacts carry their sha256 (doc 7.6 immutable versions)
        sa.Column("artifact_hash", sa.Text(), nullable=False, server_default="py-builtin"),
        sa.Column("config_schema", postgresql.JSONB(), nullable=False),
        sa.Column("created_at", TS(), nullable=False, server_default=sa.func.now()),
        sa.CheckConstraint("version <> ''", name="ck_strategy_versions_version"),
        sa.ForeignKeyConstraint(["strategy_id"], ["strategies.id"],
                                name="fk_strategy_versions_strategy", ondelete="CASCADE"),
    )
    op.create_unique_constraint("uq_strategy_versions_strategy_version",
                                "strategy_versions", ["strategy_id", "version"])

    op.create_table(
        "strategy_instances",
        sa.Column("id", UUID(), primary_key=True, server_default=sa.text("gen_random_uuid()")),
        sa.Column("account_id", UUID(), nullable=False),
        sa.Column("strategy_version_id", UUID(), nullable=False),
        sa.Column("broker_symbol", sa.Text(), nullable=False),
        sa.Column("timeframe", sa.Text(), nullable=False),
        sa.Column("config", postgresql.JSONB(), nullable=False),
        sa.Column("config_revision", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("status", sa.Text(), nullable=False, server_default="ENABLED"),
        sa.Column("created_at", TS(), nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", TS(), nullable=False, server_default=sa.func.now()),
        sa.Column("version", sa.Integer(), nullable=False, server_default="1"),
        sa.CheckConstraint("timeframe IN ('M5', 'M15', 'M30', 'H1', 'H4')",
                           name="ck_strategy_instances_timeframe"),
        sa.CheckConstraint("status IN ('ENABLED', 'DISABLED')", name="ck_strategy_instances_status"),
        sa.ForeignKeyConstraint(["account_id"], ["trading_accounts.id"],
                                name="fk_strategy_instances_account", ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["strategy_version_id"], ["strategy_versions.id"],
                                name="fk_strategy_instances_version", ondelete="RESTRICT"),
    )
    op.create_unique_constraint("uq_strategy_instances_binding", "strategy_instances",
                                ["account_id", "strategy_version_id", "broker_symbol", "timeframe"])
    op.create_index("ix_strategy_instances_match", "strategy_instances",
                    ["account_id", "broker_symbol", "timeframe", "status"])

    op.create_table(
        "strategy_signals",
        sa.Column("id", UUID(), primary_key=True, server_default=sa.text("gen_random_uuid()")),
        sa.Column("signal_id", UUID(), nullable=False),
        sa.Column("account_id", UUID(), nullable=False),
        sa.Column("strategy_instance_id", UUID(), nullable=False),
        # doc 7.4 standard signal structure
        sa.Column("signal_json", postgresql.JSONB(), nullable=False),
        sa.Column("source_bar_time", TS(), nullable=False),
        sa.Column("generated_at", TS(), nullable=False, server_default=sa.func.now()),
        sa.Column("expires_at", TS(), nullable=True),
        sa.ForeignKeyConstraint(["account_id"], ["trading_accounts.id"],
                                name="fk_strategy_signals_account", ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["strategy_instance_id"], ["strategy_instances.id"],
                                name="fk_strategy_signals_instance", ondelete="CASCADE"),
    )
    op.create_unique_constraint("uq_strategy_signals_signal", "strategy_signals", ["signal_id"])
    # one evaluation per instance per source bar: redelivered bar batches
    # never duplicate signals (doc 7.5 dedup)
    op.create_unique_constraint("uq_strategy_signals_instance_bar", "strategy_signals",
                                 ["strategy_instance_id", "source_bar_time"])
    op.create_index("ix_strategy_signals_account_generated", "strategy_signals",
                    ["account_id", "generated_at"])

    # seed the built-in strategy catalog (doc 7.3 first batch subset)
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
    op.drop_index("ix_strategy_signals_account_generated", table_name="strategy_signals")
    op.drop_constraint("uq_strategy_signals_instance_bar", "strategy_signals")
    op.drop_constraint("uq_strategy_signals_signal", "strategy_signals")
    op.drop_table("strategy_signals")
    op.drop_index("ix_strategy_instances_match", table_name="strategy_instances")
    op.drop_constraint("uq_strategy_instances_binding", "strategy_instances")
    op.drop_table("strategy_instances")
    op.drop_constraint("uq_strategy_versions_strategy_version", "strategy_versions")
    op.drop_table("strategy_versions")
    op.drop_constraint("uq_strategies_code", "strategies")
    op.drop_table("strategies")
    op.drop_index("ix_symbol_bars_lookup", table_name="symbol_bars")
    op.drop_constraint("uq_symbol_bars_key", "symbol_bars")
    op.drop_table("symbol_bars")
