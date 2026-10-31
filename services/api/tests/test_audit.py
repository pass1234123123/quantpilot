"""Audit log center (phase-2 task 6): structured operation audit trail.

audit_logs records who/what/when for every state-changing cloud operation
(config saves, instance status/config updates, command creation, backtest
creation, AI triggers). record() is best-effort by design: an audit failure
must never break the audited operation, it logs loudly instead. The query
side powers GET /api/v1/audit-logs.

R2 remediation (P1-01): business routes no longer accept the service key;
route tests ride the Bearer lane as an ADMIN via rbac_testkit.bearer().
"""

import json
import os
import unittest
from datetime import datetime, timedelta, timezone
from unittest.mock import patch
from uuid import UUID, uuid4

from fastapi.testclient import TestClient

from rbac_testkit import bearer

TEST_API_KEY = "test-api-key"


def force_api_key(case: unittest.TestCase) -> None:
    """Force QP_API_KEY so route tests are hermetic against the repo .env."""
    original = os.environ.get("QP_API_KEY")
    os.environ["QP_API_KEY"] = TEST_API_KEY

    def restore() -> None:
        if original is None:
            os.environ.pop("QP_API_KEY", None)
        else:
            os.environ["QP_API_KEY"] = original

    case.addCleanup(restore)


class AuditContractTests(unittest.TestCase):
    """record()/list_audit_logs() shape without touching PostgreSQL."""

    def test_record_inserts_structured_row(self) -> None:
        from quantpilot_api import audit, db

        account = uuid4()
        with patch.object(db, "execute") as execute:
            audit.record(account, "CONFIG_UPDATE", "account_config", "ac-1", {"version": 2})
        sql, params = execute.call_args.args
        self.assertIn("INSERT INTO audit_logs", sql)
        self.assertEqual(params[0], account)
        self.assertEqual(params[1], "api-key")  # single service-key phase (PRD 14)
        self.assertEqual(params[2], "CONFIG_UPDATE")
        self.assertEqual(params[3], "account_config")
        self.assertEqual(params[4], "ac-1")
        self.assertEqual(json.loads(params[5]), {"version": 2})

    def test_record_never_raises(self) -> None:
        from quantpilot_api import audit, db

        with patch.object(db, "execute", side_effect=RuntimeError("db down")):
            audit.record(uuid4(), "X_ACTION", "obj", None, None)  # must not raise

    def test_list_composes_filters_and_orders_newest_first(self) -> None:
        from quantpilot_api import audit, db

        account = uuid4()
        rows = [{"id": "r1"}]
        with patch.object(db, "query", return_value=rows) as query:
            result = audit.list_audit_logs(
                account_id=account, action="CONFIG_UPDATE", limit=25
            )
        sql, params = query.call_args.args
        self.assertIn("l.account_id = %s", sql)
        self.assertIn("l.action = %s", sql)
        self.assertIn("ORDER BY l.created_at DESC", sql)
        self.assertEqual(params[0], account)
        self.assertEqual(params[1], "CONFIG_UPDATE")
        self.assertEqual(params[2], 25)
        self.assertEqual(result, rows)


class AuditLogRouteTests(unittest.TestCase):
    def setUp(self) -> None:
        force_api_key(self)
        import quantpilot_api.app as app_module

        self.client = TestClient(app_module.app)

    def test_route_requires_authentication(self) -> None:
        resp = self.client.get("/api/v1/audit-logs")
        self.assertEqual(resp.status_code, 401)
        self.assertEqual(resp.json()["error"]["code"], "UNAUTHORIZED")

    def test_wraps_envelope_and_passes_filters(self) -> None:
        from quantpilot_api import audit

        account = uuid4()
        rows = [{"id": "r"}]
        with (
            bearer(role="ADMIN") as auth_headers,
            patch.object(audit, "list_audit_logs", return_value=rows) as listed,
        ):
            resp = self.client.get(
                f"/api/v1/audit-logs?account_id={account}&action=CONFIG_UPDATE&limit=7",
                headers=auth_headers,
            )
        self.assertEqual(resp.status_code, 200, resp.text)
        self.assertIsNone(resp.json()["error"])
        self.assertEqual(resp.json()["data"], rows)
        self.assertEqual(listed.call_args.args, (account, "CONFIG_UPDATE", 7))

    def test_limit_clamped_and_invalid_account_rejected(self) -> None:
        from quantpilot_api import audit

        with bearer(role="ADMIN") as auth_headers:
            with patch.object(audit, "list_audit_logs", return_value=[]) as listed:
                self.client.get("/api/v1/audit-logs?limit=9999", headers=auth_headers)
                self.assertEqual(listed.call_args.args[2], 500)
                self.client.get("/api/v1/audit-logs?limit=0", headers=auth_headers)
                self.assertEqual(listed.call_args.args[2], 1)
            resp = self.client.get(
                "/api/v1/audit-logs?account_id=not-a-uuid", headers=auth_headers
            )
        self.assertEqual(resp.status_code, 400)


class AuditWiringTests(unittest.TestCase):
    """State-changing routes must record one audit row after success."""

    def setUp(self) -> None:
        force_api_key(self)
        import quantpilot_api.app as app_module

        self.app = app_module
        self.client = TestClient(app_module.app)

    def test_config_put_records_audit(self) -> None:
        account = uuid4()
        with (
            bearer(role="ADMIN") as auth_headers,
            patch.object(self.app, "_account_or_404", return_value=account),
            patch.object(self.app.db, "conn"),
            patch.object(self.app, "_active_config_row", return_value=None),
            patch.object(self.app.audit, "record") as record,
        ):
            resp = self.client.put(
                f"/api/v1/accounts/{account}/config", json={}, headers=auth_headers
            )
        self.assertEqual(resp.status_code, 200, resp.text)
        record.assert_called_once()
        args = record.call_args.args
        self.assertEqual(args[0], account)
        self.assertEqual(args[1], "CONFIG_UPDATE")
        self.assertEqual(args[2], "account_config")

    def test_instance_status_records_audit(self) -> None:
        account = uuid4()
        instance = uuid4()
        row = {"instance_id": str(instance), "status": "ENABLED"}
        with (
            bearer(role="ADMIN") as auth_headers,
            patch.object(self.app, "_account_or_404", return_value=account),
            patch.object(self.app, "_instance_or_404", return_value=row),
            patch.object(self.app.db, "conn"),
            patch.object(self.app.audit, "record") as record,
        ):
            resp = self.client.post(
                f"/api/v1/accounts/{account}/strategy-instances/{instance}/status",
                json={"status": "ENABLED"},
                headers=auth_headers,
            )
        self.assertEqual(resp.status_code, 200, resp.text)
        record.assert_called_once()
        args = record.call_args.args
        self.assertEqual(args[1], "INSTANCE_STATUS_UPDATE")
        self.assertEqual(args[2], "strategy_instance")
        self.assertEqual(args[3], str(instance))
        self.assertEqual(args[4], {"status": "ENABLED"})

    def test_backtest_create_records_audit(self) -> None:
        from quantpilot_api import backtests

        account, instance, job = uuid4(), uuid4(), uuid4()
        stored = {
            "job_id": str(job),
            "account_id": str(account),
            "status": "COMPLETED",
            "strategy_instance_id": str(instance),
        }
        body = {
            "strategy_instance_id": str(instance),
            "range_start": "2026-01-05T00:00:00+00:00",
            "range_end": "2026-01-05T02:00:00+00:00",
            "initial_balance": "10000",
            "volume": "1",
        }
        with (
            bearer(role="ADMIN") as auth_headers,
            patch.object(self.app.db, "query_one",
                         return_value={"account_id": str(account)}),
            patch.object(backtests, "create_backtest", return_value=stored),
            patch.object(self.app.audit, "record") as record,
        ):
            resp = self.client.post(
                "/api/v1/backtests",
                json=body,
                headers={"Idempotency-Key": str(uuid4()), **auth_headers},
            )
        self.assertEqual(resp.status_code, 200, resp.text)
        record.assert_called_once()
        args = record.call_args.args
        self.assertEqual(args[0], account)
        self.assertEqual(args[1], "BACKTEST_CREATE")
        self.assertEqual(args[2], "backtest")
        self.assertEqual(args[3], str(job))

    def test_ai_analyze_records_audit(self) -> None:
        from quantpilot_api import ai_engine

        account = uuid4()
        analysis = {"analysis_id": "a1", "market_regime": "RANGE"}
        with (
            bearer(role="ADMIN") as auth_headers,
            patch.object(self.app, "_account_or_404", return_value=account),
            patch.object(ai_engine, "analyze_on_demand", return_value=("ADVISORY", analysis, None)),
            patch.object(self.app.audit, "record") as record,
        ):
            resp = self.client.post(
                f"/api/v1/accounts/{account}/ai/analyze",
                json={"broker_symbol": "EURUSD", "timeframe": "M15"},
                headers=auth_headers,
            )
        self.assertEqual(resp.status_code, 200, resp.text)
        record.assert_called_once()
        args, kwargs = record.call_args.args, record.call_args.kwargs
        self.assertEqual(args[0], account)
        self.assertEqual(args[1], "AI_ANALYZE")
        payload = kwargs.get("payload") or args[4]
        self.assertEqual(payload["broker_symbol"], "EURUSD")
        self.assertEqual(payload["timeframe"], "M15")
        self.assertEqual(payload["ai_mode"], "ADVISORY")

    def test_command_create_records_audit(self) -> None:
        from quantpilot_api import audit, command_service
        from quantpilot_contracts.models import TradeCommandCreate

        account, command_id = uuid4(), uuid4()
        body = TradeCommandCreate.model_validate(
            {
                "command_id": str(command_id),
                "account_id": str(account),
                "command_type": "RECONCILE",
                "expires_at": (datetime.now(timezone.utc) + timedelta(seconds=60)).isoformat(),
                "created_by": "audit-test",
                "payload": {},
            }
        )
        row = {
            "command_id": str(command_id),
            "account_id": str(account),
            "command_type": "RECONCILE",
            "status": "QUEUED",
        }
        with (
            patch.object(command_service, "_account_or_error", return_value=account),
            patch.object(command_service, "command_by_id", side_effect=[None, row]),
            patch.object(command_service.db, "query_one", return_value=None),
            patch.object(command_service.db, "conn"),
            patch.object(command_service.writers, "add_system_event"),
            patch.object(audit, "record") as record,
        ):
            payload = command_service.create_command(body)
        self.assertTrue(payload["created"])
        record.assert_called_once()
        args = record.call_args.args
        self.assertEqual(args[0], account)
        self.assertEqual(args[1], "COMMAND_CREATE")
        self.assertEqual(args[2], "command")
        self.assertEqual(args[3], str(command_id))


if __name__ == "__main__":
    unittest.main()
