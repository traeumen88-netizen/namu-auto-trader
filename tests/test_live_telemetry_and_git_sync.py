"""
Unit and scenario tests for Live Telemetry Exporter and GitHub Syncer (Section 22).
Verifies:
1. test_telemetry_event_serialization
2. test_sensitive_data_redaction
3. test_account_number_masking
4. test_ack_not_marked_as_fill
5. test_partial_fill_telemetry
6. test_full_fill_telemetry
7. test_duplicate_event_id_dedup
8. test_latest_live_status_update
9. test_daily_summary_generation
10. test_git_sync_failure_does_not_block_trading
11. test_git_sync_lock
12. test_telemetry_batching
13. test_critical_event_fast_sync
14. test_recovery_after_git_push_failure
"""

import os
import json
import time
import shutil
import tempfile
import threading
from unittest.mock import MagicMock, patch

import pytest
from core.telemetry_sanitizer import TelemetrySanitizer
from execution.live_telemetry_exporter import LiveTelemetryExporter, CRITICAL_EVENTS
from execution.telemetry_git_syncer import TelemetryGitSyncer
from core.models import Order, OrderSide, OrderType, OrderStatus, TimeHorizon


@pytest.fixture
def temp_telemetry_dir():
    temp_dir = tempfile.mkdtemp(prefix="test_telemetry_")
    yield temp_dir
    shutil.rmtree(temp_dir, ignore_errors=True)


@pytest.fixture
def fresh_exporter(temp_telemetry_dir):
    LiveTelemetryExporter.reset_instance()
    exp = LiveTelemetryExporter(base_dir=temp_telemetry_dir)
    yield exp
    exp.stop()
    LiveTelemetryExporter.reset_instance()


class TestLiveTelemetryAndGitSync:

    def test_telemetry_event_serialization(self, fresh_exporter, temp_telemetry_dir):
        """1. test_telemetry_event_serialization: Check JSONL format, types, and schema."""
        exp = fresh_exporter
        payload = {
            "symbol": "005930",
            "name": "삼성전자",
            "strategy": "AI_MOMENTUM",
            "rvol": 2.45,
            "momentum_3m": 0.015,
            "int_val": 100,
            "flag": True,
            "tags": ["alpha", "beta"],
        }
        eid = exp.emit_event("BUY_SIGNAL", payload, date_str="20260918")
        assert eid is not None
        exp.flush()

        # Check that file was created and is valid JSON
        decision_file = os.path.join(temp_telemetry_dir, "20260918", "live_decision.jsonl")
        assert os.path.exists(decision_file), "live_decision.jsonl should be created"

        with open(decision_file, "r", encoding="utf-8") as f:
            lines = f.readlines()
        assert len(lines) >= 1
        data = json.loads(lines[-1])
        assert data["symbol"] == "005930"
        assert data["rvol"] == 2.45
        assert data["flag"] is True
        assert data["event"] == "BUY_SIGNAL"
        assert data["event_id"] == eid

    def test_sensitive_data_redaction(self):
        """2. test_sensitive_data_redaction: Check that tokens, passwords, keys, raw payloads are redacted."""
        raw_data = {
            "access_token": "secret_oauth_token_12345",
            "refresh_token": "refresh_token_67890",
            "api_key": "my_nh_api_key",
            "appsecret": "my_nh_app_secret",
            "password": "my_secret_password",
            "authorization": "Bearer " + "dummy_token_abc123",
            "header": "Authorization: Bearer dummy_bearer_token",
            "github_token": "ghp_" + "1234567890abcdefghijklmnopqrst",
            "fine_pat": "github_pat_" + "1234567890_abcdefghijklmnopqrstuvwxyz0123456789",
            "raw_response": {"huge": "payload", "dump": [1, 2, 3]},
            "normal_field": 42,
        }

        sanitized = TelemetrySanitizer.sanitize_data(raw_data)

        assert sanitized["access_token"] == "[REDACTED]"
        assert sanitized["refresh_token"] == "[REDACTED]"
        assert sanitized["api_key"] == "[REDACTED]"
        assert sanitized["appsecret"] == "[REDACTED]"
        assert sanitized["password"] == "[REDACTED]"
        assert sanitized["authorization"] == "[REDACTED]"
        assert "Bearer [REDACTED]" in sanitized["header"]
        assert "[REDACTED_GH_TOKEN]" in sanitized["github_token"]
        assert "[REDACTED_GH_PAT]" in sanitized["fine_pat"]
        assert sanitized["raw_response"] == "[OMITTED_FOR_TELEMETRY]"
        assert sanitized["normal_field"] == 42

    def test_account_number_masking(self):
        """3. test_account_number_masking: Check 20201549311 -> LIVE_****49311, 50001003032 -> MOCK_****03032."""
        live_acct = "2020" + "1549311"
        mock_acct = "5000" + "1003032"

        # LIVE account (11-digit)
        live_masked = TelemetrySanitizer.mask_account_number(live_acct)
        assert live_masked == "LIVE_****49311", f"Expected LIVE_****49311, got {live_masked}"

        # MOCK account (11-digit)
        mock_masked = TelemetrySanitizer.mask_account_number(mock_acct)
        assert mock_masked == "MOCK_****03032", f"Expected MOCK_****03032, got {mock_masked}"

        # Generic 8-digit
        generic_masked = TelemetrySanitizer.mask_account_number("12345678")
        assert generic_masked == "ACCT_****5678"

        # Embedded in dictionary
        d = {
            "account_no": live_acct,
            "act_no": int(mock_acct),
            "message": f"Order placed for account {live_acct} on broker",
        }
        sanitized = TelemetrySanitizer.sanitize_data(d)
        assert sanitized["account_no"] == "LIVE_****49311"
        assert sanitized["act_no"] == "MOCK_****03032"
        assert "LIVE_****" in sanitized["message"]

    def test_ack_not_marked_as_fill(self, fresh_exporter, temp_telemetry_dir):
        """4. test_ack_not_marked_as_fill: ORDER_ACK must not increment fills or set remaining_qty=0."""
        exp = fresh_exporter
        order = Order(
            client_order_id="ORD_027360_ACK_TEST",
            iem_cd="027360",
            strategy_id="AI_MOMENTUM",
            time_horizon=TimeHorizon.INTRADAY,
            side=OrderSide.SELL,
            order_type=OrderType.LIMIT,
            qty=2,
            price=15000,
            status=OrderStatus.ORDER_ACK,
            filled_qty=0,
            remaining_qty=2,
            broker_order_no="327455",
        )

        exp.record_order_event("ORDER_ACK", order, extra_info={"broker_order_state": "ACK"})
        exp.flush()

        # Check latest status
        status_path = os.path.join(temp_telemetry_dir, "latest_live_status.json")
        with open(status_path, "r", encoding="utf-8") as f:
            status = json.load(f)

        assert status["sell_fills"] == 0, "ORDER_ACK must NOT increment sell_fills!"
        assert status["buy_fills"] == 0
        assert status["last_event"]["event"] == "SELL_ORDER_ACK"

        # Verify live_orders.jsonl
        today_str = time.strftime("%Y%m%d")
        orders_file = os.path.join(temp_telemetry_dir, today_str, "live_orders.jsonl")
        assert os.path.exists(orders_file)
        with open(orders_file, "r", encoding="utf-8") as f:
            entry = json.loads(f.readlines()[-1])
        assert entry["broker_order_state"] in ("ACK", "ORDER_ACK")
        assert entry["filled_qty"] == 0
        assert entry["remaining_qty"] == 2

    def test_partial_fill_telemetry(self, fresh_exporter, temp_telemetry_dir):
        """5. test_partial_fill_telemetry: Partial fill records PARTIAL_FILL event with proper quantities."""
        exp = fresh_exporter
        order = Order(
            client_order_id="ORD_PARTIAL_TEST",
            iem_cd="005930",
            strategy_id="AI_MOMENTUM",
            time_horizon=TimeHorizon.INTRADAY,
            side=OrderSide.BUY,
            order_type=OrderType.LIMIT,
            qty=10,
            price=70000,
            status=OrderStatus.PARTIAL_FILL,
            filled_qty=4,
            remaining_qty=6,
            broker_order_no="B_PARTIAL_1",
        )

        exp.record_fill(order, fill_qty=4, fill_price=70000.0, is_full_fill=False)
        exp.flush()

        today_str = time.strftime("%Y%m%d")
        fills_file = os.path.join(temp_telemetry_dir, today_str, "live_fills.jsonl")
        assert os.path.exists(fills_file)
        with open(fills_file, "r", encoding="utf-8") as f:
            entry = json.loads(f.readlines()[-1])

        assert entry["event"] == "BUY_PARTIAL_FILL"
        assert entry["newly_filled_qty"] == 4
        assert entry["filled_qty"] == 4
        assert entry["remaining_qty"] == 6
        assert entry["is_full_fill"] is False

    def test_full_fill_telemetry(self, fresh_exporter, temp_telemetry_dir):
        """6. test_full_fill_telemetry: Full fill records BUY_FILLED and updates status."""
        exp = fresh_exporter
        order = Order(
            client_order_id="ORD_FULL_TEST",
            iem_cd="005930",
            strategy_id="AI_MOMENTUM",
            time_horizon=TimeHorizon.INTRADAY,
            side=OrderSide.BUY,
            order_type=OrderType.LIMIT,
            qty=10,
            price=70000,
            status=OrderStatus.FILLED,
            filled_qty=10,
            remaining_qty=0,
            broker_order_no="B_FULL_1",
        )

        exp.record_fill(order, fill_qty=6, fill_price=70000.0, is_full_fill=True)
        exp.flush()

        today_str = time.strftime("%Y%m%d")
        fills_file = os.path.join(temp_telemetry_dir, today_str, "live_fills.jsonl")
        with open(fills_file, "r", encoding="utf-8") as f:
            entry = json.loads(f.readlines()[-1])

        assert entry["event"] == "BUY_FILLED"
        assert entry["filled_qty"] == 10
        assert entry["remaining_qty"] == 0
        assert entry["is_full_fill"] is True

        # Check status update
        status_path = os.path.join(temp_telemetry_dir, "latest_live_status.json")
        with open(status_path, "r", encoding="utf-8") as f:
            status = json.load(f)
        assert status["buy_fills"] == 1

    def test_duplicate_event_id_dedup(self, fresh_exporter, temp_telemetry_dir):
        """7. test_duplicate_event_id_dedup: Identical event_id must not be written twice."""
        exp = fresh_exporter
        evt_id = "evt_20260918_unique_123456"
        payload = {"event_id": evt_id, "symbol": "027360", "data": "first_arrival"}

        # Emit twice
        exp.emit_event("ORDER_ACK", payload, date_str="20260918")
        exp.emit_event("ORDER_ACK", payload, date_str="20260918")
        exp.flush()

        orders_file = os.path.join(temp_telemetry_dir, "20260918", "live_orders.jsonl")
        with open(orders_file, "r", encoding="utf-8") as f:
            lines = f.readlines()

        matching = [l for l in lines if evt_id in l]
        assert len(matching) == 1, f"Duplicate event should be deduplicated to 1, found {len(matching)}"

    def test_latest_live_status_update(self, fresh_exporter, temp_telemetry_dir):
        """8. test_latest_live_status_update: Verifies metrics in latest_live_status.json."""
        exp = fresh_exporter

        exp.emit_event("BUY_SIGNAL", {"symbol": "005930"})
        exp.emit_event("BUY_APPROVED", {"symbol": "005930"})
        exp.emit_event("BUY_ORDER_SENT", {"symbol": "005930", "side": "BUY"})
        exp.emit_event("BUY_FILLED", {"symbol": "005930", "side": "BUY"})
        exp.emit_event("POSITION_OPEN", {"symbol": "005930"})
        exp.emit_event("PROFIT_OPPORTUNITY_REJECT", {"symbol": "035720", "gate": "PROFIT_GATE", "reason": "EXP_MOVE_LOW"})
        exp.emit_event("ENTRY_QUALITY_REJECT", {"symbol": "035420", "gate": "ENTRY_GATE", "reason": "RVOL_TOO_LOW"})
        exp.flush()

        status_path = os.path.join(temp_telemetry_dir, "latest_live_status.json")
        with open(status_path, "r", encoding="utf-8") as f:
            s = json.load(f)

        assert s["buy_signal_count"] == 1
        assert s["buy_approved"] == 1
        assert s["buy_orders_sent"] == 1
        assert s["buy_fills"] == 1
        assert s["open_positions"] == 1
        assert s["profit_opportunity_reject"] == 1
        assert s["entry_quality_reject"] == 1
        assert s["last_event"]["event"] == "ENTRY_QUALITY_REJECT"
        assert s["git_sync_status"] == "OK"

    def test_daily_summary_generation(self, fresh_exporter, temp_telemetry_dir):
        """9. test_daily_summary_generation: Verifies live_summary.json generation and sections."""
        exp = fresh_exporter
        exp.emit_event("BUY_APPROVED", {"symbol": "005930"})
        exp.emit_event("BUY_ORDER_SENT", {"symbol": "005930", "side": "BUY"})
        exp.emit_event("BUY_FILLED", {"symbol": "005930", "side": "BUY"})
        exp.emit_event("PROFIT_OPPORTUNITY_REJECT", {"symbol": "011200", "gate": "PROFIT", "reason": "COST_TOO_HIGH"})
        exp.flush()

        summary = exp.generate_daily_summary(date_str="20260918")

        summary_file = os.path.join(temp_telemetry_dir, "20260918", "live_summary.json")
        assert os.path.exists(summary_file)

        assert "entry_quality" in summary
        assert "profit_opportunity" in summary
        assert "reentry" in summary
        assert "micro_trade" in summary
        assert "counterfactual" in summary
        assert "completed_trades" in summary

        assert summary["entry_quality"]["buy_approved"] >= 1
        assert summary["profit_opportunity"]["reject"] >= 1
        assert "COST_TOO_HIGH" in summary["profit_opportunity"]["reject_reasons"]

    def test_git_sync_failure_does_not_block_trading(self, fresh_exporter, temp_telemetry_dir):
        """10. test_git_sync_failure_does_not_block_trading: Git failure never crashes trading engine."""
        exp = fresh_exporter
        syncer = TelemetryGitSyncer(
            repo_dir=".",
            telemetry_dir=temp_telemetry_dir,
            exporter=exp,
        )

        # Mock git push to fail
        with patch.object(syncer, "_run_git") as mock_git:
            # git add succeeds, git diff has files, git commit succeeds, git push fails
            def fake_git(args, timeout_sec=30):
                if args[0] == "add":
                    return 0, "", ""
                elif args[0] == "diff":
                    return 0, "data/live_telemetry/latest_live_status.json\n", ""
                elif args[0] == "commit":
                    return 0, "[main abcdef] telemetry", ""
                elif args[0] == "push":
                    return 1, "", "fatal: unable to access repository: Connection timed out"
                return 0, "", ""

            mock_git.side_effect = fake_git

            # Execute sync_now - MUST NOT RAISE
            res = syncer.sync_now(reason="test_failure")
            assert res is False
            assert syncer.git_sync_status == "DEGRADED"

        # Trading engine still functions normally
        eid = exp.emit_event("BUY_SIGNAL", {"symbol": "005930"})
        assert eid is not None
        exp.flush()

        # Status reflects degraded without crashing
        status_path = os.path.join(temp_telemetry_dir, "latest_live_status.json")
        with open(status_path, "r", encoding="utf-8") as f:
            s = json.load(f)
        assert s["git_sync_status"] == "DEGRADED"
        assert "Connection timed out" in s["last_push_result"]

    def test_git_sync_lock(self, temp_telemetry_dir):
        """11. test_git_sync_lock: Concurrent sync attempts are locked and mutually exclusive."""
        syncer1 = TelemetryGitSyncer(repo_dir=".", telemetry_dir=temp_telemetry_dir)
        syncer2 = TelemetryGitSyncer(repo_dir=".", telemetry_dir=temp_telemetry_dir)

        assert syncer1._acquire_file_lock() is True, "First lock should succeed"
        assert syncer2._acquire_file_lock() is False, "Second lock should fail while first is held"

        syncer1._release_file_lock()
        assert syncer2._acquire_file_lock() is True, "Lock should succeed after release"
        syncer2._release_file_lock()

    def test_telemetry_batching(self, fresh_exporter, temp_telemetry_dir):
        """12. test_telemetry_batching: Regular events are queued and batched together."""
        exp = fresh_exporter
        mock_syncer = MagicMock()
        exp.set_git_syncer(mock_syncer)

        # Emit 5 regular events
        for i in range(5):
            exp.emit_event("BUY_SIGNAL", {"symbol": f"00593{i}"}, is_critical=False)

        # Critical sync should NOT have been triggered
        mock_syncer.trigger_critical_sync.assert_not_called()

        exp.flush()
        assert exp.queue.empty()

    def test_critical_event_fast_sync(self, fresh_exporter):
        """13. test_critical_event_fast_sync: Critical events trigger fast sync."""
        exp = fresh_exporter
        mock_syncer = MagicMock()
        exp.set_git_syncer(mock_syncer)

        # BUY_FILLED is critical
        exp.emit_event("BUY_FILLED", {"symbol": "027360", "qty": 2})
        exp.flush()

        mock_syncer.trigger_critical_sync.assert_called_with("BUY_FILLED")

        # RECONCILIATION_MISMATCH is critical
        mock_syncer.reset_mock()
        exp.record_error("RECONCILIATION_MISMATCH", "Broker qty differs from local")
        exp.flush()
        mock_syncer.trigger_critical_sync.assert_called_with("RECONCILIATION_MISMATCH")

    def test_recovery_after_git_push_failure(self, fresh_exporter, temp_telemetry_dir):
        """14. test_recovery_after_git_push_failure: DEGRADED status recovers to OK after a successful push."""
        exp = fresh_exporter
        syncer = TelemetryGitSyncer(
            repo_dir=".",
            telemetry_dir=temp_telemetry_dir,
            exporter=exp,
        )

        with patch.object(syncer, "_run_git") as mock_git:
            # 1. First run: fail on push
            def failing_git(args, timeout_sec=30):
                if args[0] == "add":
                    return 0, "", ""
                elif args[0] == "diff":
                    return 0, "data/live_telemetry/latest_live_status.json\n", ""
                elif args[0] == "commit":
                    return 0, "[main 12345] telemetry update", ""
                elif args[0] == "push":
                    return 1, "", "Network offline"
                return 0, "", ""

            mock_git.side_effect = failing_git
            res1 = syncer.sync_now(reason="failure_test")
            assert res1 is False
            assert syncer.git_sync_status == "DEGRADED"

            # 2. Second run: recover and succeed
            def successful_git(args, timeout_sec=30):
                if args[0] == "add":
                    return 0, "", ""
                elif args[0] == "diff":
                    return 0, "data/live_telemetry/latest_live_status.json\n", ""
                elif args[0] == "commit":
                    return 0, "[main 12345] telemetry update", ""
                elif args[0] == "push":
                    return 0, "To github.com:traeumen88-netizen/namu-auto-trader.git", ""
                return 0, "", ""

            mock_git.side_effect = successful_git
            res = syncer.sync_now(reason="recovery_test")
            assert res is True
            assert syncer.git_sync_status == "OK"
            assert syncer.last_push_result == "SUCCESS"
