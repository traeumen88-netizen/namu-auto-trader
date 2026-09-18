# -*- coding: utf-8 -*-
"""[FINAL MASTER v16.0] Unit and Concurrency Tests for Deadlock Transaction Manager
Section 13-20: Deadlock Verification Tests (TEST 1 ~ TEST 7)
Section 13-21: Final Invariants (Idempotency & Consistent Lock Ordering)
"""

import os
import time
import shutil
import sqlite3
import unittest
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
from unittest.mock import patch, MagicMock

from execution.deadlock_transaction import (
    DeadlockSafeTransactionManager,
    ExecutionEvent,
    ExecutionResult,
    DeadlockError,
    LockTimeoutError
)


class TestDeadlockTransaction(unittest.TestCase):
    def setUp(self):
        self.test_dir = "data/test_deadlock"
        os.makedirs(self.test_dir, exist_ok=True)
        self.db_path = os.path.join(self.test_dir, "test_operational.db")
        if os.path.exists(self.db_path):
            try:
                os.remove(self.db_path)
            except Exception:
                pass
        self.tx_mgr = DeadlockSafeTransactionManager(
            db_path=self.db_path,
            base_delay=0.01,
            max_delay=0.1,
            max_retries=5,
            busy_timeout_sec=2.0
        )

    def tearDown(self):
        # Give connections time to close
        time.sleep(0.05)
        if os.path.exists(self.test_dir):
            try:
                shutil.rmtree(self.test_dir, ignore_errors=True)
            except Exception:
                pass

    # =========================================================================
    # TEST 1: WS + REST 동시 체결 처리 (다른 주문)
    # WebSocket Handler와 REST Polling Worker가 서로 다른 주문에 대해 동시 트랜잭션 실행
    # 한쪽이 Lock 충돌(Deadlock/busy) 발생 시 Retry 수행 -> 최종 둘 다 COMMIT 성공
    # =========================================================================
    def test_01_ws_rest_concurrent_different_orders(self):
        event_ws = ExecutionEvent(
            execution_id="EXEC_WS_001",
            client_order_id="ORD_WS_001",
            iem_cd="005930",
            side="BUY",
            filled_qty=10,
            filled_price=70000.0,
            broker_order_no="B_WS_001"
        )
        event_rest = ExecutionEvent(
            execution_id="EXEC_REST_002",
            client_order_id="ORD_REST_002",
            iem_cd="000660",
            side="BUY",
            filled_qty=5,
            filled_price=150000.0,
            broker_order_no="B_REST_002"
        )

        results = []
        def run_ws():
            res = self.tx_mgr.apply_execution_update(event_ws)
            results.append(("WS", res))

        def run_rest():
            res = self.tx_mgr.apply_execution_update(event_rest)
            results.append(("REST", res))

        t1 = threading.Thread(target=run_ws)
        t2 = threading.Thread(target=run_rest)
        t1.start()
        t2.start()
        t1.join(timeout=5.0)
        t2.join(timeout=5.0)

        self.assertEqual(len(results), 2)
        for source, res in results:
            self.assertEqual(res.status, "SUCCESS", f"{source} failed with {res.message}")

        # Check DB State
        conn = sqlite3.connect(self.db_path)
        cur = conn.cursor()
        orders = cur.execute("SELECT client_order_id, status FROM orders ORDER BY client_order_id").fetchall()
        self.assertEqual(len(orders), 2)
        self.assertEqual(orders[0][0], "ORD_REST_002")
        self.assertEqual(orders[1][0], "ORD_WS_001")

        fills = cur.execute("SELECT fill_id FROM fills ORDER BY fill_id").fetchall()
        self.assertEqual(len(fills), 2)

        positions = cur.execute("SELECT iem_cd, qty, status FROM positions ORDER BY iem_cd").fetchall()
        self.assertEqual(len(positions), 2)
        self.assertEqual(positions[0][0], "000660")
        self.assertEqual(positions[0][1], 5)
        self.assertEqual(positions[1][0], "005930")
        self.assertEqual(positions[1][1], 10)
        conn.close()

    # =========================================================================
    # TEST 2: 동일 Position에 대한 동시 체결 UPDATE
    # 동일 포지션에 대해 2개 스레드가 동시 체결 반영 시도
    # Lock 충돌 발생 시 Retry 후 최종 정상 반영 (수량 정확히 갱신)
    # =========================================================================
    def test_02_concurrent_update_same_position(self):
        # 1. 초기 포지션 생성 (100주 @ 50,000원)
        init_event = ExecutionEvent(
            execution_id="EXEC_INIT_001",
            client_order_id="ORD_INIT_001",
            iem_cd="005930",
            side="BUY",
            filled_qty=100,
            filled_price=50000.0
        )
        res_init = self.tx_mgr.apply_execution_update(init_event)
        self.assertEqual(res_init.status, "SUCCESS")

        # 2. 2개 스레드에서 동일 포지션에 대해 동시 매수 추가 체결
        event_a = ExecutionEvent(
            execution_id="EXEC_ADD_A",
            client_order_id="ORD_ADD_A",
            iem_cd="005930",
            side="BUY",
            filled_qty=30,
            filled_price=51000.0
        )
        event_b = ExecutionEvent(
            execution_id="EXEC_ADD_B",
            client_order_id="ORD_ADD_B",
            iem_cd="005930",
            side="BUY",
            filled_qty=20,
            filled_price=52000.0
        )

        results = []
        with ThreadPoolExecutor(max_workers=2) as executor:
            futures = [
                executor.submit(self.tx_mgr.apply_execution_update, event_a),
                executor.submit(self.tx_mgr.apply_execution_update, event_b)
            ]
            for f in as_completed(futures):
                results.append(f.result())

        for res in results:
            self.assertEqual(res.status, "SUCCESS")

        # DB 검증: 총 수량 100 + 30 + 20 = 150주, 가중 평균 매수가 검증
        conn = sqlite3.connect(self.db_path)
        cur = conn.cursor()
        pos = cur.execute("SELECT qty, entry_price, status FROM positions WHERE iem_cd = '005930'").fetchone()
        self.assertIsNotNone(pos)
        self.assertEqual(pos[0], 150)
        expected_avg_price = (100 * 50000.0 + 30 * 51000.0 + 20 * 52000.0) / 150.0
        self.assertAlmostEqual(pos[1], expected_avg_price, places=2)
        self.assertEqual(pos[2], "OPEN")

        fills = cur.execute("SELECT count(*) FROM fills WHERE iem_cd = '005930'").fetchone()[0]
        self.assertEqual(fills, 3)
        conn.close()

    # =========================================================================
    # TEST 3: Retry 중 동일 execution_id 재처리 방지 (Idempotency)
    # 이미 존재하는 execution_id 처리 시 중복 Fill 생성 방지 (Fill 1개, 포지션 1회만 반영)
    # =========================================================================
    def test_03_idempotency_duplicate_execution_id(self):
        event = ExecutionEvent(
            execution_id="EXEC_DUP_001",
            client_order_id="ORD_DUP_001",
            iem_cd="005930",
            side="BUY",
            filled_qty=50,
            filled_price=70000.0
        )

        # 1st attempt: Must succeed
        res1 = self.tx_mgr.apply_execution_update(event)
        self.assertEqual(res1.status, "SUCCESS")

        # 2nd attempt: Must be recognized as duplicate and ignored idempotently
        res2 = self.tx_mgr.apply_execution_update(event)
        self.assertEqual(res2.status, "DUPLICATE_EXECUTION_IGNORED")
        self.assertIn("IDEMPOTENT_IGNORE", res2.message)

        # DB 검증: fills 테이블에는 정확히 1개만 존재, position 수량은 50 (100이 아님)
        conn = sqlite3.connect(self.db_path)
        cur = conn.cursor()
        fill_count = cur.execute("SELECT count(*) FROM fills WHERE fill_id = 'EXEC_DUP_001'").fetchone()[0]
        self.assertEqual(fill_count, 1)

        pos_qty = cur.execute("SELECT qty FROM positions WHERE iem_cd = '005930'").fetchone()[0]
        self.assertEqual(pos_qty, 50)
        conn.close()

    # =========================================================================
    # TEST 4: 5회 연속 Deadlock 시 Safe Fallback
    # 5회 연속 Lock 오류 발생 시 MAX_RETRIES 초과 -> EXECUTION_UPDATE_FAILED &
    # Order status RECONCILIATION_REQUIRED & [DB_RETRY_EXHAUSTED] 로깅
    # =========================================================================
    def test_04_max_retries_exhausted_fallback(self):
        event = ExecutionEvent(
            execution_id="EXEC_EXHAUST_001",
            client_order_id="ORD_EXHAUST_001",
            iem_cd="005930",
            side="BUY",
            filled_qty=10,
            filled_price=70000.0
        )

        # Pre-create order in DB
        conn = sqlite3.connect(self.db_path)
        conn.execute("INSERT INTO orders (client_order_id, iem_cd, side, qty, price, status) VALUES (?, ?, ?, ?, ?, ?)",
                     (event.client_order_id, event.iem_cd, event.side, event.filled_qty, event.filled_price, "PENDING"))
        conn.commit()
        conn.close()

        # Mock _apply_execution_update_once to consistently raise DeadlockError
        with patch.object(self.tx_mgr, "_apply_execution_update_once", side_effect=DeadlockError("Simulated persistent deadlock")):
            res = self.tx_mgr.apply_execution_update(event)

        self.assertEqual(res.status, "RECONCILIATION_REQUIRED")
        self.assertEqual(res.retries_attempted, self.tx_mgr.max_retries)
        self.assertEqual(self.tx_mgr.metrics.db_deadlock_retry_exhausted, 1)

        # DB 검증: 주문 상태가 RECONCILIATION_REQUIRED 로 전이되었고 reconciliation_logs 기록됨
        conn = sqlite3.connect(self.db_path)
        cur = conn.cursor()
        order_status = cur.execute("SELECT status FROM orders WHERE client_order_id = ?", (event.client_order_id,)).fetchone()[0]
        self.assertEqual(order_status, "RECONCILIATION_REQUIRED")

        recon_log = cur.execute("SELECT details, action_taken FROM reconciliation_logs WHERE details LIKE '%EXEC_EXHAUST_001%'").fetchone()
        self.assertIsNotNone(recon_log)
        self.assertEqual(recon_log[1], "ORDER_MARKED_RECONCILIATION_REQUIRED")
        conn.close()

    # =========================================================================
    # TEST 5: Unique Constraint 위반 또는 비-Deadlock 에러 시 즉시 중단 (No Retry)
    # sqlite3.IntegrityError 또는 비-락 에러 발생 시 즉시 중단하여 무의미한 5회 retry 방지
    # =========================================================================
    def test_05_non_retryable_error_fails_immediately(self):
        event = ExecutionEvent(
            execution_id="EXEC_FAIL_001",
            client_order_id="ORD_FAIL_001",
            iem_cd="005930",
            side="BUY",
            filled_qty=10,
            filled_price=70000.0
        )

        # Mock _apply_execution_update_once to raise non-recoverable ValueError
        with patch.object(self.tx_mgr, "_apply_execution_update_once", side_effect=ValueError("Invalid data format")):
            res = self.tx_mgr.apply_execution_update(event)

        # Should fail immediately on attempt 0 without retrying
        self.assertEqual(res.status, "FAILED")
        self.assertEqual(res.retries_attempted, 0)
        self.assertEqual(self.tx_mgr.metrics.db_deadlock_retry_count, 0)

        # Check fatal IntegrityError (e.g. CHECK constraint) also fails fast
        with patch.object(self.tx_mgr, "_apply_execution_update_once", side_effect=sqlite3.IntegrityError("CHECK constraint failed: qty > 0")):
            res2 = self.tx_mgr.apply_execution_update(event)

        self.assertEqual(res2.status, "FAILED")
        self.assertEqual(res2.retries_attempted, 0)

    # =========================================================================
    # TEST 6: DB 연결/락 일시 장애 시 Limited retry 후 성공 복구
    # 2회 락 오류 후 3회차에 성공 -> retry count 2회, 최종 SUCCESS 및 retry_success 증가
    # =========================================================================
    def test_06_transient_lock_retry_and_recovery(self):
        event = ExecutionEvent(
            execution_id="EXEC_TRANSIENT_001",
            client_order_id="ORD_TRANSIENT_001",
            iem_cd="005930",
            side="BUY",
            filled_qty=25,
            filled_price=69000.0
        )

        real_apply = self.tx_mgr._apply_execution_update_once
        attempts = 0

        def flaky_apply(conn, ev):
            nonlocal attempts
            attempts += 1
            if attempts <= 2:
                raise sqlite3.OperationalError("database is locked")
            return real_apply(conn, ev)

        with patch.object(self.tx_mgr, "_apply_execution_update_once", side_effect=flaky_apply):
            res = self.tx_mgr.apply_execution_update(event)

        self.assertEqual(res.status, "SUCCESS")
        self.assertEqual(res.retries_attempted, 2)
        self.assertEqual(self.tx_mgr.metrics.db_deadlock_retry_count, 2)
        self.assertEqual(self.tx_mgr.metrics.db_deadlock_retry_success, 1)

        # Verify DB updated
        conn = sqlite3.connect(self.db_path)
        cur = conn.cursor()
        fill = cur.execute("SELECT filled_qty FROM fills WHERE fill_id = 'EXEC_TRANSIENT_001'").fetchone()
        self.assertIsNotNone(fill)
        self.assertEqual(fill[0], 25)
        conn.close()

    # =========================================================================
    # TEST 7: 실전 시나리오 검증 (469610 SCALE_OUT + TRAILING_STOP 동시 발생)
    # 469610 종목의 분할 매도와 트레일링 스탑 동시 발생 상황에서
    # Deadlock 방어 레이어가 정상 작동하여 포지션과 체결 내역이 정합성을 유지하는지 검증
    # =========================================================================
    def test_07_real_world_scenario_469610_scale_out_and_trailing_stop(self):
        # 1. 469610 초기 포지션 진입 (100주 @ 50,000원)
        entry_event = ExecutionEvent(
            execution_id="EXEC_469610_ENTRY",
            client_order_id="ORD_469610_BUY",
            iem_cd="469610",
            side="BUY",
            filled_qty=100,
            filled_price=50000.0
        )
        res_entry = self.tx_mgr.apply_execution_update(entry_event)
        self.assertEqual(res_entry.status, "SUCCESS")

        # 2. SCALE_OUT 50주 @ 53,000원 매도 + TRAILING_STOP 50주 @ 52,000원 매도 동시 발주
        scale_out_event = ExecutionEvent(
            execution_id="EXEC_469610_SCALE_OUT",
            client_order_id="ORD_469610_SCALE",
            iem_cd="469610",
            side="SELL",
            filled_qty=50,
            filled_price=53000.0,
            broker_order_no="B_SCALE_01"
        )
        trailing_stop_event = ExecutionEvent(
            execution_id="EXEC_469610_TRAILING_STOP",
            client_order_id="ORD_469610_TSTOP",
            iem_cd="469610",
            side="SELL",
            filled_qty=50,
            filled_price=52000.0,
            broker_order_no="B_TSTOP_02"
        )

        results = []
        with ThreadPoolExecutor(max_workers=2) as executor:
            f1 = executor.submit(self.tx_mgr.apply_execution_update, scale_out_event)
            f2 = executor.submit(self.tx_mgr.apply_execution_update, trailing_stop_event)
            for f in as_completed([f1, f2]):
                results.append(f.result())

        # 둘 다 정상 체결 완료 확인
        for res in results:
            self.assertEqual(res.status, "SUCCESS")

        # 3. 최종 정합성 검증
        conn = sqlite3.connect(self.db_path)
        cur = conn.cursor()

        # 포지션 잔여 수량 0주, CLOSED 상태
        pos = cur.execute("SELECT qty, status, pnl, exit_price FROM positions WHERE iem_cd = '469610'").fetchone()
        self.assertIsNotNone(pos)
        self.assertEqual(pos[0], 0, "Position quantity must be 0 after full exit")
        self.assertEqual(pos[1], "CLOSED", "Position status must be CLOSED")

        # 실현 손익 (PnL):
        # 50주 * (53,000 - 50,000) = 150,000원
        # 50주 * (52,000 - 50,000) = 100,000원
        # 총 PnL = 250,000원
        expected_pnl = (53000.0 - 50000.0) * 50 + (52000.0 - 50000.0) * 50
        self.assertAlmostEqual(pos[2], expected_pnl, places=2)

        # 체결 내역 (fills) 2건 정확히 기록
        fills = cur.execute("SELECT fill_id, filled_qty, filled_price FROM fills WHERE iem_cd = '469610' AND side = 'SELL' ORDER BY fill_id").fetchall()
        self.assertEqual(len(fills), 2)
        total_sold_qty = sum(f[1] for f in fills)
        self.assertEqual(total_sold_qty, 100)

        # 주문 (orders) 2건 모두 FILLED
        orders = cur.execute("SELECT client_order_id, status FROM orders WHERE iem_cd = '469610' AND side = 'SELL'").fetchall()
        self.assertEqual(len(orders), 2)
        for ord_row in orders:
            self.assertEqual(ord_row[1], "FILLED")

        conn.close()


if __name__ == "__main__":
    unittest.main()
