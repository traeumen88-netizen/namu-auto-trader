"""
tests/test_trade_accounting_parity.py
Unit tests for trade accounting parity:
TEST 1: BUY 10 filled, SELL 10 created, SELL unfilled -> Open Pos=10, Realized PnL=0, Sell Amt=0, Completed=0
TEST 2: BUY 10 filled, SELL 10 -> 4 filled -> Open Pos=6, Completed reflects 4, Realized PnL based on 4
TEST 3: SELL 10 fully filled -> Open Pos=0, Realized PnL calculated on full 10
TEST 4: SELL order REJECTED -> Realized PnL=0, Position maintained=10
TEST 5: SELL order CANCELLED -> Realized PnL=0, Position maintained=10
TEST 6: SELL SIGNAL only -> Realized PnL=0, Position maintained=10
TEST 7: RESTART_RECOVERY record -> Not double counted with real strategy trade
"""

import os
import tempfile
import sqlite3
import unittest
from datetime import datetime

from core.trade_accounting import TradeAccountingManager


class TestTradeAccountingParity(unittest.TestCase):
    def setUp(self):
        import gc
        gc.collect()
        self.temp_dir = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.op_db_path = os.path.join(self.temp_dir.name, "operational.db")
        self.trade_db_path = os.path.join(self.temp_dir.name, "trade_history.db")

        with sqlite3.connect(self.op_db_path) as conn:
            conn.execute("""
            CREATE TABLE positions (
                position_id TEXT PRIMARY KEY,
                iem_cd TEXT,
                name TEXT,
                time_horizon TEXT,
                entry_price REAL,
                qty INTEGER,
                stop_price REAL,
                target_1r REAL,
                target_2r REAL,
                status TEXT,
                entry_time TEXT,
                exit_time TEXT,
                exit_price REAL,
                pnl REAL,
                trading_mode TEXT NOT NULL,
                account_no TEXT NOT NULL
            )
            """)
            conn.execute("""
            CREATE TABLE orders (
                client_order_id TEXT PRIMARY KEY,
                broker_order_no TEXT,
                iem_cd TEXT,
                side TEXT,
                order_type TEXT,
                qty INTEGER,
                price REAL,
                status TEXT,
                created_at TEXT,
                sent_at TEXT,
                ack_at TEXT,
                filled_at TEXT,
                trading_mode TEXT NOT NULL,
                account_no TEXT NOT NULL
            )
            """)
            conn.execute("""
            CREATE TABLE fills (
                fill_id TEXT PRIMARY KEY,
                client_order_id TEXT,
                iem_cd TEXT,
                side TEXT,
                filled_qty INTEGER,
                filled_price REAL,
                timestamp TEXT,
                slippage_pct REAL,
                trading_mode TEXT NOT NULL,
                account_no TEXT NOT NULL
            )
            """)

        with sqlite3.connect(self.trade_db_path) as conn:
            conn.execute("""
            CREATE TABLE trades (
                trade_id TEXT PRIMARY KEY,
                symbol TEXT,
                symbol_name TEXT,
                entry_time TEXT,
                exit_time TEXT,
                entry_price REAL,
                exit_price REAL,
                shares INTEGER,
                pnl REAL,
                return_pct REAL,
                r_multiple REAL,
                exit_reason TEXT,
                bad_trade_category TEXT,
                setup_name TEXT,
                trading_mode TEXT,
                account_no TEXT
            )
            """)

        self.mgr = TradeAccountingManager(
            op_db_path=self.op_db_path,
            trade_db_path=self.trade_db_path
        )

    def tearDown(self):
        import gc
        gc.collect()
        try:
            self.temp_dir.cleanup()
        except Exception:
            pass

    def test_test1_sell_unfilled(self):
        """TEST 1: BUY 10 filled, SELL 10 created, SELL unfilled -> Open Pos=10, Realized=0, Sell Amt=0, Completed=0"""
        with sqlite3.connect(self.op_db_path) as conn:
            # BUY 10 filled
            conn.execute("""
            INSERT INTO positions VALUES (
                'POS-T1', '005930', '삼성전자', 'DAY', 70000, 10, 68000, 72000, 74000,
                'OPEN', '2026-09-15 09:10:00', NULL, NULL, 0, 'mock', '50001003032'
            )
            """)
            conn.execute("""
            INSERT INTO fills VALUES (
                'FILL-BUY-T1', 'ORD-BUY-T1', '005930', 'BUY', 10, 70000,
                '2026-09-15 09:10:00', 0.0, 'mock', '50001003032'
            )
            """)
            # SELL order 10 created/sent but unfilled (status = 'PENDING' / 'SENT' / 'ACK')
            conn.execute("""
            INSERT INTO orders VALUES (
                'ORD-SELL-T1', '1001', '005930', 'SELL', '00', 10, 72000,
                'SENT', '2026-09-15 09:30:00', '2026-09-15 09:30:01', '2026-09-15 09:30:02', NULL, 'mock', '50001003032'
            )
            """)

        summary = self.mgr.get_trade_summary(trading_mode='mock', account_no='50001003032')
        self.assertEqual(summary["completed_trades_count"], 0)
        self.assertEqual(summary["realized_pnl"], 0)
        self.assertEqual(summary["total_sell_amount"], 0)
        self.assertEqual(summary["open_positions_count"], 1)
        self.assertEqual(summary["open_positions"][0]["remaining_qty"], 10)
        self.assertEqual(summary["open_orders_count"], 1)
        self.assertEqual(summary["open_orders"][0]["remaining_qty"], 10)

    def test_test2_sell_partial_fill(self):
        """TEST 2: BUY 10 filled, SELL 10 -> 4 filled -> Open Pos=6, Completed reflects 4, Realized based on 4"""
        with sqlite3.connect(self.op_db_path) as conn:
            # Position has 6 remaining
            conn.execute("""
            INSERT INTO positions VALUES (
                'POS-T2', '005930', '삼성전자', 'DAY', 70000, 6, 68000, 72000, 74000,
                'PARTIALLY_CLOSED', '2026-09-15 09:10:00', '2026-09-15 09:35:00', 72000, 8000, 'mock', '50001003032'
            )
            """)
            conn.execute("""
            INSERT INTO fills VALUES (
                'FILL-BUY-T2', 'ORD-BUY-T2', '005930', 'BUY', 10, 70000,
                '2026-09-15 09:10:00', 0.0, 'mock', '50001003032'
            )
            """)
            # SELL order 10 created, 4 filled at 72000
            conn.execute("""
            INSERT INTO orders VALUES (
                'ORD-SELL-T2', '1002', '005930', 'SELL', '00', 10, 72000,
                'PARTIAL_FILL', '2026-09-15 09:30:00', '2026-09-15 09:30:01', '2026-09-15 09:30:02', '2026-09-15 09:35:00', 'mock', '50001003032'
            )
            """)
            conn.execute("""
            INSERT INTO fills VALUES (
                'FILL-SELL-T2', 'ORD-SELL-T2', '005930', 'SELL', 4, 72000,
                '2026-09-15 09:35:00', 0.0, 'mock', '50001003032'
            )
            """)

        summary = self.mgr.get_trade_summary(trading_mode='mock', account_no='50001003032')
        # Open Position = 6
        self.assertEqual(summary["open_positions_count"], 1)
        self.assertEqual(summary["open_positions"][0]["remaining_qty"], 6)
        # Completed Trade reflects 4 shares only
        self.assertEqual(summary["completed_trades_count"], 1)
        self.assertEqual(summary["completed_trades"][0]["qty"], 4)
        self.assertEqual(summary["completed_trades"][0]["sell_amount"], 4 * 72000)
        # Realized PnL is 4 * (72000 - 70000) = 8000
        self.assertEqual(summary["realized_pnl"], 8000)
        # Open Orders remaining qty = 6
        self.assertEqual(summary["open_orders_count"], 1)
        self.assertEqual(summary["open_orders"][0]["remaining_qty"], 6)

    def test_test3_sell_fully_filled(self):
        """TEST 3: SELL 10 fully filled -> Open Pos=0, Realized calculated on full 10"""
        with sqlite3.connect(self.op_db_path) as conn:
            conn.execute("""
            INSERT INTO positions VALUES (
                'POS-T3', '005930', '삼성전자', 'DAY', 70000, 0, 68000, 72000, 74000,
                'CLOSED', '2026-09-15 09:10:00', '2026-09-15 10:00:00', 73000, 30000, 'mock', '50001003032'
            )
            """)
            conn.execute("""
            INSERT INTO fills VALUES (
                'FILL-BUY-T3', 'ORD-BUY-T3', '005930', 'BUY', 10, 70000,
                '2026-09-15 09:10:00', 0.0, 'mock', '50001003032'
            )
            """)
            conn.execute("""
            INSERT INTO orders VALUES (
                'ORD-SELL-T3', '1003', '005930', 'SELL', '00', 10, 73000,
                'FILLED', '2026-09-15 09:59:00', '2026-09-15 09:59:01', '2026-09-15 09:59:02', '2026-09-15 10:00:00', 'mock', '50001003032'
            )
            """)
            conn.execute("""
            INSERT INTO fills VALUES (
                'FILL-SELL-T3', 'ORD-SELL-T3', '005930', 'SELL', 10, 73000,
                '2026-09-15 10:00:00', 0.0, 'mock', '50001003032'
            )
            """)

        summary = self.mgr.get_trade_summary(trading_mode='mock', account_no='50001003032')
        self.assertEqual(summary["open_positions_count"], 0)
        self.assertEqual(summary["completed_trades_count"], 1)
        self.assertEqual(summary["completed_trades"][0]["qty"], 10)
        self.assertEqual(summary["completed_trades"][0]["sell_amount"], 10 * 73000)
        self.assertEqual(summary["realized_pnl"], 10 * (73000 - 70000))
        self.assertEqual(summary["open_orders_count"], 0)

    def test_test4_sell_rejected(self):
        """TEST 4: SELL order REJECTED -> Realized PnL=0, Position maintained=10"""
        with sqlite3.connect(self.op_db_path) as conn:
            conn.execute("""
            INSERT INTO positions VALUES (
                'POS-T4', '005930', '삼성전자', 'DAY', 70000, 0, 68000, 72000, 74000,
                'CLOSED', '2026-09-15 09:10:00', '2026-09-15 09:30:00', 72000, 20000, 'mock', '50001003032'
            )
            """)
            conn.execute("""
            INSERT INTO orders VALUES (
                'ORD-SELL-T4', '1004', '005930', 'SELL', '00', 10, 72000,
                'REJECTED', '2026-09-15 09:30:00', NULL, NULL, NULL, 'mock', '50001003032'
            )
            """)

        summary = self.mgr.get_trade_summary(trading_mode='mock', account_no='50001003032')
        self.assertEqual(summary["realized_pnl"], 0)
        self.assertEqual(summary["total_sell_amount"], 0)
        self.assertEqual(summary["completed_trades_count"], 0)
        self.assertEqual(summary["open_positions_count"], 1)
        self.assertEqual(summary["open_positions"][0]["remaining_qty"], 10)
        self.assertEqual(summary["open_orders_count"], 0)

    def test_test5_sell_cancelled(self):
        """TEST 5: SELL order CANCELLED -> Realized PnL=0, Position maintained=10"""
        with sqlite3.connect(self.op_db_path) as conn:
            conn.execute("""
            INSERT INTO positions VALUES (
                'POS-T5', '005930', '삼성전자', 'DAY', 70000, 0, 68000, 72000, 74000,
                'CLOSED', '2026-09-15 09:10:00', '2026-09-15 09:30:00', 72000, 20000, 'mock', '50001003032'
            )
            """)
            conn.execute("""
            INSERT INTO orders VALUES (
                'ORD-SELL-T5', '1005', '005930', 'SELL', '00', 10, 72000,
                'CANCELLED', '2026-09-15 09:30:00', '2026-09-15 09:30:01', NULL, NULL, 'mock', '50001003032'
            )
            """)

        summary = self.mgr.get_trade_summary(trading_mode='mock', account_no='50001003032')
        self.assertEqual(summary["realized_pnl"], 0)
        self.assertEqual(summary["total_sell_amount"], 0)
        self.assertEqual(summary["completed_trades_count"], 0)
        self.assertEqual(summary["open_positions_count"], 1)
        self.assertEqual(summary["open_positions"][0]["remaining_qty"], 10)
        self.assertEqual(summary["open_orders_count"], 0)

    def test_test6_sell_signal_only(self):
        """TEST 6: SELL SIGNAL only -> Realized PnL=0, Position maintained=10"""
        with sqlite3.connect(self.op_db_path) as conn:
            # Position OPEN with 10 shares, no sell order or fills created
            conn.execute("""
            INSERT INTO positions VALUES (
                'POS-T6', '005930', '삼성전자', 'DAY', 70000, 10, 68000, 72000, 74000,
                'OPEN', '2026-09-15 09:10:00', NULL, NULL, 0, 'mock', '50001003032'
            )
            """)

        summary = self.mgr.get_trade_summary(trading_mode='mock', account_no='50001003032')
        self.assertEqual(summary["realized_pnl"], 0)
        self.assertEqual(summary["total_sell_amount"], 0)
        self.assertEqual(summary["completed_trades_count"], 0)
        self.assertEqual(summary["open_positions_count"], 1)
        self.assertEqual(summary["open_positions"][0]["remaining_qty"], 10)
        self.assertEqual(summary["open_orders_count"], 0)

    def test_test7_restart_recovery_deduplication(self):
        """TEST 7: RESTART_RECOVERY record exists -> Not double counted with real strategy trade"""
        with sqlite3.connect(self.op_db_path) as conn:
            # Real strategy closed position
            conn.execute("""
            INSERT INTO positions VALUES (
                'POS-REAL-01', '005930', '삼성전자', 'DAY', 70000, 0, 68000, 72000, 74000,
                'CLOSED', '2026-09-15 09:10:00', '2026-09-15 10:00:00', 72000, 20000, 'mock', '50001003032'
            )
            """)
            conn.execute("""
            INSERT INTO orders VALUES (
                'ORD-SELL-REAL', '1007', '005930', 'SELL', '00', 10, 72000,
                'FILLED', '2026-09-15 09:59:00', '2026-09-15 09:59:01', '2026-09-15 09:59:02', '2026-09-15 10:00:00', 'mock', '50001003032'
            )
            """)
            conn.execute("""
            INSERT INTO fills VALUES (
                'FILL-SELL-REAL', 'ORD-SELL-REAL', '005930', 'SELL', 10, 72000,
                '2026-09-15 10:00:00', 0.0, 'mock', '50001003032'
            )
            """)

        with sqlite3.connect(self.trade_db_path) as conn:
            # Duplicate trade history record with RESTART_RECOVERY
            conn.execute("""
            INSERT INTO trades VALUES (
                'T-REC-001', '005930', '삼성전자', '2026-09-15 09:10:00', '2026-09-15 10:00:00',
                70000, 72000, 10, 20000, 2.85, 1.0, '재시작 복구 청산', 'RESTART_RECOVERY',
                'RESTART_RECOVERY', 'mock', '50001003032'
            )
            """)
            # Another duplicate trade record with same timestamp
            conn.execute("""
            INSERT INTO trades VALUES (
                'POS-REAL-01', '005930', '삼성전자', '2026-09-15 09:10:00', '2026-09-15 10:00:00',
                70000, 72000, 10, 20000, 2.85, 1.0, '정상 익절', 'NONE',
                'BREAKOUT_V1', 'mock', '50001003032'
            )
            """)

        summary = self.mgr.get_trade_summary(trading_mode='mock', account_no='50001003032')
        # Exactly 1 completed trade, NOT 2 or 3
        self.assertEqual(summary["completed_trades_count"], 1)
        self.assertEqual(summary["realized_pnl"], 20000)
        self.assertEqual(summary["total_sell_amount"], 720000)


if __name__ == '__main__':
    unittest.main()
