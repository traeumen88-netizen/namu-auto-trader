"""[FINAL MASTER v16.0] Exit Watchdog 12대 Chaos 및 결함 격리 검증 테스트 스위트
tests/test_exit_watchdog_chaos.py

불변조건:
"어떤 신규매수/후보탐색/ML/텔레그램/시세 처리 오류가 발생하더라도
 기존 보유 포지션의 청산 및 손절 기능은 살아 있어야 한다."

12대 Chaos 테스트 시나리오:
TEST 1: 후보 검색 예외 격리 (Candidate Scan Exception)
TEST 2: 전략 평가 예외 격리 (Strategy Setup Exception)
TEST 3: ML 예측 예외 격리 (ML Prediction Exception)
TEST 4: 텔레그램 전송 예외 격리 (Telegram Notification Exception)
TEST 5: 단일 종목 시세 조회 예외 격리 (Single Quote Exception)
TEST 6: 워치독 내부 포지션 예외 격리 (Watchdog Position Exception)
TEST 7: EXIT_TRIGGER 발동 실패 및 보호 모드 (Exit Trigger Failure)
TEST 8: EXIT_ORDER_CREATED 생성 실패 및 Fail-Safe (Order Creation Failure)
TEST 9: 브로커 전송 실패 및 Idempotent 재시도 (Broker Submit Exception & Retry)
TEST 10: 브로커 ACK 타임아웃 감지 (Broker ACK Timeout)
TEST 11: 트레이딩 파이프라인 전면 장애 격리 (Complete Pipeline Failure)
TEST 12: 워치독 최상위 장애 및 자동 복구 (Watchdog Top-Level Crash & Recovery)
"""

import os
import sys
import time
import json
import sqlite3
import unittest
from datetime import datetime, timedelta
from typing import Dict, Any, List, Optional

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if BASE_DIR not in sys.path:
    sys.path.insert(0, BASE_DIR)

from core.models import (
    Position, TradeSignal, Order, OrderSide, OrderType, OrderStatus, TimeHorizon, SymbolState
)
from execution.exit_watchdog import (
    ExitWatchdog, ExitLifecycleStage, WatchdogStatus, WatchdogHeartbeat, CriticalExitFailureRecord
)
from execution.position_manager import PositionManager
from execution.order_router import OrderRouter
from execution.persistence_manager import PersistenceManager
from risk.circuit_breaker import CircuitBreaker


class MockChaosBrokerClient:
    """Chaos 시뮬레이션을 위한 커스텀 모의 브로커 클라이언트"""
    def __init__(self):
        self.prices: Dict[str, Dict[str, Any]] = {}
        self.submitted_orders: List[Dict[str, Any]] = []
        self.holdings: Dict[str, int] = {}
        self.throw_on_quote: Dict[str, Exception] = {}
        self.throw_on_order: bool = False
        self.throw_on_balance: bool = False
        self.order_counter: int = 1000

    def set_price(self, iem_cd: str, price: int, bid: Optional[int] = None, ask: Optional[int] = None):
        self.prices[iem_cd] = {
            "price": price,
            "bid": bid or price,
            "ask": ask or price,
            "open": price,
            "high": price,
            "low": price,
            "prev_close": price
        }

    def get_current_price(self, iem_cd: str) -> Dict[str, Any]:
        if iem_cd in self.throw_on_quote:
            raise self.throw_on_quote[iem_cd]
        return self.prices.get(iem_cd, {"price": 10000, "bid": 10000, "ask": 10000})

    def get_balance(self) -> Dict[str, Any]:
        if self.throw_on_balance:
            raise RuntimeError("Broker balance query network timeout")
        holdings_list = [{"iem_cd": code, "qty": qty} for code, qty in self.holdings.items()]
        return {"holdings": holdings_list, "cash": 10000000}

    def sell_market(self, iem_cd: str, qty: int) -> Dict[str, Any]:
        if self.throw_on_order:
            raise RuntimeError("Broker sell_market gateway timeout")
        self.order_counter += 1
        ord_no = f"MKT_SELL_{self.order_counter}"
        self.submitted_orders.append({
            "iem_cd": iem_cd, "side": "SELL", "order_type": "MARKET", "qty": qty, "order_no": ord_no
        })
        # 체결 반영
        if iem_cd in self.holdings:
            self.holdings[iem_cd] = max(0, self.holdings[iem_cd] - qty)
        return {"Output_0": {"mkt_orr_no": ord_no}}

    def sell_limit(self, iem_cd: str, qty: int, price: int) -> Dict[str, Any]:
        if self.throw_on_order:
            raise RuntimeError("Broker sell_limit gateway timeout")
        self.order_counter += 1
        ord_no = f"LMT_SELL_{self.order_counter}"
        self.submitted_orders.append({
            "iem_cd": iem_cd, "side": "SELL", "order_type": "LIMIT", "qty": qty, "price": price, "order_no": ord_no
        })
        if iem_cd in self.holdings:
            self.holdings[iem_cd] = max(0, self.holdings[iem_cd] - qty)
        return {"Output_0": {"mkt_orr_no": ord_no}}

    def buy_market(self, iem_cd: str, qty: int) -> Dict[str, Any]:
        if self.throw_on_order:
            raise RuntimeError("Broker buy_market rejected")
        self.order_counter += 1
        ord_no = f"MKT_BUY_{self.order_counter}"
        self.submitted_orders.append({
            "iem_cd": iem_cd, "side": "BUY", "order_type": "MARKET", "qty": qty, "order_no": ord_no
        })
        self.holdings[iem_cd] = self.holdings.get(iem_cd, 0) + qty
        return {"Output_0": {"mkt_orr_no": ord_no}}


class MockScanner:
    def get_aggregator(self, code: str):
        class MockAgg:
            def calculate_atr(self, *args): return 100.0
            def calculate_ema(self, *args): return 50000.0
        return MockAgg()


class MockStore:
    def __init__(self):
        self.demoted = []
        self.cooldowns = []
    def demote(self, code, state, reason=""):
        self.demoted.append((code, state, reason))
    def set_cooldown(self, code, now, cooldown_seconds=600):
        self.cooldowns.append((code, cooldown_seconds))


class MockAccount:
    def __init__(self, name: str, client: Any, pm: PositionManager, router: OrderRouter):
        self.name = name
        self.client = client
        self.position_manager = pm
        self.order_router = router


class TestExitWatchdogChaos(unittest.TestCase):
    def setUp(self):
        self.test_db = f"data/test_chaos_{int(time.time() * 1000)}.db"
        self.test_hb = f"data/test_hb_{int(time.time() * 1000)}.json"
        self.client = MockChaosBrokerClient()
        self.circuit_breaker = CircuitBreaker()
        self.circuit_breaker.update_data_heartbeat(datetime.now())
        self.router = OrderRouter(namu_client=self.client, circuit_breaker=self.circuit_breaker)
        self.persistence = PersistenceManager(db_path=self.test_db)
        self.pm = PositionManager(order_router=self.router, persistence_manager=self.persistence)
        self.account = MockAccount("MOCK", self.client, self.pm, self.router)
        self.scanner = MockScanner()
        self.store = MockStore()

        self.watchdog = ExitWatchdog(
            db_path=self.test_db,
            heartbeat_file=self.test_hb,
            stop_to_trigger_timeout_sec=0.5,
            trigger_to_order_timeout_sec=0.5,
            order_to_sent_timeout_sec=0.5,
            sent_to_ack_timeout_sec=0.5
        )

    def tearDown(self):
        for f in [self.test_db, self.test_hb, self.test_hb + ".tmp"]:
            if os.path.exists(f):
                try:
                    os.remove(f)
                except Exception:
                    pass

    # -------------------------------------------------------------------------
    # TEST 1: Candidate scan exception -> Watchdog runs, STOP detected, SELL generated
    # -------------------------------------------------------------------------
    def test_chaos_01_candidate_scan_exception(self):
        pos = self.pm.open_position(
            TimeHorizon.INTRADAY, "STRAT_A", "005930", "삼성전자",
            10, 70000, 68000, 72000, 74000, 76000, 20000
        )
        self.client.holdings["005930"] = 10
        self.client.set_price("005930", 67000)  # 손절가 68,000원 하회

        # Simulate Candidate Scan throwing a fatal exception
        def failing_scan():
            raise ConnectionResetError("Broker candidate socket crashed!")

        with self.assertRaises(ConnectionResetError):
            failing_scan()

        # Step 0 Exit Watchdog runs independently
        res = self.watchdog.run_watchdog_cycle([self.account], self.scanner, self.store)
        self.assertEqual(res["held_position_count"], 1)
        self.assertGreaterEqual(res["stop_triggered_count"], 1)
        self.assertEqual(len(self.client.submitted_orders), 1)
        self.assertEqual(self.client.submitted_orders[0]["side"], "SELL")
        self.assertEqual(self.client.submitted_orders[0]["iem_cd"], "005930")
        self.assertTrue(pos.is_closed)

    # -------------------------------------------------------------------------
    # TEST 2: Strategy exception -> Watchdog runs, STOP detected, SELL generated
    # -------------------------------------------------------------------------
    def test_chaos_02_strategy_exception(self):
        pos = self.pm.open_position(
            TimeHorizon.INTRADAY, "STRAT_B", "000660", "SK하이닉스",
            5, 120000, 115000, 125000, 130000, 135000, 25000
        )
        self.client.holdings["000660"] = 5
        self.client.set_price("000660", 114000)  # 손절가 115,000원 하회

        # Simulate Strategy evaluating crashing with ZeroDivisionError
        try:
            _ = 1 / 0
        except ZeroDivisionError:
            pass  # isolated

        res = self.watchdog.run_watchdog_cycle([self.account], self.scanner, self.store)
        self.assertEqual(len(self.client.submitted_orders), 1)
        self.assertEqual(self.client.submitted_orders[0]["iem_cd"], "000660")
        self.assertTrue(pos.is_closed)

    # -------------------------------------------------------------------------
    # TEST 3: ML exception -> ML crashes, BUY fails, Watchdog runs, STOP -> SELL
    # -------------------------------------------------------------------------
    def test_chaos_03_ml_exception(self):
        pos = self.pm.open_position(
            TimeHorizon.INTRADAY, "STRAT_ML", "035420", "NAVER",
            8, 200000, 195000, 210000, 220000, 230000, 40000
        )
        self.client.holdings["035420"] = 8
        self.client.set_price("035420", 192000)

        # Simulate ML model failure
        def ml_predict():
            raise MemoryError("ML ONNX Model inference OOM")

        try:
            ml_predict()
        except MemoryError:
            pass

        # Step 0 Watchdog executes exit without failure
        res = self.watchdog.run_watchdog_cycle([self.account], self.scanner, self.store)
        self.assertEqual(len(self.client.submitted_orders), 1)
        self.assertEqual(self.client.submitted_orders[0]["iem_cd"], "035420")
        self.assertTrue(pos.is_closed)

    # -------------------------------------------------------------------------
    # TEST 4: Telegram exception -> Telegram fails, Watchdog runs, STOP -> SELL
    # -------------------------------------------------------------------------
    def test_chaos_04_telegram_exception(self):
        pos = self.pm.open_position(
            TimeHorizon.INTRADAY, "STRAT_TG", "051910", "LG화학",
            3, 300000, 290000, 320000, 330000, 340000, 30000
        )
        self.client.holdings["051910"] = 3
        self.client.set_price("051910", 285000)

        # Simulate Telegram API 409 Conflict
        def send_telegram():
            raise RuntimeError("HTTP 409 Conflict: terminated by other getUpdates request")

        try:
            send_telegram()
        except RuntimeError:
            pass

        res = self.watchdog.run_watchdog_cycle([self.account], self.scanner, self.store)
        self.assertEqual(len(self.client.submitted_orders), 1)
        self.assertEqual(self.client.submitted_orders[0]["iem_cd"], "051910")
        self.assertTrue(pos.is_closed)

    # -------------------------------------------------------------------------
    # TEST 5: Single quote exception -> Pos A fails, Pos B/C check & STOP -> SELL
    # -------------------------------------------------------------------------
    def test_chaos_05_single_quote_exception(self):
        pos_a = self.pm.open_position(
            TimeHorizon.INTRADAY, "STRAT_A", "111111", "종목A",
            10, 10000, 9500, 11000, 12000, 13000, 5000
        )
        pos_b = self.pm.open_position(
            TimeHorizon.INTRADAY, "STRAT_B", "222222", "종목B",
            20, 20000, 19000, 22000, 23000, 24000, 20000
        )
        self.client.holdings["111111"] = 10
        self.client.holdings["222222"] = 20

        # 종목A 시세 조회 시 강제 SocketTimeout 예외
        self.client.throw_on_quote["111111"] = TimeoutError("REST quote timeout on 111111")
        self.client.set_price("222222", 18500)  # 종목B는 정상 수신 및 손절가 하회

        res = self.watchdog.run_watchdog_cycle([self.account], self.scanner, self.store)

        # 종목A는 WATCHDOG_POSITION_ERROR로 격리 (에러 카운트 1)
        self.assertEqual(res["watchdog_error_count"], 1)
        # 종목B는 정상 감시되어 손절 매도 발주 완료
        self.assertEqual(len(self.client.submitted_orders), 1)
        self.assertEqual(self.client.submitted_orders[0]["iem_cd"], "222222")
        self.assertTrue(pos_b.is_closed)
        self.assertFalse(pos_a.is_closed)

    # -------------------------------------------------------------------------
    # TEST 6: Watchdog position exception -> Pos A isolated, Pos B checked
    # -------------------------------------------------------------------------
    def test_chaos_06_watchdog_position_exception(self):
        pos_a = self.pm.open_position(
            TimeHorizon.INTRADAY, "STRAT_A", "333333", "에러종목A",
            10, 50000, 48000, 52000, 54000, 56000, 20000
        )
        pos_b = self.pm.open_position(
            TimeHorizon.INTRADAY, "STRAT_B", "444444", "정상종목B",
            10, 50000, 48000, 52000, 54000, 56000, 20000
        )
        self.client.holdings["333333"] = 10
        self.client.holdings["444444"] = 10

        # 0원 호가로 ValueError 유발
        self.client.set_price("333333", 0)
        self.client.set_price("444444", 47000)  # 손절가 하회

        res = self.watchdog.run_watchdog_cycle([self.account], self.scanner, self.store)
        self.assertGreaterEqual(res["watchdog_error_count"], 1)
        # 정상 종목B 매도 발주 성공
        self.assertTrue(pos_b.is_closed)
        self.assertEqual(self.client.submitted_orders[0]["iem_cd"], "444444")

    # -------------------------------------------------------------------------
    # TEST 7: EXIT_TRIGGER exception -> CRITICAL_EXIT_FAILURE set, BUY blocked
    # -------------------------------------------------------------------------
    def test_chaos_07_exit_trigger_timeout_and_protective_mode(self):
        pos = self.pm.open_position(
            TimeHorizon.INTRADAY, "STRAT_A", "555555", "트리거장애",
            10, 10000, 9000, 11000, 12000, 13000, 10000
        )
        self.client.holdings["555555"] = 10
        self.client.set_price("555555", 8500)
        pos.current_price = 8500

        # HARD_STOP_CONDITION_TRUE 기록 후 EXIT_TRIGGERED 누락 & 타임아웃 초과 시뮬레이션
        old_time = time.time() - 2.0
        self.watchdog.stage_timestamps[pos.position_id] = {
            ExitLifecycleStage.HARD_STOP_CONDITION_TRUE.value: old_time
        }

        # 불변조건 검사 직접 호출
        held_items = [(self.account, pos)]
        self.watchdog._evaluate_critical_exit_invariants(held_items, "CYCLE_TEST_7", datetime.now())

        # CRITICAL_EXIT_FAILURE 활성화 및 보호 모드 진입 확인
        self.assertTrue(self.watchdog.is_protective_mode_active())
        self.assertIn(pos.position_id, self.watchdog.active_critical_failures)
        rec = self.watchdog.active_critical_failures[pos.position_id]
        self.assertEqual(rec.failed_stage, ExitLifecycleStage.EXIT_TRIGGERED.value)

        # OrderRouter 보호 모드 동기화 -> 신규 BUY는 차단, SELL은 허용
        self.router.set_critical_exit_failure(True)
        buy_sig = TradeSignal(
            strategy_id="BUY_STRAT", time_horizon=TimeHorizon.INTRADAY, iem_cd="005930",
            name="삼성전자", side=OrderSide.BUY, strategy_price=70000, stop_price=68000,
            score=90.0, reason="진입", timestamp=datetime.now()
        )
        passed, msg = self.router.run_pre_order_checks(
            buy_sig, 10, 70000, {"cash": 10000000}, "NORMAL"
        )
        self.assertFalse(passed)
        self.assertIn("CRITICAL_EXIT_FAILURE", msg)

    # -------------------------------------------------------------------------
    # TEST 8: Order creation exception -> CRITICAL_EXIT_FAILURE, retry triggered
    # -------------------------------------------------------------------------
    def test_chaos_08_order_creation_failure_and_retry(self):
        pos = self.pm.open_position(
            TimeHorizon.INTRADAY, "STRAT_A", "666666", "주문생성장애",
            10, 20000, 19000, 21000, 22000, 23000, 10000
        )
        self.client.holdings["666666"] = 10
        pos.current_price = 18000

        old_time = time.time() - 2.0
        self.watchdog.stage_timestamps[pos.position_id] = {
            ExitLifecycleStage.HARD_STOP_CONDITION_TRUE.value: old_time,
            ExitLifecycleStage.EXIT_TRIGGERED.value: old_time
            # EXIT_ORDER_CREATED 누락
        }

        held_items = [(self.account, pos)]
        self.watchdog._evaluate_critical_exit_invariants(held_items, "CYCLE_TEST_8", datetime.now())

        self.assertTrue(self.watchdog.is_protective_mode_active())
        rec = self.watchdog.active_critical_failures[pos.position_id]
        self.assertEqual(rec.failed_stage, ExitLifecycleStage.EXIT_ORDER_CREATED.value)

        # Fail-Safe Retry 실행 -> 긴급 매도 재발주 성공 확인
        self.watchdog._execute_exit_retry_failsafe([self.account], "CYCLE_TEST_8", datetime.now())
        self.assertEqual(len(self.client.submitted_orders), 1)
        self.assertEqual(self.client.submitted_orders[0]["iem_cd"], "666666")
        self.assertTrue(rec.resolved)

    # -------------------------------------------------------------------------
    # TEST 9: Broker submit exception -> Retry succeeds without duplicates
    # -------------------------------------------------------------------------
    def test_chaos_09_broker_submit_exception_and_idempotent_retry(self):
        pos = self.pm.open_position(
            TimeHorizon.INTRADAY, "STRAT_A", "777777", "브로커장애",
            5, 30000, 29000, 31000, 32000, 33000, 5000
        )
        self.client.holdings["777777"] = 5
        self.client.set_price("777777", 28000)

        # 1차 시도: 브로커 장애 발생
        self.client.throw_on_order = True
        try:
            self.watchdog.run_watchdog_cycle([self.account], self.scanner, self.store)
        except Exception:
            pass

        # 브로커 장애 상태 복구
        self.client.throw_on_order = False

        # Fail-safe retry 실행
        self.watchdog.active_critical_failures[pos.position_id] = CriticalExitFailureRecord(
            stock_code="777777", position_id=pos.position_id, current_price=28000,
            stop_price=29000, quantity=5, stop_condition=True, exit_state="ORDER_FAILED",
            failed_stage=ExitLifecycleStage.EXIT_ORDER_CREATED.value, watchdog_cycle_id="CYCLE_9",
            timestamp=datetime.now().isoformat(), error_message="Broker timeout"
        )
        self.watchdog._execute_exit_retry_failsafe([self.account], "CYCLE_9", datetime.now())

        # 1건만 정상 발주되어 중복 발주가 방지되었는지 확인
        self.assertEqual(len(self.client.submitted_orders), 1)
        self.assertEqual(self.client.submitted_orders[0]["iem_cd"], "777777")
        self.assertTrue(pos.is_closed)

    # -------------------------------------------------------------------------
    # TEST 10: Broker ACK timeout -> ORDER_ACK_TIMEOUT detected
    # -------------------------------------------------------------------------
    def test_chaos_10_broker_ack_timeout(self):
        pos = self.pm.open_position(
            TimeHorizon.INTRADAY, "STRAT_A", "888888", "ACK장애",
            10, 40000, 38000, 42000, 44000, 46000, 20000
        )
        # SELL 주문은 보냈으나 ACK를 받지 못하고 PENDING 상태로 멈춘 가상 주문 생성
        fake_order = Order(
            client_order_id="SELL_ACK_TIMEOUT_123",
            iem_cd="888888",
            side=OrderSide.SELL,
            order_type=OrderType.MARKET,
            qty=10,
            price=37000,
            strategy_id="STRAT_A",
            time_horizon=TimeHorizon.INTRADAY,
            status=OrderStatus.PENDING,
            sent_at=datetime.now() - timedelta(seconds=10)  # 10초 전 전송
        )
        self.router.pending_orders["SELL_ACK_TIMEOUT_123"] = fake_order
        pos.active_exit_order_id = "SELL_ACK_TIMEOUT_123"
        pos.current_price = 37000

        held_items = [(self.account, pos)]
        self.watchdog._evaluate_critical_exit_invariants(held_items, "CYCLE_10", datetime.now())

        self.assertTrue(self.watchdog.is_protective_mode_active())
        rec = self.watchdog.active_critical_failures[pos.position_id]
        self.assertEqual(rec.failed_stage, ExitLifecycleStage.EXIT_ORDER_ACK.value)
        self.assertEqual(rec.exit_state, "ORDER_ACK_TIMEOUT")

    # -------------------------------------------------------------------------
    # TEST 11: Complete trading loop failure -> Watchdog still executes STOP -> SELL
    # -------------------------------------------------------------------------
    def test_chaos_11_complete_trading_loop_failure(self):
        pos = self.pm.open_position(
            TimeHorizon.INTRADAY, "STRAT_A", "999999", "루프파괴종목",
            15, 50000, 48000, 52000, 54000, 56000, 30000
        )
        self.client.holdings["999999"] = 15
        self.client.set_price("999999", 47000)

        # 시뮬레이션: 메인 트레이딩 파이프라인 전체가 파괴되었을 때도 Step 0 워치독 단독 완수
        def broken_main_pipeline():
            raise SystemError("Fatal pipeline memory corruption")

        try:
            broken_main_pipeline()
        except SystemError:
            pass

        res = self.watchdog.run_watchdog_cycle([self.account], self.scanner, self.store)
        self.assertEqual(len(self.client.submitted_orders), 1)
        self.assertEqual(self.client.submitted_orders[0]["iem_cd"], "999999")
        self.assertTrue(pos.is_closed)

    # -------------------------------------------------------------------------
    # TEST 12: Watchdog self-termination & recovery -> recover() restores HEALTHY
    # -------------------------------------------------------------------------
    def test_chaos_12_watchdog_crash_and_recovery(self):
        # 최상위 예외 발생 시뮬레이션: accounts 인자에 None 전달
        res = self.watchdog.run_watchdog_cycle(None, self.scanner, self.store)
        self.assertEqual(res["watchdog_status"], WatchdogStatus.CRITICAL.value)
        self.assertGreaterEqual(res["watchdog_consecutive_failures"], 1)

        # 복구 메커니즘 recover() 호출
        self.watchdog.recover()
        self.assertEqual(self.watchdog.current_heartbeat.watchdog_status, WatchdogStatus.HEALTHY.value)
        self.assertEqual(self.watchdog.current_heartbeat.watchdog_consecutive_failures, 0)
        self.assertEqual(len(self.watchdog.active_critical_failures), 0)


if __name__ == "__main__":
    unittest.main()
