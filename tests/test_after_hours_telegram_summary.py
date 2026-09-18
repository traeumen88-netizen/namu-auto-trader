"""
tests/test_after_hours_telegram_summary.py
Unit tests for after-hours spike Telegram daily summary batching:
TEST 1: Detect 30 after-hours spikes -> 0 immediate alerts, 30 DB records saved.
TEST 2: Scanner runs repeatedly on same trade date -> 0 individual alerts, detailed data maintained.
TEST 3: After-hours ends -> exactly 1 DAILY SUMMARY Telegram sent.
TEST 4: Restart process & rerun daily summary -> 0 additional summaries sent (Idempotent).
TEST 5: Next trade date new after-hours spikes -> exactly 1 new summary sent for new date.
TEST 6: Actual BUY/SELL/FILL trade alerts -> unchanged and do not conflict with daily summary.
"""

import os
import tempfile
import sqlite3
import unittest
from datetime import datetime, timedelta
from unittest.mock import MagicMock

from core.after_hours_manager import AfterHoursManager, WatchlistStatus
from core.telegram_notifier import TelegramNotifier


class MockTelegramNotifier:
    def __init__(self, db_path):
        self.db_path = db_path
        from core.telegram_feedback_manager import TelegramFeedbackManager
        self.feedback_manager = TelegramFeedbackManager(db_path=db_path)
        self.idempotency = self.feedback_manager.idempotency
        self.sent_messages = []
        self.spike_alerts_count = 0
        self.daily_summaries_count = 0
        self.trade_alerts_count = 0

    def send_message(self, text, parse_mode="HTML", reply_markup=None, idempotency_key=None,
                     message_type=None, business_date=None, event_id=None, trade_id=None, feedback_id=None):
        # Check idempotency
        if idempotency_key and self.idempotency.is_sent(idempotency_key):
            return False

        self.sent_messages.append({
            "text": text,
            "message_type": message_type,
            "idempotency_key": idempotency_key,
            "business_date": business_date
        })

        if message_type == "AFTER_HOURS_SPIKE":
            self.spike_alerts_count += 1
        elif message_type == "AFTER_HOURS_DAILY_SUMMARY":
            self.daily_summaries_count += 1
        elif message_type in ("TRADE_EVENT", "BUY", "SELL", "FILL"):
            self.trade_alerts_count += 1

        if idempotency_key:
            self.idempotency.mark_sent(
                idempotency_key=idempotency_key,
                message_type=message_type or "MESSAGE",
                business_date=business_date or datetime.now().strftime("%Y-%m-%d"),
                event_id=event_id,
                trade_id=trade_id
            )
        return True

    def send_after_hours_spike_alert(self, symbol, name, after_hours_return_pct, volume=0):
        self.spike_alerts_count += 1
        return self.send_message(
            f"Spike {name}",
            message_type="AFTER_HOURS_SPIKE",
            event_id=f"AH_{symbol}"
        )


class TestAfterHoursTelegramSummary(unittest.TestCase):
    def setUp(self):
        import gc
        gc.collect()
        self.temp_dir = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.db_path = os.path.join(self.temp_dir.name, "operational.db")

        # Initialize schema
        with sqlite3.connect(self.db_path) as conn:
            conn.execute("""
            CREATE TABLE after_hours_candidates (
                iem_cd TEXT PRIMARY KEY,
                name TEXT NOT NULL,
                detected_at TEXT NOT NULL,
                regular_close REAL NOT NULL,
                after_hours_price REAL NOT NULL,
                after_hours_return REAL NOT NULL,
                after_hours_volume INTEGER NOT NULL,
                after_hours_turnover REAL NOT NULL,
                after_hours_regime TEXT NOT NULL,
                spike_bracket TEXT NOT NULL,
                next_session_watchlist INTEGER NOT NULL DEFAULT 1,
                status TEXT NOT NULL DEFAULT 'ACTIVE',
                next_day_revalidated INTEGER NOT NULL DEFAULT 0,
                next_day_decision TEXT,
                next_day_reason TEXT
            )
            """)
            conn.execute("""
            CREATE TABLE telegram_sent_messages (
                idempotency_key TEXT PRIMARY KEY,
                message_type TEXT NOT NULL,
                event_id TEXT,
                trade_id TEXT,
                feedback_id TEXT,
                business_date TEXT,
                sent_at TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'SENT',
                content_hash TEXT
            )
            """)

        self.mock_tg = MockTelegramNotifier(db_path=self.db_path)
        self.ah_mgr = AfterHoursManager(
            db_path=self.db_path,
            telegram_notifier=self.mock_tg
        )

    def tearDown(self):
        import gc
        gc.collect()
        try:
            self.temp_dir.cleanup()
        except Exception:
            pass

    def test_test1_spike_detection_no_immediate_telegram(self):
        """TEST 1: Detect 30 after-hours spikes -> 0 immediate alerts, 30 DB records saved."""
        now = datetime(2026, 9, 15, 16, 0, 0)
        for i in range(30):
            sym = f"{i+1:06d}"
            cand = self.ah_mgr.detect_after_hours_spike(
                iem_cd=sym,
                name=f"종목_{i+1}",
                regular_close=10000.0,
                current_price=10500.0 + (i * 50),
                volume=2000 + (i * 100),
                turnover=20000000.0 + (i * 1000000),
                regime="BULL",
                dt=now
            )
            self.assertIsNotNone(cand)

        # 0 immediate Telegram alerts sent!
        self.assertEqual(self.mock_tg.spike_alerts_count, 0)
        self.assertEqual(len(self.mock_tg.sent_messages), 0)

        # 30 records saved in database!
        with sqlite3.connect(self.db_path) as conn:
            cnt = conn.execute("SELECT count(*) FROM after_hours_candidates").fetchone()[0]
            self.assertEqual(cnt, 30)

    def test_test2_scanner_repeated_runs_no_alert_data_preserved(self):
        """TEST 2: Scanner runs repeatedly on same trade date -> 0 individual alerts, detailed data maintained."""
        now = datetime(2026, 9, 15, 16, 5, 0)
        # Scan cycle 1
        for i in range(5):
            self.ah_mgr.detect_after_hours_spike(
                iem_cd=f"00000{i}",
                name=f"반복종목_{i}",
                regular_close=10000.0,
                current_price=10600.0,
                volume=5000,
                dt=now
            )
        # Scan cycle 2 (3 seconds later)
        now2 = datetime(2026, 9, 15, 16, 5, 3)
        for i in range(5):
            self.ah_mgr.detect_after_hours_spike(
                iem_cd=f"00000{i}",
                name=f"반복종목_{i}",
                regular_close=10000.0,
                current_price=10600.0,
                volume=5000,
                dt=now2
            )

        self.assertEqual(self.mock_tg.spike_alerts_count, 0)
        with sqlite3.connect(self.db_path) as conn:
            cnt = conn.execute("SELECT count(*) FROM after_hours_candidates").fetchone()[0]
            self.assertEqual(cnt, 5)

    def test_test3_after_hours_end_daily_summary_exactly_one(self):
        """TEST 3: After-hours ends -> exactly 1 DAILY SUMMARY Telegram sent."""
        trade_date = "2026-09-15"
        now = datetime(2026, 9, 15, 16, 30, 0)
        # Register 10 candidates
        for i in range(10):
            self.ah_mgr.detect_after_hours_spike(
                iem_cd=f"10000{i}",
                name=f"종목_{i}",
                regular_close=10000.0,
                current_price=10500.0 + (i * 100),
                volume=1500 + (i * 200),
                turnover=15000000.0,
                dt=now
            )

        # Trigger summary at EOD
        sent = self.ah_mgr.send_after_hours_daily_summary(trade_date=trade_date)
        self.assertTrue(sent)
        self.assertEqual(self.mock_tg.daily_summaries_count, 1)

        # Check summary content
        msg = self.mock_tg.sent_messages[0]
        self.assertEqual(msg["message_type"], "AFTER_HOURS_DAILY_SUMMARY")
        self.assertEqual(msg["idempotency_key"], "2026-09-15_AFTER_HOURS_DAILY_SUMMARY")
        self.assertIn("시간외 급등 일일 분석", msg["text"])
        self.assertIn("급등 감지: 10종목", msg["text"])

    def test_test4_restart_idempotency_no_duplicate_summary(self):
        """TEST 4: Restart process & rerun daily summary -> 0 additional summaries sent (Idempotent)."""
        trade_date = "2026-09-15"
        now = datetime(2026, 9, 15, 16, 30, 0)
        for i in range(5):
            self.ah_mgr.detect_after_hours_spike(
                iem_cd=f"20000{i}",
                name=f"종목_{i}",
                regular_close=10000.0,
                current_price=10600.0,
                volume=2000,
                dt=now
            )

        # First send
        res1 = self.ah_mgr.send_after_hours_daily_summary(trade_date=trade_date)
        self.assertTrue(res1)
        self.assertEqual(self.mock_tg.daily_summaries_count, 1)

        # Simulate program restart: new AfterHoursManager and new Notifier with same DB
        restarted_notifier = MockTelegramNotifier(db_path=self.db_path)
        restarted_ah_mgr = AfterHoursManager(
            db_path=self.db_path,
            telegram_notifier=restarted_notifier
        )

        # Attempt to resend on restart
        res2 = restarted_ah_mgr.send_after_hours_daily_summary(trade_date=trade_date)
        self.assertFalse(res2)
        # Count remains 0 in new notifier, total sent messages did not increase
        self.assertEqual(restarted_notifier.daily_summaries_count, 0)

    def test_test5_next_day_new_summary(self):
        """TEST 5: Next trade date new after-hours spikes -> exactly 1 new summary sent for new date."""
        # Day 1: 2026-09-15
        day1 = "2026-09-15"
        self.ah_mgr.detect_after_hours_spike(
            iem_cd="300001", name="Day1종목", regular_close=10000.0, current_price=10500.0,
            volume=2000, dt=datetime(2026, 9, 15, 16, 0)
        )
        self.ah_mgr.send_after_hours_daily_summary(trade_date=day1)
        self.assertEqual(self.mock_tg.daily_summaries_count, 1)

        # Day 2: 2026-09-16
        day2 = "2026-09-16"
        self.ah_mgr.detect_after_hours_spike(
            iem_cd="300002", name="Day2종목", regular_close=20000.0, current_price=21000.0,
            volume=3000, dt=datetime(2026, 9, 16, 16, 0)
        )
        sent_day2 = self.ah_mgr.send_after_hours_daily_summary(trade_date=day2)
        self.assertTrue(sent_day2)
        self.assertEqual(self.mock_tg.daily_summaries_count, 2)

    def test_test6_real_trade_telegram_alerts_preserved(self):
        """TEST 6: Actual BUY/SELL/FILL trade alerts -> unchanged and do not conflict with daily summary."""
        # Send a normal trade alert
        trade_sent = self.mock_tg.send_message(
            text="BUY 005930 10주 체결",
            message_type="TRADE_EVENT",
            idempotency_key="TRADE_BUY_005930_1001",
            trade_id="TR-001"
        )
        self.assertTrue(trade_sent)
        self.assertEqual(self.mock_tg.trade_alerts_count, 1)

        # Send daily summary
        self.ah_mgr.detect_after_hours_spike(
            iem_cd="400001", name="테스트종목", regular_close=10000.0, current_price=10500.0,
            volume=2000, dt=datetime(2026, 9, 15, 16, 0)
        )
        sum_sent = self.ah_mgr.send_after_hours_daily_summary(trade_date="2026-09-15")
        self.assertTrue(sum_sent)
        self.assertEqual(self.mock_tg.daily_summaries_count, 1)

        # Both exist independently without conflict
        self.assertEqual(len(self.mock_tg.sent_messages), 2)
        types = [m["message_type"] for m in self.mock_tg.sent_messages]
        self.assertIn("TRADE_EVENT", types)
        self.assertIn("AFTER_HOURS_DAILY_SUMMARY", types)


if __name__ == '__main__':
    unittest.main()
