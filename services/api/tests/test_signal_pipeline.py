import unittest
from datetime import datetime, timedelta, timezone
from decimal import Decimal

from quantpilot_api.account_config import AccountConfig


SOURCE_BAR = datetime(2026, 10, 31, 12, 0, tzinfo=timezone.utc)


def signal(signal_id: str, direction: str = "BUY", strength: float = 0.5) -> dict:
    return {
        "signal_id": signal_id,
        "strategy_instance_id": "11111111-1111-1111-1111-111111111111",
        "source_bar_time": SOURCE_BAR,
        "signal_json": {
            "signal_id": signal_id,
            "symbol": "EURUSD",
            "timeframe": "M15",
            "direction": direction,
            "strength": strength,
            "proposed_stop_loss": "1.07800000",
            "proposed_take_profit": "1.09000000",
            "valid_until": (SOURCE_BAR + timedelta(minutes=15)).isoformat(),
        },
    }


class SignalPipelineTests(unittest.TestCase):
    def test_automation_is_disabled_by_default_and_deserializes(self) -> None:
        self.assertFalse(AccountConfig().automation.command_generation)
        configured = AccountConfig.model_validate(
            {"automation": {"command_generation": True}}
        )
        self.assertTrue(configured.automation.command_generation)

    def test_same_direction_signals_select_strongest_primary(self) -> None:
        from quantpilot_api.signal_pipeline import aggregate_signals

        weak = signal("00000000-0000-0000-0000-000000000002", strength=0.6)
        strong = signal("00000000-0000-0000-0000-000000000003", strength=0.9)
        result = aggregate_signals([weak, strong])

        self.assertEqual(result["conflict_signal_ids"], [])
        self.assertEqual(len(result["groups"]), 1)
        group = result["groups"][0]
        self.assertEqual(group["direction"], "BUY")
        self.assertEqual(group["primary"]["signal_id"], strong["signal_id"])
        self.assertEqual(group["signal_ids"], sorted([weak["signal_id"], strong["signal_id"]]))

    def test_equal_strength_uses_signal_id_as_deterministic_tie_breaker(self) -> None:
        from quantpilot_api.signal_pipeline import aggregate_signals

        first = signal("00000000-0000-0000-0000-000000000001", strength=0.8)
        second = signal("00000000-0000-0000-0000-000000000002", strength=0.8)
        result = aggregate_signals([second, first])

        self.assertEqual(result["groups"][0]["primary"]["signal_id"], first["signal_id"])

    def test_opposite_directions_conflict_and_emit_no_group(self) -> None:
        from quantpilot_api.signal_pipeline import aggregate_signals

        buy = signal("00000000-0000-0000-0000-000000000001", "BUY")
        sell = signal("00000000-0000-0000-0000-000000000002", "SELL")
        result = aggregate_signals([buy, sell])

        self.assertEqual(result["groups"], [])
        self.assertEqual(result["conflict_signal_ids"], sorted([buy["signal_id"], sell["signal_id"]]))

    def test_group_command_id_is_deterministic_and_direction_scoped(self) -> None:
        from quantpilot_api.signal_pipeline import group_command_id

        account_id = "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa"
        buy = group_command_id(account_id, "EURUSD", "M15", SOURCE_BAR, "BUY")
        replay = group_command_id(account_id, "EURUSD", "M15", SOURCE_BAR, "BUY")
        sell = group_command_id(account_id, "EURUSD", "M15", SOURCE_BAR, "SELL")

        self.assertEqual(buy, replay)
        self.assertNotEqual(buy, sell)

    def test_processing_is_a_noop_when_automation_is_off(self) -> None:
        from quantpilot_api.signal_pipeline import process_signals

        result = process_signals(
            [signal("00000000-0000-0000-0000-000000000001")],
            account_id="aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa",
            broker_symbol="EURUSD",
            timeframe="M15",
            source_bar_time=SOURCE_BAR,
            config=AccountConfig(),
            config_version=0,
        )

        self.assertEqual(result["command_ids"], [])
        self.assertEqual(result["conflict_signal_ids"], [])

    def test_processing_conflict_never_builds_a_command(self) -> None:
        from quantpilot_api.signal_pipeline import process_signals

        config = AccountConfig.model_validate({"automation": {"command_generation": True}})
        result = process_signals(
            [
                signal("00000000-0000-0000-0000-000000000001", "BUY"),
                signal("00000000-0000-0000-0000-000000000002", "SELL"),
            ],
            account_id="aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa",
            broker_symbol="EURUSD",
            timeframe="M15",
            source_bar_time=SOURCE_BAR,
            config=config,
            config_version=1,
        )

        self.assertEqual(result["command_ids"], [])
        self.assertEqual(len(result["conflict_signal_ids"]), 2)

    def test_command_builder_uses_sizing_signal_levels_and_expiry_cap(self) -> None:
        from quantpilot_api.signal_pipeline import aggregate_signals, build_trade_command

        group = aggregate_signals(
            [signal("00000000-0000-0000-0000-000000000001")]
        )["groups"][0]
        config = AccountConfig.model_validate(
            {
                "expert": {"magic_number": 9001001},
                "risk": {"per_trade_risk_fraction": "0.001"},
                "automation": {"command_generation": True},
            }
        )
        command = build_trade_command(
            group,
            account_id="aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa",
            broker_symbol="EURUSD",
            timeframe="M15",
            source_bar_time=SOURCE_BAR,
            config=config,
            snapshot={"equity": Decimal("9980"), "balance": Decimal("10000")},
            quote={"bid": Decimal("1.08123000"), "ask": Decimal("1.08135000")},
            spec={
                "tick_size": "0.00001000",
                "tick_value_loss": "1.00000000",
                "volume_min": "0.01000000",
                "volume_max": "100.00000000",
                "volume_step": "0.01000000",
            },
            now=SOURCE_BAR,
        )
        doc = command.model_dump(mode="json")

        self.assertEqual(command.command_type, "OPEN_MARKET")
        self.assertEqual(command.created_by, "signal-aggregator")
        self.assertEqual(doc["payload"]["volume"], "0.02")
        self.assertEqual(doc["payload"]["stop_loss"], "1.07800000")
        self.assertEqual(doc["payload"]["take_profit"], "1.09000000")
        self.assertEqual(doc["payload"]["magic_number"], 9001001)
        self.assertEqual(command.expires_at, SOURCE_BAR + timedelta(seconds=600))

    def test_risk_context_preserves_advisory_ai_without_changing_decision(self) -> None:
        from quantpilot_api.signal_pipeline import aggregate_signals, risk_context

        group = aggregate_signals(
            [signal("00000000-0000-0000-0000-000000000001")]
        )["groups"][0]
        analysis = {
            "analysis_id": "analysis-1",
            "model_provider": "statistical-regime",
            "model_id": "regime-rules-v1",
            "market_regime": "HIGH_VOL",
            "regime_confidence": 0.68,
            "signal_decision": "REVIEW",
            "risk_flags": ["VOLATILITY_ELEVATED"],
            "source_bar_time": SOURCE_BAR.isoformat(),
            "expires_at": (SOURCE_BAR + timedelta(minutes=30)).isoformat(),
        }

        context = risk_context(group, analysis)

        self.assertEqual(context["signal_ids"], group["signal_ids"])
        self.assertEqual(context["ai"]["analysis_id"], "analysis-1")
        self.assertEqual(context["ai"]["signal_decision"], "REVIEW")
        self.assertEqual(context["ai"]["risk_flags"], ["VOLATILITY_ELEVATED"])

    def test_processing_builds_and_persists_group_command(self) -> None:
        from quantpilot_api.signal_pipeline import process_signals

        config = AccountConfig.model_validate(
            {
                "risk": {"per_trade_risk_fraction": "0.001"},
                "automation": {"command_generation": True},
            }
        )
        captured = {}

        def create(command, **kwargs):
            captured["command"] = command
            captured.update(kwargs)
            return {"command_id": str(command.command_id)}

        result = process_signals(
            [signal("00000000-0000-0000-0000-000000000001")],
            account_id="aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa",
            broker_symbol="EURUSD",
            timeframe="M15",
            source_bar_time=SOURCE_BAR,
            config=config,
            config_version=3,
            snapshot={"equity": Decimal("9980"), "balance": Decimal("10000")},
            quote={"bid": Decimal("1.08123000"), "ask": Decimal("1.08135000")},
            spec={
                "tick_size": "0.00001000",
                "tick_value_loss": "1.00000000",
                "volume_min": "0.01000000",
                "volume_max": "100.00000000",
                "volume_step": "0.01000000",
            },
            ai_analysis={"analysis_id": "analysis-1", "signal_decision": "REVIEW"},
            now=SOURCE_BAR,
            command_creator=create,
        )

        self.assertEqual(result["command_ids"], [str(captured["command"].command_id)])
        self.assertEqual(result["failures"], [])
        self.assertEqual(captured["config_version"], 3)
        self.assertEqual(captured["context"]["ai"]["signal_decision"], "REVIEW")

    def test_processing_reports_sizing_failure_without_creating_a_command(self) -> None:
        from quantpilot_api.signal_pipeline import process_signals

        config = AccountConfig.model_validate(
            {"automation": {"command_generation": True}}
        )

        def unexpected_create(command, **kwargs):
            self.fail("zero risk configuration must not create a command")

        result = process_signals(
            [signal("00000000-0000-0000-0000-000000000001")],
            account_id="aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa",
            broker_symbol="EURUSD",
            timeframe="M15",
            source_bar_time=SOURCE_BAR,
            config=config,
            config_version=1,
            snapshot={"equity": Decimal("9980"), "balance": Decimal("10000")},
            quote={"bid": Decimal("1.08123000"), "ask": Decimal("1.08135000")},
            spec={
                "tick_size": "0.00001000",
                "tick_value_loss": "1.00000000",
                "volume_min": "0.01000000",
                "volume_max": "100.00000000",
                "volume_step": "0.01000000",
            },
            now=SOURCE_BAR,
            command_creator=unexpected_create,
        )

        self.assertEqual(result["command_ids"], [])
        self.assertEqual(result["failures"][0]["reason"], "RISK_NOT_CONFIGURED")

    def test_risk_rejection_preserves_advisory_context_without_using_it(self) -> None:
        from unittest.mock import patch
        from uuid import UUID

        from quantpilot_api import risk

        context = {
            "signal_ids": ["00000000-0000-0000-0000-000000000001"],
            "ai": {"analysis_id": "analysis-1", "signal_decision": "REVIEW"},
        }
        stopped = {
            "GLOBAL": "STOP_NEW_RISK",
            "ACCOUNT": "NORMAL",
            "global_reason": "test stop",
            "account_reason": None,
        }
        with patch.object(risk, "_stop_states", return_value=stopped):
            with_ai = risk.evaluate_command(
                UUID("aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa"),
                "OPEN_MARKET",
                {"symbol": "EURUSD", "side": "BUY", "volume": "0.02"},
                AccountConfig(),
                4,
                context=context,
            )
            without_ai = risk.evaluate_command(
                UUID("aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa"),
                "OPEN_MARKET",
                {"symbol": "EURUSD", "side": "BUY", "volume": "0.02"},
                AccountConfig(),
                4,
            )

        self.assertEqual(with_ai.decision, without_ai.decision)
        self.assertEqual(with_ai.reason_codes, without_ai.reason_codes)
        self.assertEqual(with_ai.snapshot["signal_ids"], context["signal_ids"])
        self.assertEqual(with_ai.snapshot["ai"]["signal_decision"], "REVIEW")

    def test_position_size_uses_risk_budget_and_floors_to_step(self) -> None:
        from quantpilot_api.risk import calculate_volume

        volume = calculate_volume(
            basis=Decimal("9980"),
            risk_fraction=Decimal("0.001"),
            entry=Decimal("1.08135000"),
            stop_loss=Decimal("1.07800000"),
            tick_size=Decimal("0.00001000"),
            tick_value_loss=Decimal("1.00000000"),
            volume_min=Decimal("0.01000000"),
            volume_max=Decimal("100.00000000"),
            volume_step=Decimal("0.01000000"),
        )

        # budget=9.98, loss/lot=335, raw=0.02979..., floor(0.01)=0.02
        self.assertEqual(volume, Decimal("0.02"))
        self.assertIsInstance(volume, Decimal)

    def test_position_size_below_symbol_minimum_is_rejected(self) -> None:
        from quantpilot_api.risk import SizingError, calculate_volume

        with self.assertRaisesRegex(SizingError, "VOLUME_BELOW_MIN"):
            calculate_volume(
                basis=Decimal("9980"),
                risk_fraction=Decimal("0.00001"),
                entry=Decimal("1.08135000"),
                stop_loss=Decimal("1.07800000"),
                tick_size=Decimal("0.00001000"),
                tick_value_loss=Decimal("1.00000000"),
                volume_min=Decimal("0.01000000"),
                volume_max=Decimal("100.00000000"),
                volume_step=Decimal("0.01000000"),
            )

    def test_position_size_requires_explicit_nonzero_risk(self) -> None:
        from quantpilot_api.risk import SizingError, calculate_volume

        with self.assertRaisesRegex(SizingError, "RISK_NOT_CONFIGURED"):
            calculate_volume(
                basis=Decimal("9980"),
                risk_fraction=Decimal("0"),
                entry=Decimal("1.08135000"),
                stop_loss=Decimal("1.07800000"),
                tick_size=Decimal("0.00001000"),
                tick_value_loss=Decimal("1.00000000"),
                volume_min=Decimal("0.01000000"),
                volume_max=Decimal("100.00000000"),
                volume_step=Decimal("0.01000000"),
            )


class BarPipelineTests(unittest.TestCase):
    def test_newest_bar_is_a_noop_before_market_queries_when_automation_is_off(self) -> None:
        from unittest.mock import patch

        from quantpilot_api import signal_pipeline

        with (
            patch.object(
                signal_pipeline.command_service,
                "effective_config",
                return_value=(AccountConfig(), 0),
            ),
            patch.object(signal_pipeline.db, "query") as query,
        ):
            result = signal_pipeline.process_newest_bar(
                "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa", "EURUSD", "M15", SOURCE_BAR
            )

        self.assertEqual(result["command_ids"], [])
        query.assert_not_called()

    def test_newest_bar_loads_one_bar_context_and_uses_shared_command_creator(self) -> None:
        from unittest.mock import patch

        from quantpilot_api import signal_pipeline

        config = AccountConfig.model_validate(
            {
                "risk": {"per_trade_risk_fraction": "0.001"},
                "automation": {"command_generation": True},
            }
        )
        rows = {
            "snapshot": {"equity": Decimal("9980"), "balance": Decimal("10000")},
            "quote": {"bid": Decimal("1.08123000"), "ask": Decimal("1.08135000")},
            "spec": {
                "spec_json": {
                    "tick_size": "0.00001000",
                    "tick_value_loss": "1.00000000",
                    "volume_min": "0.01000000",
                    "volume_max": "100.00000000",
                    "volume_step": "0.01000000",
                }
            },
        }
        captured = {}

        def create(command, **kwargs):
            captured["command"] = command
            captured.update(kwargs)
            return {"command_id": str(command.command_id)}

        with (
            patch.object(
                signal_pipeline.command_service,
                "effective_config",
                return_value=(config, 5),
            ),
            patch.object(
                signal_pipeline.db,
                "query",
                return_value=[signal("00000000-0000-0000-0000-000000000001")],
            ),
            patch.object(
                signal_pipeline.db,
                "query_one",
                side_effect=[rows["snapshot"], rows["quote"], rows["spec"]],
            ),
        ):
            result = signal_pipeline.process_newest_bar(
                "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa",
                "EURUSD",
                "M15",
                SOURCE_BAR,
                ai_analysis={"analysis_id": "analysis-1", "signal_decision": "REVIEW"},
                now=SOURCE_BAR,
                command_creator=create,
            )

        self.assertEqual(result["command_ids"], [str(captured["command"].command_id)])
        self.assertEqual(captured["config_version"], 5)
        self.assertEqual(captured["context"]["ai"]["analysis_id"], "analysis-1")
        self.assertEqual(captured["command"].payload["volume"], "0.02")

    def test_newest_bar_audits_signal_conflict_without_creating_a_command(self) -> None:
        from unittest.mock import patch

        from quantpilot_api import signal_pipeline

        config = AccountConfig.model_validate(
            {"automation": {"command_generation": True}}
        )
        signals = [
            signal("00000000-0000-0000-0000-000000000001", "BUY"),
            signal("00000000-0000-0000-0000-000000000002", "SELL"),
        ]
        with (
            patch.object(
                signal_pipeline.command_service,
                "effective_config",
                return_value=(config, 2),
            ),
            patch.object(signal_pipeline.db, "query", return_value=signals),
            patch.object(signal_pipeline.db, "query_one", return_value=None),
            patch.object(signal_pipeline.writers, "add_system_event") as emit,
        ):
            result = signal_pipeline.process_newest_bar(
                "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa",
                "EURUSD",
                "M15",
                SOURCE_BAR,
                command_creator=lambda *args, **kwargs: self.fail("conflict created a command"),
            )

        self.assertEqual(result["command_ids"], [])
        self.assertEqual(len(result["conflict_signal_ids"]), 2)
        event = next(call.kwargs for call in emit.call_args_list
                     if call.kwargs["event_type"] == "signal.conflict")
        self.assertEqual(event["severity"], "WARN")
        self.assertEqual(event["payload"]["signal_ids"], result["conflict_signal_ids"])

    def test_newest_bar_audits_sizing_failure(self) -> None:
        from unittest.mock import patch

        from quantpilot_api import signal_pipeline

        config = AccountConfig.model_validate(
            {"automation": {"command_generation": True}}
        )
        snapshot = {"equity": Decimal("9980"), "balance": Decimal("10000")}
        quote = {"bid": Decimal("1.08123000"), "ask": Decimal("1.08135000")}
        spec = {"spec_json": {
            "tick_size": "0.00001000", "tick_value_loss": "1.00000000",
            "volume_min": "0.01000000", "volume_max": "100.00000000",
            "volume_step": "0.01000000",
        }}
        with (
            patch.object(
                signal_pipeline.command_service,
                "effective_config",
                return_value=(config, 2),
            ),
            patch.object(
                signal_pipeline.db,
                "query",
                return_value=[signal("00000000-0000-0000-0000-000000000001")],
            ),
            patch.object(signal_pipeline.db, "query_one", side_effect=[snapshot, quote, spec]),
            patch.object(signal_pipeline.writers, "add_system_event") as emit,
        ):
            result = signal_pipeline.process_newest_bar(
                "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa",
                "EURUSD",
                "M15",
                SOURCE_BAR,
                command_creator=lambda *args, **kwargs: self.fail("zero risk created a command"),
            )

        self.assertEqual(result["failures"][0]["reason"], "RISK_NOT_CONFIGURED")
        event = next(call.kwargs for call in emit.call_args_list
                     if call.kwargs["event_type"] == "signal.command_failed")
        self.assertEqual(event["payload"]["reason"], "RISK_NOT_CONFIGURED")


class WritersPipelineTests(unittest.TestCase):
    @staticmethod
    def event() -> dict:
        return {
            "broker_symbol": "EURUSD",
            "timeframe": "M15",
            "bars": [{
                "bar_time": SOURCE_BAR.isoformat(),
                "open": "1.081", "high": "1.082", "low": "1.080",
                "close": "1.0815", "tick_volume": 10,
            }],
        }

    def test_handle_bars_passes_advisory_analysis_to_signal_pipeline(self) -> None:
        from unittest.mock import MagicMock, patch

        from quantpilot_api import ai_engine, signal_pipeline, strategy_engine, writers

        analysis = {"analysis_id": "analysis-1", "signal_decision": "REVIEW"}
        with (
            patch.object(writers, "_resolve", return_value=("terminal-1", "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa")),
            patch.object(writers.db, "conn", return_value=MagicMock()),
            patch.object(strategy_engine, "evaluate_newest_bar"),
            patch.object(ai_engine, "analyze_newest_bar", return_value=analysis),
            patch.object(signal_pipeline, "process_newest_bar") as process,
        ):
            writers.handle_bars(self.event())

        process.assert_called_once_with(
            "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa",
            "EURUSD",
            "M15",
            SOURCE_BAR,
            ai_analysis=analysis,
        )

    def test_handle_bars_isolates_and_audits_pipeline_failure(self) -> None:
        from unittest.mock import MagicMock, patch

        from quantpilot_api import ai_engine, signal_pipeline, strategy_engine, writers

        with (
            patch.object(writers, "_resolve", return_value=("terminal-1", "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa")),
            patch.object(writers.db, "conn", return_value=MagicMock()),
            patch.object(strategy_engine, "evaluate_newest_bar"),
            patch.object(ai_engine, "analyze_newest_bar", return_value=None),
            patch.object(signal_pipeline, "process_newest_bar", side_effect=RuntimeError("db lost")),
            patch.object(writers, "add_system_event") as emit,
        ):
            writers.handle_bars(self.event())

        event = next(call.kwargs for call in emit.call_args_list
                     if call.kwargs["event_type"] == "signal.pipeline_failed")
        self.assertEqual(event["severity"], "WARN")
        self.assertIn("db lost", event["payload"]["error"])


class CommandServiceTests(unittest.TestCase):
    def test_create_command_shares_risk_gate_and_persists_rejection(self) -> None:
        from unittest.mock import MagicMock, patch

        from quantpilot_contracts.models import TradeCommandCreate
        from quantpilot_api import command_service, risk

        account_id = "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa"
        command_id = "00000000-0000-0000-0000-000000000009"
        body = TradeCommandCreate(
            command_id=command_id,
            idempotency_key=command_id,
            account_id=account_id,
            command_type="OPEN_MARKET",
            expires_at=SOURCE_BAR + timedelta(seconds=600),
            signal_id="00000000-0000-0000-0000-000000000001",
            created_by="signal-aggregator",
            payload={
                "symbol": "EURUSD",
                "side": "BUY",
                "volume": "0.02",
                "stop_loss": "1.078",
            },
        )
        config = AccountConfig.model_validate(
            {
                "risk": {"per_trade_risk_fraction": "0.001"},
                "automation": {"command_generation": True},
            }
        )
        context = {"ai": {"analysis_id": "analysis-1", "signal_decision": "REVIEW"}}
        decision = risk.RiskDecision(
            "REJECT", ["GLOBAL_STOP_NEW_RISK"], {"config_version": 7}
        )
        stored = {
            "command_id": command_id,
            "account_id": account_id,
            "command_type": "OPEN_MARKET",
            "status": "REJECTED",
            "payload": body.payload,
            "expires_at": body.expires_at,
        }
        connection = MagicMock()
        connection.execute.return_value.fetchone.return_value = {"command_id": command_id}
        connection_context = MagicMock()
        connection_context.__enter__.return_value = connection
        query_results = iter([{"id": account_id}, None, None, stored])

        with (
            patch.object(command_service.db, "query_one", side_effect=lambda *args: next(query_results)),
            patch.object(command_service.db, "conn", return_value=connection_context),
            patch.object(command_service.risk, "evaluate_command", return_value=decision) as evaluate,
            patch.object(command_service.risk, "record_decision") as record,
            patch.object(command_service.writers, "add_system_event"),
            patch.object(command_service.audit, "record"),
        ):
            result = command_service.create_command(
                body,
                config=config,
                config_version=7,
                context=context,
                now=SOURCE_BAR,
            )

        self.assertTrue(result["created"])
        self.assertEqual(result["status"], "REJECTED")
        self.assertEqual(result["risk_decision"]["decision"], "REJECT")
        self.assertEqual(evaluate.call_args.kwargs["context"], context)
        self.assertEqual(evaluate.call_args.kwargs["config_version"], 7)
        record.assert_called_once()
        insert_params = connection.execute.call_args.args[1]
        self.assertEqual(insert_params[5], "REJECTED")


if __name__ == "__main__":
    unittest.main()
