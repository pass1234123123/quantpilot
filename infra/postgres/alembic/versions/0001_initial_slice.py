"""Initial slice: nodes/terminals/accounts/snapshots/symbols/specs/system_events

Tables follow PRD 12.2 core tables; indexes/constraints follow 12.3.
Money columns are NUMERIC (never float, per doc 21.3).
tenant_id is reserved for future multi-tenancy and is nullable everywhere.

Revision ID: 0001
Revises:
Create Date: 2026-10-30
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0001"
down_revision: Union[str, None] = None
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

TS = lambda: postgresql.TIMESTAMP(timezone=True)  # noqa: E731
UUID = lambda: postgresql.UUID(as_uuid=True)  # noqa: E731


def _audit_cols() -> list[sa.Column]:
    return [
        sa.Column("created_at", TS(), nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", TS(), nullable=False, server_default=sa.func.now()),
        sa.Column("version", sa.Integer(), nullable=False, server_default="1"),
    ]


def upgrade() -> None:
    # --- trading_nodes (doc 12.2: id, owner_id, device_id, status, version, last_seen_at) ---
    op.create_table(
        "trading_nodes",
        sa.Column("id", UUID(), primary_key=True, server_default=sa.text("gen_random_uuid()")),
        sa.Column("tenant_id", UUID(), nullable=True),
        sa.Column("owner_id", UUID(), nullable=True),
        sa.Column("device_id", sa.Text(), nullable=False),
        sa.Column("name", sa.Text(), nullable=True),
        sa.Column("hostname", sa.Text(), nullable=True),
        sa.Column("os_info", sa.Text(), nullable=True),
        sa.Column("agent_version", sa.Text(), nullable=True),
        sa.Column("status", sa.Text(), nullable=False, server_default="ONLINE"),
        sa.Column("last_seen_at", TS(), nullable=True),
        *_audit_cols(),
        sa.UniqueConstraint("device_id", name="uq_trading_nodes_device_id"),
    )

    # --- mt5_terminals ---
    # terminal_path_hash = sha256 of the terminal DATA path (stable instance identity,
    # matches Agent terminal discovery via origin.txt). Doc 12.3:
    # UNIQUE (node_id, terminal_path_hash)
    op.create_table(
        "mt5_terminals",
        sa.Column("id", UUID(), primary_key=True, server_default=sa.text("gen_random_uuid()")),
        sa.Column("tenant_id", UUID(), nullable=True),
        sa.Column("node_id", UUID(), nullable=False),
        sa.Column("terminal_path_hash", sa.Text(), nullable=False),
        sa.Column("terminal_build", sa.Integer(), nullable=True),
        sa.Column("terminal_company", sa.Text(), nullable=True),
        sa.Column("server", sa.Text(), nullable=True),
        sa.Column("status", sa.Text(), nullable=False, server_default="ACTIVE"),
        sa.Column("last_seen_at", TS(), nullable=True),
        *_audit_cols(),
        sa.ForeignKeyConstraint(["node_id"], ["trading_nodes.id"], name="fk_terminals_node", ondelete="CASCADE"),
        sa.UniqueConstraint("node_id", "terminal_path_hash", name="uq_terminals_node_path_hash"),
    )

    # --- trading_accounts (sensitive credentials are never stored cloud-side) ---
    op.create_table(
        "trading_accounts",
        sa.Column("id", UUID(), primary_key=True, server_default=sa.text("gen_random_uuid()")),
        sa.Column("tenant_id", UUID(), nullable=True),
        sa.Column("terminal_id", UUID(), nullable=False),
        sa.Column("login_ref", sa.Text(), nullable=False),
        sa.Column("server", sa.Text(), nullable=False),
        sa.Column("company", sa.Text(), nullable=True),
        sa.Column("currency", sa.Text(), nullable=True),
        sa.Column("margin_mode", sa.Text(), nullable=True),
        sa.Column("account_mode", sa.Text(), nullable=True),
        sa.Column("leverage", sa.Integer(), nullable=True),
        # accounts start OBSERVE_ONLY; never auto-enable trading (doc 2.5)
        sa.Column("status", sa.Text(), nullable=False, server_default="OBSERVE_ONLY"),
        sa.Column("last_seen_at", TS(), nullable=True),
        *_audit_cols(),
        sa.ForeignKeyConstraint(["terminal_id"], ["mt5_terminals.id"], name="fk_accounts_terminal", ondelete="CASCADE"),
        sa.UniqueConstraint("terminal_id", "login_ref", "server", name="uq_accounts_terminal_login_server"),
    )

    # --- account_snapshots (money NUMERIC) ---
    op.create_table(
        "account_snapshots",
        sa.Column("id", UUID(), primary_key=True, server_default=sa.text("gen_random_uuid()")),
        sa.Column("account_id", UUID(), nullable=False),
        sa.Column("balance", sa.Numeric(20, 8), nullable=False),
        sa.Column("equity", sa.Numeric(20, 8), nullable=True),
        sa.Column("margin", sa.Numeric(20, 8), nullable=True),
        sa.Column("free_margin", sa.Numeric(20, 8), nullable=True),
        sa.Column("currency", sa.Text(), nullable=True),
        sa.Column("open_positions", sa.Integer(), nullable=True),
        sa.Column("connected", sa.Boolean(), nullable=True),
        sa.Column("trade_allowed", sa.Boolean(), nullable=True),
        sa.Column("expert_allowed", sa.Boolean(), nullable=True),
        sa.Column("terminal_version", sa.Text(), nullable=True),
        sa.Column("ea_version", sa.Text(), nullable=True),
        sa.Column("captured_at", TS(), nullable=False),
        sa.Column("created_at", TS(), nullable=False, server_default=sa.func.now()),
        sa.ForeignKeyConstraint(["account_id"], ["trading_accounts.id"], name="fk_snapshots_account", ondelete="CASCADE"),
    )
    op.create_index(
        "ix_snapshots_account_captured",
        "account_snapshots",
        ["account_id", sa.text("captured_at DESC")],
    )

    # --- symbols (canonical registry) ---
    op.create_table(
        "symbols",
        sa.Column("id", UUID(), primary_key=True, server_default=sa.text("gen_random_uuid()")),
        sa.Column("tenant_id", UUID(), nullable=True),
        sa.Column("canonical_instrument", sa.Text(), nullable=False),
        sa.Column("asset_class", sa.Text(), nullable=True),
        *_audit_cols(),
        sa.UniqueConstraint("canonical_instrument", name="uq_symbols_canonical"),
    )

    # --- broker_symbols (per-account broker-facing symbols) ---
    op.create_table(
        "broker_symbols",
        sa.Column("id", UUID(), primary_key=True, server_default=sa.text("gen_random_uuid()")),
        sa.Column("tenant_id", UUID(), nullable=True),
        sa.Column("account_id", UUID(), nullable=False),
        sa.Column("broker_symbol", sa.Text(), nullable=False),
        sa.Column("canonical_symbol_id", UUID(), nullable=False),
        sa.Column("enabled", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("visible", sa.Boolean(), nullable=True),
        sa.Column("selectable", sa.Boolean(), nullable=True),
        sa.Column("tradable", sa.Boolean(), nullable=True),
        *_audit_cols(),
        sa.ForeignKeyConstraint(["account_id"], ["trading_accounts.id"], name="fk_broker_symbols_account", ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["canonical_symbol_id"], ["symbols.id"], name="fk_broker_symbols_canonical"),
        sa.UniqueConstraint("account_id", "broker_symbol", name="uq_broker_symbols_account_symbol"),
    )

    # --- symbol_specs (immutable versions keyed by spec_hash) ---
    op.create_table(
        "symbol_specs",
        sa.Column("id", UUID(), primary_key=True, server_default=sa.text("gen_random_uuid()")),
        sa.Column("tenant_id", UUID(), nullable=True),
        sa.Column("broker_symbol_id", UUID(), nullable=False),
        sa.Column("spec_json", postgresql.JSONB(), nullable=False),
        sa.Column("spec_hash", sa.Text(), nullable=False),
        sa.Column("observed_at", TS(), nullable=False),
        sa.Column("created_at", TS(), nullable=False, server_default=sa.func.now()),
        sa.ForeignKeyConstraint(["broker_symbol_id"], ["broker_symbols.id"], name="fk_specs_broker_symbol", ondelete="CASCADE"),
        sa.UniqueConstraint("broker_symbol_id", "spec_hash", name="uq_specs_broker_symbol_hash"),
    )

    # --- system_events (doc 12.2/12.3) ---
    op.create_table(
        "system_events",
        sa.Column("id", UUID(), primary_key=True, server_default=sa.text("gen_random_uuid()")),
        sa.Column("tenant_id", UUID(), nullable=True),
        sa.Column("source", sa.Text(), nullable=False),
        sa.Column("event_type", sa.Text(), nullable=False),
        sa.Column("severity", sa.Text(), nullable=False, server_default="INFO"),
        sa.Column("payload", postgresql.JSONB(), nullable=True),
        sa.Column("occurred_at", TS(), nullable=False),
        sa.Column("created_at", TS(), nullable=False, server_default=sa.func.now()),
    )
    op.create_index("ix_system_events_severity_occurred", "system_events", ["severity", "occurred_at"])
    op.create_index("ix_system_events_occurred", "system_events", ["occurred_at"])


def downgrade() -> None:
    op.drop_table("system_events")
    op.drop_table("symbol_specs")
    op.drop_table("broker_symbols")
    op.drop_table("symbols")
    op.drop_index("ix_snapshots_account_captured", table_name="account_snapshots")
    op.drop_table("account_snapshots")
    op.drop_table("trading_accounts")
    op.drop_table("mt5_terminals")
    op.drop_table("trading_nodes")
