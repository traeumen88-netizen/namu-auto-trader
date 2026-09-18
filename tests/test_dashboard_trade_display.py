"""
tests/test_dashboard_trade_display.py
Unit tests for strict 3-way dashboard trade accounting:
1. COMPLETED TRADES (Round-trip closed trades ONLY)
2. OPEN POSITIONS (Unrealized PnL only)
3. OPEN ORDERS (Pending/unfilled orders only)
4. RESTART_RECOVERY exclusion and deduplication
"""

import os
import tempfile
import sqlite3
import unittest
from datetime import datetime

from core.trade_accounting import TradeAccountingManager


class TestDashboardTradeDisplay(unittest.TestCase):
    def setUp(self):
        import gc
        gc.collect()
        self.temp_dir = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.op_db_path = os.path.join(self.temp_dir.name, "operational.db")
        self.trade_db_path = os.path.join(self.temp_dir.name, "trade_history.db")

        # Create schemas
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

        self.accounting_mgr = TradeAccountingManager(
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

    def test_completed_trade_only(self):
        """1. Only closed trades with remaining_qty == 0 and status == 'CLOSED' appear in completed trades."""
        with sqlite3.connect(self.op_db_path) as conn:
            # Fully closed trade
            conn.execute("""
            INSERT INTO positions VALUES (
                'POS-001', '005930', '삼성전자', '당일단타', 70000, 0, 68000, 72000, 74000,
                'CLOSED', '2026-09-15 09:10:00', '2026-09-15 10:00:00', 72000, 20000, 'mock', '50001003032'
            )
            """)
            # Still open position
            conn.execute("""
            INSERT INTO positions VALUES (
                'POS-002', '000660', 'SK하이닉스', '당일단타', 150000, 10, 145000, 155000, 160000,
                'OPEN', '2026-09-15 09:30:00', NULL, NULL, 0, 'mock', '50001003032'
            )
            """)

        completed = self.accounting_mgr.get_completed_trades(trading_mode='mock', account_no='50001003032')
        self.assertEqual(len(completed), 1)
        self.assertEqual(completed[0]["position_id"], "POS-001")
        self.assertEqual(completed[0]["status"], "CLOSED")
        self.assertEqual(completed[0]["remaining_qty"], 0)

    def test_open_position_excluded_from_realized(self):
        """2. Open positions with unrealized PnL are excluded from realized PnL."""
        with sqlite3.connect(self.op_db_path) as conn:
            conn.execute("""
            INSERT INTO positions VALUES (
                'POS-OPEN', '005930', '삼성전자', '당일단타', 70000, 10, 68000, 72000, 74000,
                'OPEN', '2026-09-15 09:10:00', '2026-09-15 10:00:00', 75000, 50000, 'mock', '50001003032'
            )
            """)

        summary = self.accounting_mgr.get_trade_summary(trading_mode='mock', account_no='50001003032')
        self.assertEqual(summary["realized_pnl"], 0)
        self.assertEqual(summary["completed_trades_count"], 0)
        self.assertEqual(summary["open_positions_count"], 1)
        self.assertEqual(summary["unrealized_pnl"], 50000)

    def test_partial_close_excluded_from_completed(self):
        """3. Partially closed positions (remaining_qty > 0) are excluded from completed trades."""
        with sqlite3.connect(self.op_db_path) as conn:
            conn.execute("""
            INSERT INTO positions VALUES (
                'POS-PARTIAL', '005930', '삼성전자', '당일단타', 70000, 5, 68000, 72000, 74000,
                'PARTIALLY_CLOSED', '2026-09-15 09:10:00', NULL, 72000, 10000, 'mock', '50001003032'
            )
            """)

        completed = self.accounting_mgr.get_completed_trades(trading_mode='mock')
        self.assertEqual(len(completed), 0)

        open_pos = self.accounting_mgr.get_open_positions(trading_mode='mock')
        self.assertEqual(len(open_pos), 1)
        self.assertEqual(open_pos[0]["symbol"], "005930")
        self.assertEqual(open_pos[0]["remaining_qty"], 5)

    def test_unfilled_excluded_from_completed(self):
        """4. Unfilled pending orders are excluded from completed trades and open positions."""
        with sqlite3.connect(self.op_db_path) as conn:
            conn.execute("""
            INSERT INTO orders VALUES (
                'ORD-UNFILLED', '1001', '005930', 'BUY', '00', 10, 70000,
                'PENDING', '2026-09-15 09:10:00', NULL, NULL, NULL, 'mock', '50001003032'
            )
            """)

        completed = self.accounting_mgr.get_completed_trades(trading_mode='mock')
        self.assertEqual(len(completed), 0)

        open_pos = self.accounting_mgr.get_open_positions(trading_mode='mock')
        self.assertEqual(len(open_pos), 0)

        open_orders = self.accounting_mgr.get_open_orders(trading_mode='mock')
        self.assertEqual(len(open_orders), 1)
        self.assertEqual(open_orders[0]["order_id"], "ORD-UNFILLED")
        self.assertEqual(open_orders[0]["remaining_qty"], 10)

    def test_canceled_excluded_from_completed(self):
        """5. Canceled orders are excluded from completed trades and open orders."""
        with sqlite3.connect(self.op_db_path) as conn:
            conn.execute("""
            INSERT INTO orders VALUES (
                'ORD-CANCELED', '1002', '005930', 'BUY', '00', 10, 70000,
                'CANCELED', '2026-09-15 09:10:00', NULL, NULL, NULL, 'mock', '50001003032'
            )
            """)

        completed = self.accounting_mgr.get_completed_trades(trading_mode='mock')
        self.assertEqual(len(completed), 0)

        open_orders = self.accounting_mgr.get_open_orders(trading_mode='mock')
        self.assertEqual(len(open_orders), 0)

    def test_rejected_excluded_from_completed(self):
        """6. Rejected orders are excluded from completed trades and open orders."""
        with sqlite3.connect(self.op_db_path) as conn:
            conn.execute("""
            INSERT INTO orders VALUES (
                'ORD-REJECTED', '1003', '005930', 'BUY', '00', 10, 70000,
                'REJECTED', '2026-09-15 09:10:00', NULL, NULL, NULL, 'mock', '50001003032'
            )
            """)

        completed = self.accounting_mgr.get_completed_trades(trading_mode='mock')
        self.assertEqual(len(completed), 0)

        open_orders = self.accounting_mgr.get_open_orders(trading_mode='mock')
        self.assertEqual(len(open_orders), 0)

    def test_round_trip_deduplication(self):
        """7. Duplicate records for the same round-trip across sources are deduplicated."""
        with sqlite3.connect(self.op_db_path) as conn:
            conn.execute("""
            INSERT INTO positions VALUES (
                'TR-DUP-01', '005930', '삼성전자', '당일단타', 70000, 0, 68000, 72000, 74000,
                'CLOSED', '2026-09-15 09:10:00', '2026-09-15 10:00:00', 72000, 20000, 'mock', '50001003032'
            )
            """)

        with sqlite3.connect(self.trade_db_path) as conn:
            # Same position_id/trade_id inserted in trade_history
            conn.execute("""
            INSERT INTO trades VALUES (
                'TR-DUP-01', '005930', '삼성전자', '2026-09-15 09:10:00', '2026-09-15 10:00:00',
                70000, 72000, 10, 20000, 2.85, 1.0, '익절', 'NONE', 'VPCI_MOMENTUM', 'mock', '50001003032'
            )
            """)

        completed = self.accounting_mgr.get_completed_trades(trading_mode='mock')
        self.assertEqual(len(completed), 1)
        self.assertEqual(completed[0]["position_id"], "TR-DUP-01")

    def test_realized_unrealized_separation(self):
        """8. Realized and unrealized PnL are completely separated, and total is sum."""
        with sqlite3.connect(self.op_db_path) as conn:
            # Closed trade: +30,000
            conn.execute("""
            INSERT INTO positions VALUES (
                'POS-CLOSED', '005930', '삼성전자', '당일단타', 70000, 0, 68000, 72000, 74000,
                'CLOSED', '2026-09-15 09:10:00', '2026-09-15 10:00:00', 73000, 30000, 'mock', '50001003032'
            )
            """)
            # Open position: -10,000
            conn.execute("""
            INSERT INTO positions VALUES (
                'POS-OPEN', '000660', 'SK하이닉스', '당일단타', 150000, 10, 145000, 155000, 160000,
                'OPEN', '2026-09-15 09:30:00', NULL, 149000, -10000, 'mock', '50001003032'
            )
            """)

        summary = self.accounting_mgr.get_trade_summary(trading_mode='mock', account_no='50001003032')
        self.assertEqual(summary["realized_pnl"], 30000)
        self.assertEqual(summary["unrealized_pnl"], -10000)
        self.assertEqual(summary["total_pnl"], 20000)
        self.assertEqual(summary["completed_trades_count"], 1)
        self.assertEqual(summary["open_positions_count"], 1)

    def test_restart_recovery_excluded(self):
        """9. RESTART_RECOVERY and RESTART_RECOVERY_SCALE_OUT rows are strictly excluded."""
        with sqlite3.connect(self.trade_db_path) as conn:
            conn.execute("""
            INSERT INTO trades VALUES (
                'REC-01', '005930', '삼성전자', '2026-09-15 09:00:00', '2026-09-15 09:01:00',
                70000, 69000, 10, -10000, -1.4, -0.5, '재시작 복구', 'RESTART_RECOVERY',
                'RESTART_RECOVERY', 'mock', '50001003032'
            )
            """)
            conn.execute("""
            INSERT INTO trades VALUES (
                'REC-02', '000660', 'SK하이닉스', '2026-09-15 09:00:00', '2026-09-15 09:01:00',
                150000, 148000, 5, -10000, -1.3, -0.5, '재시작 분할매도', 'RESTART_RECOVERY_SCALE_OUT',
                'RESTART_RECOVERY_SCALE_OUT', 'mock', '50001003032'
            )
            """)
            conn.execute("""
            INSERT INTO trades VALUES (
                'LEGIT-01', '035420', 'NAVER', '2026-09-15 09:30:00', '2026-09-15 10:15:00',
                200000, 206000, 10, 60000, 3.0, 1.2, '정상 익절', 'NONE',
                'BREAKOUT_V1', 'mock', '50001003032'
            )
            """)

        completed = self.accounting_mgr.get_completed_trades(trading_mode='mock', account_no='50001003032')
        self.assertEqual(len(completed), 1)
        self.assertEqual(completed[0]["trade_id"], "LEGIT-01")
        self.assertEqual(completed[0]["net_pnl"], 60000)


if __name__ == '__main__':
    unittest.main()
