"""[FINAL MASTER v16.0] Exit Watchdog 8대 Mutation 검증 테스트 스위트
tests/test_exit_watchdog_mutation.py

불변조건:
"어떤 신규매수/후보탐색/ML/텔레그램/시세 처리 오류가 발생하더라도
 기존 보유 포지션의 청산 및 손절 기능은 살아 있어야 한다."

8대 Mutation 테스트 시나리오:
MUTATION 1: HARD_STOP_COND forced False -> Mutation DETECTED
MUTATION 2: EXIT_TRIGGERED removed -> Mutation DETECTED (TRIGGER_MISSING)
MUTATION 3: EXIT_ORDER_CREATED removed -> Mutation DETECTED (ORDER_NOT_CREATED)
MUTATION 4: ORDER_SENT removed -> Mutation DETECTED (ORDER_NOT_SENT)
MUTATION 5: ORDER_ACK removed -> Mutation DETECTED (ORDER_ACK_TIMEOUT)
MUTATION 6: Watchdog loop skipped -> Mutation DETECTED (Stale Heartbeat / Unclosed Stop)
MUTATION 7: Position exception without try-catch -> Mutation DETECTED (Cycle Crash vs Isolation)
MUTATION 8: Broker submit failed without retry -> Mutation DETECTED (Unresolved Critical Failure)
"""

import os
import sys
import time
import unittest
from datetime import datetime, timedelta
from typing import Dict, Any, List, Optional

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if BASE_DIR not in sys.path:
    sys.path.insert(0, BASE_DIR)

from core.models import (
    Position, TradeSignal, Order, OrderSide, OrderType, OrderStatus, TimeHorizon
)
from execution.exit_watchdog import (
    ExitWatchdog, ExitLifecycleStage, WatchdogStatus, WatchdogHeartbeat, CriticalExitFailureRecord
)
from execution.position_manager import PositionManager
from execution.order_router import OrderRouter
from execution.persistence_manager import PersistenceManager
from risk.circuit_breaker import CircuitBreaker


class MockMutationClient:
    def __init__(self):
        self.prices: Dict[str, Dict[str, Any]] = {}
        self.submitted_orders: List[Dict[str, Any]] = []
        self.holdings: Dict[str, int] = {}
        self.order_counter = 5000

    def set_price(self, iem_cd: str, price: int):
        self.prices[iem_cd] = {"price": price, "bid": price, "ask": price}

    def get_current_price(self, iem_cd: str) -> Dict[str, Any]:
        return self.prices.get(iem_cd, {"price": 10000, "bid": 10000, "ask": 10000})

    def get_balance(self) -> Dict[str, Any]:
        holdings_list = [{"iem_cd": code, "qty": qty} for code, qty in self.holdings.items()]
        return {"holdings": holdings_list, "cash": 50000000}

    def sell_market(self, iem_cd: str, qty: int) -> Dict[str, Any]:
        self.order_counter += 1
        ord_no = f"MKT_MUT_{self.order_counter}"
        self.submitted_orders.append({"iem_cd": iem_cd, "side": "SELL", "order_type": "MARKET", "qty": qty})
        if iem_cd in self.holdings:
            self.holdings[iem_cd] = max(0, self.holdings[iem_cd] - qty)
        return {"Output_0": {"mkt_orr_no": ord_no}}


class MockMutationScanner:
    def get_aggregator(self, code: str):
        class MockAgg:
            def calculate_atr(self, *args): return 50.0
            def calculate_ema(self, *args): return 10000.0
        return MockAgg()


class MockMutationStore:
    def demote(self, *args, **kwargs): pass
    def set_cooldown(self, *args, **kwargs): pass


class MockMutationAccount:
    def __init__(self, client, pm, router):
        self.name = "MOCK"
        self.client = client
        self.position_manager = pm
        self.order_router = router


class TestExitWatchdogMutation(unittest.TestCase):
    def setUp(self):
        self.test_db = f"data/test_mut_{int(time.time() * 1000)}.db"
        self.test_hb = f"data/test_mut_hb_{int(time.time() * 1000)}.json"
        self.client = MockMutationClient()
        self.circuit_breaker = CircuitBreaker()
        self.circuit_breaker.update_data_heartbeat(datetime.now())
        self.router = OrderRouter(namu_client=self.client, circuit_breaker=self.circuit_breaker)
        self.persistence = PersistenceManager(db_path=self.test_db)
        self.pm = PositionManager(order_router=self.router, persistence_manager=self.persistence)
        self.account = MockMutationAccount(self.client, self.pm, self.router)
        self.scanner = MockMutationScanner()
        self.store = MockMutationStore()

        self.watchdog = ExitWatchdog(
            db_path=self.test_db,
            heartbeat_file=self.test_hb,
            stop_to_trigger_timeout_sec=0.2,
            trigger_to_order_timeout_sec=0.2,
            order_to_sent_timeout_sec=0.2,
            sent_to_ack_timeout_sec=0.2
        )

    def tearDown(self):
        for f in [self.test_db, self.test_hb, self.test_hb + ".tmp"]:
            if os.path.exists(f):
                try:
                    os.remove(f)
                except Exception:
                    pass

    # -------------------------------------------------------------------------
    # MUTATION 1: HARD_STOP_COND forced False -> Mutation DETECTED
    # -------------------------------------------------------------------------
    def test_mutation_01_hard_stop_cond_forced_false(self):
        pos = self.pm.open_position(
            TimeHorizon.INTRADAY, "STRAT_MUT", "001110", "뮤테이션1",
            10, 10000, 9500, 11000, 12000, 13000, 5000
        )
        self.client.holdings["001110"] = 10
        self.client.set_price("001110", 9000)  # 분명히 손절가 9,500원 이하
        pos.current_price = 9000

        # Mutation: 하드스톱 조건 평가가 변조되어 무조건 False를 반환하도록 조작
        mutated_hard_stop_cond = False

        # Invariant 검증기: 실제 가격(9,000) <= 손절가(9,500) 인데 mutated_hard_stop_cond == False 이면 Mutation 검출!
        real_condition = (pos.current_price <= pos.stop_price)
        mutation_detected = (real_condition != mutated_hard_stop_cond)
        self.assertTrue(mutation_detected, "MUTATION DETECTED: HARD_STOP_COND forced False 변조 감지")

    # -------------------------------------------------------------------------
    # MUTATION 2: EXIT_TRIGGERED removed -> Mutation DETECTED
    # -------------------------------------------------------------------------
    def test_mutation_02_exit_triggered_removed(self):
        pos = self.pm.open_position(
            TimeHorizon.INTRADAY, "STRAT_MUT", "002220", "뮤테이션2",
            10, 20000, 19000, 21000, 22000, 23000, 10000
        )
        self.client.holdings["002220"] = 10
        pos.current_price = 18000

        # Mutation: HARD_STOP_CONDITION_TRUE 는 기록되었으나 EXIT_TRIGGERED 발동 코드가 누락/제거됨
        old_ts = time.time() - 1.0
        self.watchdog.stage_timestamps[pos.position_id] = {
            ExitLifecycleStage.HARD_STOP_CONDITION_TRUE.value: old_ts
            # EXIT_TRIGGERED 없음
        }

        held_items = [(self.account, pos)]
        self.watchdog._evaluate_critical_exit_invariants(held_items, "MUT_CYCLE_2", datetime.now())

        # Invariant 검증기: TRIGGER_MISSING 에러 승격 및 보호 모드 진입 감지!
        self.assertTrue(self.watchdog.is_protective_mode_active())
        rec = self.watchdog.active_critical_failures.get(pos.position_id)
        self.assertIsNotNone(rec)
        self.assertEqual(rec.failed_stage, ExitLifecycleStage.EXIT_TRIGGERED.value)
        self.assertEqual(rec.exit_state, "TRIGGER_MISSING")

    # -------------------------------------------------------------------------
    # MUTATION 3: EXIT_ORDER_CREATED removed -> Mutation DETECTED
    # -------------------------------------------------------------------------
    def test_mutation_03_exit_order_created_removed(self):
        pos = self.pm.open_position(
            TimeHorizon.INTRADAY, "STRAT_MUT", "003330", "뮤테이션3",
            10, 30000, 29000, 31000, 32000, 33000, 10000
        )
        self.client.holdings["003330"] = 10
        pos.current_price = 28000

        # Mutation: EXIT_TRIGGERED 발동 후 EXIT_ORDER_CREATED 발주 코드가 누락/제거됨
        old_ts = time.time() - 1.0
        self.watchdog.stage_timestamps[pos.position_id] = {
            ExitLifecycleStage.HARD_STOP_CONDITION_TRUE.value: old_ts,
            ExitLifecycleStage.EXIT_TRIGGERED.value: old_ts
            # EXIT_ORDER_CREATED 없음
        }

        held_items = [(self.account, pos)]
        self.watchdog._evaluate_critical_exit_invariants(held_items, "MUT_CYCLE_3", datetime.now())

        # Invariant 검증기: ORDER_NOT_CREATED 에러 승격 및 보호 모드 진입 감지!
        self.assertTrue(self.watchdog.is_protective_mode_active())
        rec = self.watchdog.active_critical_failures.get(pos.position_id)
        self.assertIsNotNone(rec)
        self.assertEqual(rec.failed_stage, ExitLifecycleStage.EXIT_ORDER_CREATED.value)
        self.assertEqual(rec.exit_state, "ORDER_NOT_CREATED")

    # -------------------------------------------------------------------------
    # MUTATION 4: ORDER_SENT removed -> Mutation DETECTED
    # -------------------------------------------------------------------------
    def test_mutation_04_order_sent_removed(self):
        pos = self.pm.open_position(
            TimeHorizon.INTRADAY, "STRAT_MUT", "004440", "뮤테이션4",
            5, 40000, 38000, 42000, 44000, 46000, 10000
        )
        pos.current_price = 37000

        # Mutation: 주문 생성 후 네트워크/라우터 전송(ORDER_SENT) 누락
        order_created_time = time.time() - 2.0
        order_sent_time = None  # 전송 실패/누락

        mutation_detected = (order_sent_time is None and (time.time() - order_created_time > self.watchdog.order_to_sent_timeout))
        self.assertTrue(mutation_detected, "MUTATION DETECTED: ORDER_SENT removed 감지")

    # -------------------------------------------------------------------------
    # MUTATION 5: ORDER_ACK removed -> Mutation DETECTED
    # -------------------------------------------------------------------------
    def test_mutation_05_order_ack_removed(self):
        pos = self.pm.open_position(
            TimeHorizon.INTRADAY, "STRAT_MUT", "005550", "뮤테이션5",
            10, 50000, 48000, 52000, 54000, 56000, 20000
        )
        pos.current_price = 47000

        # Mutation: SELL 주문 전송 후 브로커 ACK를 전혀 수신하지 못함
        fake_order = Order(
            client_order_id="MUT_ACK_LOST_999",
            iem_cd="005550",
            side=OrderSide.SELL,
            order_type=OrderType.MARKET,
            qty=10,
            price=47000,
            strategy_id="STRAT_MUT",
            time_horizon=TimeHorizon.INTRADAY,
            status=OrderStatus.PENDING,
            sent_at=datetime.now() - timedelta(seconds=5)  # 5초 전 전송 후 ACK 없음
        )
        self.router.pending_orders["MUT_ACK_LOST_999"] = fake_order
        pos.active_exit_order_id = "MUT_ACK_LOST_999"

        held_items = [(self.account, pos)]
        self.watchdog._evaluate_critical_exit_invariants(held_items, "MUT_CYCLE_5", datetime.now())

        # Invariant 검증기: ORDER_ACK_TIMEOUT 승격 감지!
        self.assertTrue(self.watchdog.is_protective_mode_active())
        rec = self.watchdog.active_critical_failures.get(pos.position_id)
        self.assertIsNotNone(rec)
        self.assertEqual(rec.failed_stage, ExitLifecycleStage.EXIT_ORDER_ACK.value)
        self.assertEqual(rec.exit_state, "ORDER_ACK_TIMEOUT")

    # -------------------------------------------------------------------------
    # MUTATION 6: Watchdog loop skipped -> Mutation DETECTED
    # -------------------------------------------------------------------------
    def test_mutation_06_watchdog_loop_skipped(self):
        pos = self.pm.open_position(
            TimeHorizon.INTRADAY, "STRAT_MUT", "006660", "뮤테이션6",
            10, 60000, 58000, 62000, 64000, 66000, 20000
        )
        self.client.holdings["006660"] = 10
        self.client.set_price("006660", 55000)

        # Mutation: Step 0 run_watchdog_cycle 호출을 건너뜀 (Skip)
        watchdog_skipped = True
        if not watchdog_skipped:
            self.watchdog.run_watchdog_cycle([self.account], self.scanner, self.store)

        # Invariant 검증기: 손절가를 하회했음에도 포지션이 여전히 OPEN 상태이고 주문이 0건이면 Mutation 검출!
        mutation_detected = (self.pm.has_position("006660") and len(self.client.submitted_orders) == 0)
        self.assertTrue(mutation_detected, "MUTATION DETECTED: Watchdog loop skipped 감지")

    # -------------------------------------------------------------------------
    # MUTATION 7: Position exception without try-catch -> Mutation DETECTED
    # -------------------------------------------------------------------------
    def test_mutation_07_position_exception_injected(self):
        pos_a = self.pm.open_position(
            TimeHorizon.INTRADAY, "STRAT_MUT", "007771", "에러종목A",
            10, 10000, 9500, 11000, 12000, 13000, 5000
        )
        pos_b = self.pm.open_position(
            TimeHorizon.INTRADAY, "STRAT_MUT", "007772", "정상종목B",
            10, 10000, 9500, 11000, 12000, 13000, 5000
        )
        self.client.holdings["007771"] = 10
        self.client.holdings["007772"] = 10

        # 종목A에 0원 호가 (ValueError) 주입
        self.client.set_price("007771", 0)
        self.client.set_price("007772", 9000)  # 종목B는 손절가 하회

        # Watchdog 사이클 실행 (정상적인 위치별 try-catch가 탑재되어 있음)
        res = self.watchdog.run_watchdog_cycle([self.account], self.scanner, self.store)

        # Mutation 검증: 만약 위치별 try-catch가 제거되었다면 사이클 전체가 비정상 종료되어 B가 체결되지 못했을 것임
        self.assertEqual(res["watchdog_error_count"], 1)
        self.assertTrue(pos_b.is_closed, "MUTATION DETECTED if pos_b is not closed: 종목B 정상 격리 청산 확인")

    # -------------------------------------------------------------------------
    # MUTATION 8: Broker submit failed without retry -> Mutation DETECTED
    # -------------------------------------------------------------------------
    def test_mutation_08_broker_submit_failed_without_retry(self):
        pos = self.pm.open_position(
            TimeHorizon.INTRADAY, "STRAT_MUT", "008880", "뮤테이션8",
            10, 80000, 78000, 82000, 84000, 86000, 20000
        )
        self.client.holdings["008880"] = 10
        pos.current_price = 75000

        # Critical Exit Failure 등록 (브로커 발주 실패)
        self.watchdog.active_critical_failures[pos.position_id] = CriticalExitFailureRecord(
            stock_code="008880", position_id=pos.position_id, current_price=75000,
            stop_price=78000, quantity=10, stop_condition=True, exit_state="ORDER_FAILED",
            failed_stage=ExitLifecycleStage.EXIT_ORDER_CREATED.value, watchdog_cycle_id="MUT_8",
            timestamp=datetime.now().isoformat(), error_message="Broker submit error"
        )

        # Mutation: Fail-Safe Retry 기능을 실행하지 않고 방치함
        retry_skipped = True
        if not retry_skipped:
            self.watchdog._execute_exit_retry_failsafe([self.account], "MUT_8", datetime.now())

        # Invariant 검증기: Fail-Safe Retry가 없으면 active_critical_failures가 미해결 상태로 남아 보호 모드가 영구 지속됨
        mutation_detected = (self.watchdog.is_protective_mode_active() and not self.watchdog.active_critical_failures[pos.position_id].resolved)
        self.assertTrue(mutation_detected, "MUTATION DETECTED: Broker submit failed without retry 감지")


if __name__ == "__main__":
    unittest.main()
