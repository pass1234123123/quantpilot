import unittest
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from unittest.mock import patch
from uuid import UUID, uuid4

from fastapi.testclient import TestClient
from pydantic import ValidationError


NOW = datetime(2026, 1, 5, tzinfo=timezone.utc)


def body_dict(instance_id: UUID) -> dict:
    return {
        "strategy_instance_id": str(instance_id),
        "range_start": NOW.isoformat(),
        "range_end": (NOW + timedelta(hours=2)).isoformat(),
        "initial_balance": "10000",
        "volume": "1",
        "cost_model": {
            "spread_per_side": "0.0001",
            "slippage_per_side": "0.0002",
            "commission_per_lot_per_side": "3.5",
            "swap_per_lot_per_bar": "0",
            "contract_size": "100000",
            "quote_to_account_rate": "1",
        },
    }


class BacktestContractTests(unittest.TestCase):
    def test_request_rejects_naive_or_reversed_range(self) -> None:
        from quantpilot_api.backtests import BacktestCreate

        instance_id = uuid4()
        naive = body_dict(instance_id)
        naive["range_start"] = "2026-01-05T00:00:00"
        with self.assertRaises(ValidationError):
            BacktestCreate.model_validate(naive)
        reversed_ = body_dict(instance_id)
        reversed_["range_end"] = reversed_["range_start"]
        with self.assertRaises(ValidationError):
            BacktestCreate.model_validate(reversed_)

    def test_job_id_is_deterministic_for_idempotency_key(self) -> None:
        from quantpilot_api.backtests import deterministic_backtest_id

        key = uuid4()
        self.assertEqual(deterministic_backtest_id(key), deterministic_backtest_id(key))
        self.assertNotEqual(deterministic_backtest_id(key), deterministic_backtest_id(uuid4()))

    def test_idempotent_replay_returns_original_without_loading_or_running(self) -> None:
        from quantpilot_api import backtests

        instance_id, key = uuid4(), uuid4()
        body = backtests.BacktestCreate.model_validate(body_dict(instance_id))
        stored = {
            "job_id": str(backtests.deterministic_backtest_id(key)),
            "status": "COMPLETED",
            "request": body.model_dump(mode="json"),
        }
        with (
            patch.object(backtests, "get_backtest", return_value=stored) as get,
            patch.object(backtests, "_instance_or_error") as load,
            patch.object(backtests, "run_backtest") as run,
        ):
            result = backtests.create_backtest(body, key)

        self.assertEqual(result, stored)
        get.assert_called_once_with(backtests.deterministic_backtest_id(key))
        load.assert_not_called()
        run.assert_not_called()

    def test_idempotency_key_reuse_with_different_request_conflicts(self) -> None:
        from quantpilot_api import backtests

        instance_id, key = uuid4(), uuid4()
        body = backtests.BacktestCreate.model_validate(body_dict(instance_id))
        stored = {
            "job_id": str(backtests.deterministic_backtest_id(key)),
            "status": "COMPLETED",
            "request": body.model_dump(mode="json"),
        }
        changed = backtests.BacktestCreate.model_validate({
            **body_dict(instance_id), "volume": "2",
        })
        with patch.object(backtests, "get_backtest", return_value=stored):
            with self.assertRaises(backtests.BacktestConflict):
                backtests.create_backtest(changed, key)


class BacktestRouteTests(unittest.TestCase):
    def setUp(self) -> None:
        self.client = TestClient(__import__("quantpilot_api.app", fromlist=["app"]).app)
        self.instance_id = uuid4()
        self.key = uuid4()

    def test_post_and_get_routes_use_standard_envelope(self) -> None:
        from quantpilot_api import backtests

        job_id = backtests.deterministic_backtest_id(self.key)
        stored = {"job_id": str(job_id), "status": "COMPLETED", "metrics": {}}
        with patch.object(backtests, "create_backtest", return_value=stored) as create:
            response = self.client.post(
                "/api/v1/backtests",
                json=body_dict(self.instance_id),
                headers={"Idempotency-Key": str(self.key)},
            )

        self.assertEqual(response.status_code, 200, response.text)
        self.assertIsNone(response.json()["error"])
        self.assertEqual(response.json()["data"], stored)
        create.assert_called_once()
        self.assertEqual(create.call_args.args[1], self.key)

        with patch.object(backtests, "get_backtest", return_value=stored):
            fetched = self.client.get(f"/api/v1/backtests/{job_id}")
        self.assertEqual(fetched.status_code, 200, fetched.text)
        self.assertEqual(fetched.json()["data"]["job_id"], str(job_id))

    def test_post_requires_idempotency_key(self) -> None:
        response = self.client.post("/api/v1/backtests", json=body_dict(self.instance_id))
        self.assertEqual(response.status_code, 422)

    def test_list_backtests_filters_by_account_and_wraps_envelope(self) -> None:
        from quantpilot_api import backtests

        rows = [{"job_id": str(uuid4()), "status": "COMPLETED", "strategy_code": "ma_cross"}]
        with patch.object(backtests, "list_backtests", return_value=rows) as listed:
            response = self.client.get(f"/api/v1/backtests?account_id={self.instance_id}&limit=5")

        self.assertEqual(response.status_code, 200, response.text)
        self.assertIsNone(response.json()["error"])
        self.assertEqual(response.json()["data"], rows)
        listed.assert_called_once()
        self.assertEqual(listed.call_args.args, (self.instance_id, None, 5))


if __name__ == "__main__":
    unittest.main()
