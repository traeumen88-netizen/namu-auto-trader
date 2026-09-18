"""
tests/test_audit_pnl_reconciliation.py
PnL 집계, 손실 거래 보존, LIVE/MOCK 격리, 중복 제거 및 기간별 실현손익 검증 테스트 스위트
User Requirements TEST 1 ~ TEST 8 전수 검증
"""

import os
import sys
import unittest
import sqlite3
from datetime import datetime

import uuid

base_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if base_dir not in sys.path:
    sys.path.insert(0, base_dir)

from core.trade_accounting import TradeAccountingManager


class TestAuditPnLReconciliation(unittest.TestCase):
    def setUp(self):
        self.test_dir = os.path.join(base_dir, "tests", "scratch_audit_test")
        os.makedirs(self.test_dir, exist_ok=True)
        unique_id = uuid.uuid4().hex
        self.op_db = os.path.join(self.test_dir, f"op_{unique_id}.db")
        self.trade_db = os.path.join(self.test_dir, f"trade_{unique_id}.db")

        # Init DBs
        with sqlite3.connect(self.op_db) as conn:
            conn.execute("""
                CREATE TABLE positions (
                    pos_id TEXT PRIMARY KEY,
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
                    trading_mode TEXT,
                    account_no TEXT,
                    signal_session TEXT,
                    entry_session TEXT
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
                    trading_mode TEXT,
                    account_no TEXT,
                    signal_session TEXT,
                    entry_session TEXT
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
                    trading_mode TEXT,
                    account_no TEXT
                )
            """)

        with sqlite3.connect(self.trade_db) as conn:
            conn.execute("""
                CREATE TABLE trades (
                    trade_id TEXT PRIMARY KEY,
                    symbol TEXT,
                    symbol_name TEXT,
                    setup_name TEXT,
                    time_horizon TEXT,
                    side TEXT,
                    entry_time TEXT,
                    exit_time TEXT,
                    entry_price REAL,
                    exit_price REAL,
                    shares INTEGER,
                    stop_price REAL,
                    target_price REAL,
                    pnl REAL,
                    return_pct REAL,
                    r_multiple REAL,
                    mae_pct REAL,
                    mfe_pct REAL,
                    mae_r REAL,
                    mfe_r REAL,
                    holding_seconds REAL,
                    model_version TEXT,
                    p_target_pred REAL,
                    p_stop_pred REAL,
                    expected_net_r_pred REAL,
                    bad_trade_category TEXT,
                    raw_features TEXT,
                    created_at TEXT,
                    exit_reason TEXT,
                    trading_mode TEXT,
                    account_no TEXT,
                    signal_session TEXT,
                    entry_session TEXT
                )
            """)

        self.tam = TradeAccountingManager(op_db_path=self.op_db, trade_db_path=self.trade_db)

    def tearDown(self):
        try:
            if os.path.exists(self.op_db):
                os.remove(self.op_db)
        except Exception:
            pass
        try:
            if os.path.exists(self.trade_db):
                os.remove(self.trade_db)
        except Exception:
            pass

    def test_01_full_loss_trade_realized_pnl_negative(self):
        """TEST 1: 100주 매수 후 100주를 손실 매도 -> Realized PnL < 0"""
        today = datetime.now().strftime("%Y-%m-%d")
        with sqlite3.connect(self.op_db) as conn:
            conn.execute("""
                INSERT INTO positions (pos_id, iem_cd, name, entry_price, qty, status, entry_time, exit_time, exit_price, pnl, trading_mode, account_no)
                VALUES ('POS_1', '005930', '삼성전자', 70000, 0, 'CLOSED', ?, ?, 65000, -500000, 'live', '20201549311')
            """, (f"{today}T09:05:00", f"{today}T09:30:00"))
            conn.execute("""
                INSERT INTO orders (client_order_id, iem_cd, side, qty, price, status, created_at, trading_mode, account_no)
                VALUES ('ORD_SELL_1', '005930', 'SELL', 100, 65000, 'FILLED', ?, 'live', '20201549311')
            """, (f"{today}T09:30:00",))
            conn.execute("""
                INSERT INTO fills (fill_id, client_order_id, iem_cd, side, filled_qty, filled_price, timestamp, trading_mode, account_no)
                VALUES ('FILL_1', 'ORD_SELL_1', '005930', 'SELL', 100, 65000, ?, 'live', '20201549311')
            """, (f"{today}T09:30:01",))

        summary = self.tam.get_trade_summary(trading_mode='live', account_no='20201549311', date_str=today)
        pstats = self.tam.get_period_statistics(trading_mode='live', account_no='20201549311')

        self.assertEqual(summary["completed_trades_count"], 1)
        self.assertEqual(summary["realized_pnl"], -500000)
        self.assertLess(summary["realized_pnl"], 0)
        self.assertEqual(pstats["today"]["total_pnl"], -500000)
        self.assertLess(pstats["today"]["total_pnl"], 0)

    def test_02_partial_loss_sell_reflects_only_filled_portion(self):
        """TEST 2: 50주 부분 손실 매도 -> 손실 체결분만 realized PnL 반영"""
        today = datetime.now().strftime("%Y-%m-%d")
        with sqlite3.connect(self.op_db) as conn:
            conn.execute("""
                INSERT INTO positions (pos_id, iem_cd, name, entry_price, qty, status, entry_time, trading_mode, account_no)
                VALUES ('POS_2', '000660', 'SK하이닉스', 100000, 50, 'PARTIALLY_CLOSED', ?, 'live', '20201549311')
            """, (f"{today}T09:10:00",))
            conn.execute("""
                INSERT INTO orders (client_order_id, iem_cd, side, qty, price, status, created_at, trading_mode, account_no)
                VALUES ('ORD_SELL_2', '000660', 'SELL', 100, 90000, 'PARTIAL_FILL', ?, 'live', '20201549311')
            """, (f"{today}T09:40:00",))
            conn.execute("""
                INSERT INTO fills (fill_id, client_order_id, iem_cd, side, filled_qty, filled_price, timestamp, trading_mode, account_no)
                VALUES ('FILL_2', 'ORD_SELL_2', '000660', 'SELL', 50, 90000, ?, 'live', '20201549311')
            """, (f"{today}T09:40:02",))

        summary = self.tam.get_trade_summary(trading_mode='live', account_no='20201549311', date_str=today)
        self.assertEqual(summary["completed_trades_count"], 1)
        self.assertEqual(summary["realized_pnl"], -500000)  # (90,000 - 100,000) * 50
        self.assertEqual(summary["open_positions_count"], 1)
        self.assertEqual(summary["open_positions"][0]["qty"], 50)

    def test_03_mixed_gain_and_loss_sum_exact(self):
        """TEST 3: 손실 거래와 수익 거래가 함께 존재 -> 전체 합산 손익이 정확히 계산되어야 함"""
        today = datetime.now().strftime("%Y-%m-%d")
        with sqlite3.connect(self.op_db) as conn:
            # Win trade: +200,000
            conn.execute("""
                INSERT INTO positions (pos_id, iem_cd, name, entry_price, qty, status, entry_time, exit_time, exit_price, pnl, trading_mode, account_no)
                VALUES ('POS_W', '035420', 'NAVER', 200000, 0, 'CLOSED', ?, ?, 220000, 200000, 'live', '20201549311')
            """, (f"{today}T09:00:00", f"{today}T09:20:00"))
            conn.execute("""
                INSERT INTO fills (fill_id, client_order_id, iem_cd, side, filled_qty, filled_price, timestamp, trading_mode, account_no)
                VALUES ('FILL_W', 'ORD_W', '035420', 'SELL', 10, 220000, ?, 'live', '20201549311')
            """, (f"{today}T09:20:01",))

            # Loss trade: -300,000
            conn.execute("""
                INSERT INTO positions (pos_id, iem_cd, name, entry_price, qty, status, entry_time, exit_time, exit_price, pnl, trading_mode, account_no)
                VALUES ('POS_L', '035720', '카카오', 50000, 0, 'CLOSED', ?, ?, 40000, -300000, 'live', '20201549311')
            """, (f"{today}T09:05:00", f"{today}T09:25:00"))
            conn.execute("""
                INSERT INTO fills (fill_id, client_order_id, iem_cd, side, filled_qty, filled_price, timestamp, trading_mode, account_no)
                VALUES ('FILL_L', 'ORD_L', '035720', 'SELL', 30, 40000, ?, 'live', '20201549311')
            """, (f"{today}T09:25:01",))

        summary = self.tam.get_trade_summary(trading_mode='live', account_no='20201549311', date_str=today)
        pstats = self.tam.get_period_statistics(trading_mode='live', account_no='20201549311')

        # Net = +200,000 - 300,000 = -100,000
        self.assertEqual(summary["realized_pnl"], -100000)
        self.assertEqual(pstats["today"]["total_pnl"], -100000)
        self.assertEqual(pstats["today"]["win_count"], 1)
        self.assertEqual(pstats["today"]["loss_count"], 1)

    def test_04_only_losses_must_be_strictly_negative(self):
        """TEST 4: 손실 거래만 10건 존재 -> Dashboard PnL은 반드시 음수"""
        today = datetime.now().strftime("%Y-%m-%d")
        with sqlite3.connect(self.op_db) as conn:
            for i in range(10):
                sym = f"0000{i:02d}"
                conn.execute(f"""
                    INSERT INTO positions (pos_id, iem_cd, name, entry_price, qty, status, entry_time, exit_time, exit_price, pnl, trading_mode, account_no)
                    VALUES ('POS_L_{i}', '{sym}', '종목{i}', 10000, 0, 'CLOSED', '{today}T09:00:00', '{today}T10:00:00', 9000, -10000, 'live', '20201549311')
                """)
                conn.execute(f"""
                    INSERT INTO fills (fill_id, client_order_id, iem_cd, side, filled_qty, filled_price, timestamp, trading_mode, account_no)
                    VALUES ('FILL_L_{i}', 'ORD_L_{i}', '{sym}', 'SELL', 10, 9000, '{today}T10:00:01', 'live', '20201549311')
                """)

        summary = self.tam.get_trade_summary(trading_mode='live', account_no='20201549311', date_str=today)
        pstats = self.tam.get_period_statistics(trading_mode='live', account_no='20201549311')

        self.assertEqual(summary["completed_trades_count"], 10)
        self.assertEqual(summary["realized_pnl"], -100000)
        self.assertLess(summary["realized_pnl"], 0)
        self.assertEqual(pstats["today"]["total_pnl"], -100000)
        self.assertLess(pstats["today"]["total_pnl"], 0)
        self.assertEqual(pstats["today"]["win_rate"], 0.0)

    def test_05_zero_wins_and_only_losses_never_shows_positive(self):
        """TEST 5: 수익 거래가 0건이고 손실 거래만 존재 -> Dashboard에 +수익이 표시되면 TEST FAIL"""
        today = datetime.now().strftime("%Y-%m-%d")
        with sqlite3.connect(self.op_db) as conn:
            conn.execute(f"""
                INSERT INTO positions (pos_id, iem_cd, name, entry_price, qty, status, entry_time, exit_time, exit_price, pnl, trading_mode, account_no)
                VALUES ('POS_ONLY_LOSS', '005930', '삼성전자', 80000, 0, 'CLOSED', '{today}T09:00:00', '{today}T10:00:00', 78000, -20000, 'live', '20201549311')
            """)
            conn.execute(f"""
                INSERT INTO fills (fill_id, client_order_id, iem_cd, side, filled_qty, filled_price, timestamp, trading_mode, account_no)
                VALUES ('FILL_ONLY_LOSS', 'ORD_1', '005930', 'SELL', 10, 78000, '{today}T10:00:01', 'live', '20201549311')
            """)

        pstats = self.tam.get_period_statistics(trading_mode='live', account_no='20201549311')
        self.assertFalse(pstats["today"]["total_pnl"] > 0, "TEST FAIL: 손실 거래만 있는데 +수익이 표시됨!")
        self.assertLess(pstats["today"]["total_pnl"], 0)

    def test_06_mock_and_live_strictly_isolated(self):
        """TEST 6: MOCK + LIVE 거래가 동시에 DB에 존재 -> LIVE Dashboard에는 LIVE만 반영"""
        today = datetime.now().strftime("%Y-%m-%d")
        with sqlite3.connect(self.op_db) as conn:
            # LIVE loss: -50,000
            conn.execute(f"""
                INSERT INTO positions (pos_id, iem_cd, name, entry_price, qty, status, entry_time, exit_time, exit_price, pnl, trading_mode, account_no)
                VALUES ('POS_LIVE', '005930', '삼성전자', 70000, 0, 'CLOSED', '{today}T09:00:00', '{today}T10:00:00', 65000, -50000, 'live', '20201549311')
            """)
            conn.execute(f"""
                INSERT INTO fills (fill_id, client_order_id, iem_cd, side, filled_qty, filled_price, timestamp, trading_mode, account_no)
                VALUES ('FILL_LIVE', 'ORD_LIVE', '005930', 'SELL', 10, 65000, '{today}T10:00:01', 'live', '20201549311')
            """)

            # MOCK huge profit: +5,000,000
            conn.execute(f"""
                INSERT INTO positions (pos_id, iem_cd, name, entry_price, qty, status, entry_time, exit_time, exit_price, pnl, trading_mode, account_no)
                VALUES ('POS_MOCK', '000660', 'SK하이닉스', 100000, 0, 'CLOSED', '{today}T09:00:00', '{today}T10:00:00', 150000, 5000000, 'mock', '50001003032')
            """)
            conn.execute(f"""
                INSERT INTO fills (fill_id, client_order_id, iem_cd, side, filled_qty, filled_price, timestamp, trading_mode, account_no)
                VALUES ('FILL_MOCK', 'ORD_MOCK', '000660', 'SELL', 100, 150000, '{today}T10:00:01', 'mock', '50001003032')
            """)

        summary_live = self.tam.get_trade_summary(trading_mode='live', account_no='20201549311', date_str=today)
        pstats_live = self.tam.get_period_statistics(trading_mode='live', account_no='20201549311')

        # LIVE must only reflect -50,000 and ignore MOCK's +5,000,000
        self.assertEqual(summary_live["completed_trades_count"], 1)
        self.assertEqual(summary_live["realized_pnl"], -50000)
        self.assertEqual(pstats_live["today"]["total_pnl"], -50000)

    def test_07_recovery_and_test_orders_governance(self):
        """TEST 7: RESTART_RECOVERY 실거래는 손익에 반드시 반영하되, 야간 오프라인 테스트 레코드는 배제"""
        today = datetime.now().strftime("%Y-%m-%d")
        with sqlite3.connect(self.op_db) as conn:
            # 장중 실제 RESTART_RECOVERY 체결 (09:04 체결, 손실 -23,800) -> 반영되어야 함!
            conn.execute(f"""
                INSERT INTO positions (pos_id, iem_cd, name, entry_price, qty, status, entry_time, exit_time, exit_price, pnl, trading_mode, account_no)
                VALUES ('POS_REC', '490470', '세미파이브', 15000, 0, 'CLOSED', '{today}T09:00:00', '{today}T09:04:55', 14300, -23800, 'live', '20201549311')
            """)
            conn.execute(f"""
                INSERT INTO fills (fill_id, client_order_id, iem_cd, side, filled_qty, filled_price, timestamp, trading_mode, account_no)
                VALUES ('FILL_REC', 'RESTART_RECOVERY_CLOSE_490470', '490470', 'SELL', 34, 14300, '{today}T09:04:55', 'live', '20201549311')
            """)

        with sqlite3.connect(self.trade_db) as conn:
            # 야간 심야(23:33) 오프라인 가상 테스트 레코드 -> 장외 시간이므로 배제되어야 함!
            conn.execute(f"""
                INSERT INTO trades (trade_id, symbol, symbol_name, setup_name, time_horizon, side, entry_time, exit_time, entry_price, exit_price, shares, pnl, return_pct, trading_mode, account_no)
                VALUES ('T_490470_NIGHT', '490470', '세미파이브', 'RESTART_RECOVERY', 'INTRADAY', 'BUY', '{today} 23:33:00', '{today} 23:33:12', 15000, 14830, 34, -5780, -1.13, 'live', '20201549311')
            """)

        summary = self.tam.get_trade_summary(trading_mode='live', account_no='20201549311', date_str=today)
        self.assertEqual(summary["completed_trades_count"], 1)
        self.assertEqual(summary["realized_pnl"], -23800)  # 장중 실제 체결분만 정확히 반영

    def test_08_cross_db_dedup_never_deletes_different_loss_trades(self):
        """TEST 8: cross DB dedup 수행 -> 동일 거래만 제거하고 서로 다른 손실 거래는 절대 제거하지 않음"""
        today = datetime.now().strftime("%Y-%m-%d")
        with sqlite3.connect(self.op_db) as conn:
            # Trade A in op_db: 475040 (-12,255)
            conn.execute(f"""
                INSERT INTO positions (pos_id, iem_cd, name, entry_price, qty, status, entry_time, exit_time, exit_price, pnl, trading_mode, account_no)
                VALUES ('POS_475040', '475040', '스트라드비젼', 3765, 0, 'CLOSED', '{today}T10:15:00', '{today}T13:50:26', 3480, -12255, 'live', '20201549311')
            """)
            conn.execute(f"""
                INSERT INTO fills (fill_id, client_order_id, iem_cd, side, filled_qty, filled_price, timestamp, trading_mode, account_no)
                VALUES ('FILL_475040', 'ORD_475040', '475040', 'SELL', 43, 3480, '{today}T13:50:26', 'live', '20201549311')
            """)

            # Trade B in op_db: 475580 (-840) - DISTINCT LOSS TRADE!
            conn.execute(f"""
                INSERT INTO positions (pos_id, iem_cd, name, entry_price, qty, status, entry_time, exit_time, exit_price, pnl, trading_mode, account_no)
                VALUES ('POS_475580', '475580', '에이럭스', 7900, 0, 'CLOSED', '{today}T10:22:00', '{today}T13:50:31', 7690, -840, 'live', '20201549311')
            """)
            conn.execute(f"""
                INSERT INTO fills (fill_id, client_order_id, iem_cd, side, filled_qty, filled_price, timestamp, trading_mode, account_no)
                VALUES ('FILL_475580', 'ORD_475580', '475580', 'SELL', 4, 7690, '{today}T13:50:31', 'live', '20201549311')
            """)

        with sqlite3.connect(self.trade_db) as conn:
            # Duplicate record of 475040 in trade_db (same symbol, same exit date)
            conn.execute(f"""
                INSERT INTO trades (trade_id, symbol, symbol_name, setup_name, time_horizon, side, entry_time, exit_time, entry_price, exit_price, shares, pnl, return_pct, trading_mode, account_no)
                VALUES ('T_475040_DUP', '475040', '스트라드비젼', 'INT_BREAKOUT', 'INTRADAY', 'BUY', '{today} 10:15:00', '{today} 13:50:26', 3765, 3480, 43, -12255, -7.57, 'live', '20201549311')
            """)
            # Historical trade on 2026-09-08 (342870: -1,495) - DISTINCT HISTORICAL LOSS!
            conn.execute(f"""
                INSERT INTO trades (trade_id, symbol, symbol_name, setup_name, time_horizon, side, entry_time, exit_time, entry_price, exit_price, shares, pnl, return_pct, trading_mode, account_no)
                VALUES ('T_342870_HIST', '342870', '한선엔지니어링', 'SWG_TREND', 'SWING', 'BUY', '2026-09-08 10:17:13', '2026-09-08 11:41:44', 3715, 3600, 13, -1495, -3.10, 'live', '20201549311')
            """)

        completed = self.tam.get_completed_trades(trading_mode='live', account_no='20201549311')
        symbols = [t["symbol"] for t in completed]

        # Must have exactly 3 trades: 475040 (deduped once), 475580 (kept!), 342870 (kept!)
        self.assertEqual(len(completed), 3)
        self.assertIn("475040", symbols)
        self.assertIn("475580", symbols)
        self.assertIn("342870", symbols)
        self.assertEqual(symbols.count("475040"), 1)  # Duplicate removed!

        tot_pnl = sum(t["net_pnl"] for t in completed)
        self.assertEqual(tot_pnl, -12255 - 840 - 1495)


if __name__ == "__main__":
    unittest.main()
