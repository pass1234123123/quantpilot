"""Durable, idempotent backtest jobs and reports (PRD 12.2/13.2/15).

POST replays are resolved BEFORE loading the instance or bars: the same key
and request returns the original immutable report, while key reuse with a
different request conflicts. Jobs snapshot strategy version/config revision
and the exact input-bar hash; no trade command is ever created.
"""

from __future__ import annotations

import hashlib
import json
import uuid
from datetime import datetime
from decimal import Decimal
from typing import Any
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, model_validator

from . import db
from .backtest_engine import BacktestCostModel, run_backtest
from .strategies import REGISTRY, Bar
from .strategies.base import DecimalStr


class BacktestConflict(Exception):
    pass


class BacktestNotFound(Exception):
    pass


class BacktestCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    strategy_instance_id: UUID
    range_start: datetime
    range_end: datetime
    initial_balance: DecimalStr = Field(default=Decimal("10000"), gt=0)
    volume: DecimalStr = Field(default=Decimal("1"), gt=0)
    cost_model: BacktestCostModel = Field(default_factory=BacktestCostModel)

    @model_validator(mode="after")
    def _valid_utc_range(self) -> "BacktestCreate":
        if self.range_start.tzinfo is None or self.range_start.utcoffset() is None:
            raise ValueError("range_start must include a timezone")
        if self.range_end.tzinfo is None or self.range_end.utcoffset() is None:
            raise ValueError("range_end must include a timezone")
        if self.range_end <= self.range_start:
            raise ValueError("range_end must be after range_start")
        return self


def deterministic_backtest_id(idempotency_key: UUID) -> UUID:
    return uuid.uuid5(uuid.NAMESPACE_URL, f"quantpilot-backtest-v1:{idempotency_key}")


def list_backtests(
    account_id: UUID | None = None,
    strategy_instance_id: UUID | None = None,
    limit: int = 100,
    granted_user_id: str | None = None,
) -> list[dict[str, Any]]:
    """Job summaries, newest first; optional account/instance filters.

    granted_user_id narrows the unfiltered view to that user's granted
    accounts (R2 acceptance P1-02); explicit account_ids are grant-checked
    by the caller before reaching here."""
    where = ["1=1"]
    params: list[Any] = []
    if account_id is not None:
        where.append("j.account_id = %s")
        params.append(account_id)
    if granted_user_id is not None:
        where.append(
            "j.account_id IN (SELECT account_id FROM user_account_grants WHERE user_id = %s)"
        )
        params.append(granted_user_id)
    if strategy_instance_id is not None:
        where.append("j.strategy_instance_id = %s")
        params.append(strategy_instance_id)
    params.append(max(1, min(limit, 500)))
    return db.query(
        f"""
        SELECT j.id::text AS job_id, j.account_id::text AS account_id,
               s.code AS strategy_code, j.broker_symbol, j.timeframe,
               j.status, j.range_start, j.range_end, j.config_revision,
               j.initial_balance, j.volume, j.data_hash, j.error,
               j.created_at, j.completed_at
          FROM backtest_jobs j
          JOIN strategy_versions sv ON sv.id = j.strategy_version_id
          JOIN strategies s ON s.id = sv.strategy_id
         WHERE {" AND ".join(where)}
         ORDER BY j.created_at DESC
         LIMIT %s
        """,
        tuple(params),
    )


def get_backtest(job_id: UUID) -> dict[str, Any] | None:
    return db.query_one(
        """
        SELECT j.id::text AS job_id, j.strategy_version_id::text AS strategy_version_id,
               j.strategy_instance_id::text AS strategy_instance_id,
               j.account_id::text AS account_id, s.code AS strategy_code,
               sv.version AS strategy_version, j.broker_symbol, j.timeframe,
               j.range_start, j.range_end, j.config, j.config_revision,
               j.cost_model, j.initial_balance, j.volume, j.status,
               j.idempotency_key::text AS idempotency_key,
               j.request_json AS request, j.request_hash, j.data_hash, j.error,
               j.created_at, j.updated_at, j.completed_at,
               r.metrics_json AS metrics, r.report_json AS report, r.report_uri
          FROM backtest_jobs j
          JOIN strategy_versions sv ON sv.id = j.strategy_version_id
          JOIN strategies s ON s.id = sv.strategy_id
          LEFT JOIN backtest_results r ON r.job_id = j.id
         WHERE j.id = %s
        """,
        (job_id,),
    )


def _instance_or_error(instance_id: UUID) -> dict[str, Any]:
    row = db.query_one(
        """
        SELECT si.id::text AS instance_id, si.account_id::text AS account_id,
               si.strategy_version_id::text AS strategy_version_id,
               s.code AS strategy_code, sv.version AS strategy_version,
               si.broker_symbol, si.timeframe, si.config, si.config_revision
          FROM strategy_instances si
          JOIN strategy_versions sv ON sv.id = si.strategy_version_id
          JOIN strategies s ON s.id = sv.strategy_id
         WHERE si.id = %s
        """,
        (instance_id,),
    )
    if row is None:
        raise BacktestNotFound("strategy instance not found")
    return row


def _request_hash(request: dict[str, Any], instance: dict[str, Any]) -> str:
    snapshot = {
        "request": request,
        "strategy_code": instance["strategy_code"],
        "strategy_version": instance["strategy_version"],
        "config": instance["config"],
        "config_revision": instance["config_revision"],
    }
    canonical = json.dumps(snapshot, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(canonical.encode()).hexdigest()


def _bars_hash(bars: list[Bar]) -> str:
    digest = hashlib.sha256()
    for bar in bars:
        digest.update(
            f"{bar.bar_time.isoformat()}|{bar.open}|{bar.high}|{bar.low}|"
            f"{bar.close}|{bar.tick_volume};".encode()
        )
    return digest.hexdigest()


def _check_replay(stored: dict[str, Any] | None, request: dict[str, Any]) -> dict[str, Any] | None:
    if stored is None:
        return None
    if stored["request"] != request:
        raise BacktestConflict("idempotency key was already used for a different backtest")
    return stored


def create_backtest(body: BacktestCreate, idempotency_key: UUID) -> dict[str, Any]:
    request = body.model_dump(mode="json")
    job_id = deterministic_backtest_id(idempotency_key)
    replay = _check_replay(get_backtest(job_id), request)
    if replay is not None:
        return replay

    instance = _instance_or_error(body.strategy_instance_id)
    cls = REGISTRY.get(instance["strategy_code"])
    if cls is None:
        raise ValueError("no python implementation registered for strategy")
    if instance["strategy_version"] != cls.version:
        raise ValueError("strategy version does not match its registered implementation")
    config = cls.validate_config(instance["config"])
    request_hash = _request_hash(request, instance)

    with db.conn() as connection:
        inserted = connection.execute(
            """
            INSERT INTO backtest_jobs
                (id, strategy_version_id, strategy_instance_id, account_id,
                 broker_symbol, timeframe, range_start, range_end, config,
                 config_revision, cost_model, initial_balance, volume, status,
                 idempotency_key, request_json, request_hash)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, CAST(%s AS jsonb),
                    %s, CAST(%s AS jsonb), %s, %s, 'QUEUED', %s,
                    CAST(%s AS jsonb), %s)
            ON CONFLICT (idempotency_key) DO NOTHING
            RETURNING id::text AS job_id
            """,
            (
                job_id, instance["strategy_version_id"], instance["instance_id"],
                instance["account_id"], instance["broker_symbol"], instance["timeframe"],
                body.range_start, body.range_end, json.dumps(instance["config"]),
                instance["config_revision"], json.dumps(request["cost_model"]),
                body.initial_balance, body.volume, idempotency_key,
                json.dumps(request), request_hash,
            ),
        ).fetchone()
    replay = _check_replay(get_backtest(job_id), request) if inserted is None else None
    if replay is not None:
        return replay

    try:
        with db.conn() as connection:
            connection.execute(
                "UPDATE backtest_jobs SET status = 'RUNNING', updated_at = now() WHERE id = %s",
                (job_id,),
            )
        rows = db.query(
            """
            SELECT bar_time, open, high, low, close, tick_volume
              FROM symbol_bars
             WHERE account_id = %s AND broker_symbol = %s AND timeframe = %s
               AND bar_time >= %s AND bar_time <= %s
             ORDER BY bar_time ASC
            """,
            (
                instance["account_id"], instance["broker_symbol"], instance["timeframe"],
                body.range_start, body.range_end,
            ),
        )
        bars = [Bar(**row) for row in rows]
        data_hash = _bars_hash(bars)
        engine_report = run_backtest(
            bars=bars,
            strategy=cls(),
            config=config,
            timeframe=instance["timeframe"],
            initial_balance=body.initial_balance,
            volume=body.volume,
            cost_model=body.cost_model,
        )
        report = {
            "job_id": str(job_id),
            "strategy": {
                "code": instance["strategy_code"],
                "version": instance["strategy_version"],
                "config": instance["config"],
                "config_revision": instance["config_revision"],
            },
            "data": {
                "bar_count": len(bars),
                "sha256": data_hash,
                "first_bar_time": bars[0].bar_time,
                "last_bar_time": bars[-1].bar_time,
            },
            "cost_model": request["cost_model"],
            **engine_report,
        }
        with db.conn() as connection:
            connection.execute(
                """
                UPDATE backtest_jobs
                   SET status = 'COMPLETED', data_hash = %s, error = '',
                       updated_at = now(), completed_at = now(), version = version + 1
                 WHERE id = %s
                """,
                (data_hash, job_id),
            )
            connection.execute(
                """
                INSERT INTO backtest_results (job_id, metrics_json, report_json, report_uri)
                VALUES (%s, CAST(%s AS jsonb), CAST(%s AS jsonb), %s)
                """,
                (
                    job_id, json.dumps(db.jsonable(engine_report["metrics"])),
                    json.dumps(db.jsonable(report)), f"/api/v1/backtests/{job_id}",
                ),
            )
    except Exception as exc:
        with db.conn() as connection:
            connection.execute(
                """
                UPDATE backtest_jobs
                   SET status = 'FAILED', error = %s, updated_at = now(), version = version + 1
                 WHERE id = %s
                """,
                (f"{type(exc).__name__}: {exc}", job_id),
            )
        raise
    stored = get_backtest(job_id)
    assert stored is not None
    return stored


__all__ = [
    "BacktestConflict",
    "BacktestCreate",
    "BacktestNotFound",
    "create_backtest",
    "deterministic_backtest_id",
    "get_backtest",
    "list_backtests",
]
