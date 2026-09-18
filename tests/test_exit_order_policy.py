"""[FINAL MASTER v16.0] Exit Reason별 Order Type 매핑 및 실행 정책 테스트 스위트
tests/test_exit_order_policy.py
"""

import os
import unittest
import shutil
from datetime import datetime, timedelta
from typing import Dict, Any

from core.models import Position, TimeHorizon, OrderSide, OrderType, OrderStatus, TradeSignal
from core.tick_normalizer import get_tick_size, normalize_price
from execution.exit_order_policy import ExitOrderPolicyEngine, ExitReason, ExitOrderPlan
from execution.persistence_manager import PersistenceManager
from execution.order_router import OrderRouter
from execution.position_manager import PositionManager
import config.settings as settings


class MockClient:
    def __init__(self, dry_run: bool = True):
        self.dry_run = dry_run
        self.cancelled_orders = []

    def get_balance(self):
        return {"cash": 100_000_000, "total_asset": 100_000_000, "holdings": []}

    def buy_limit(self, iem_cd, qty, price):
        return {"Output_0": {"mkt_orr_no": f"MOCK_BUY_{iem_cd}"}}

    def buy_market(self, iem_cd, qty):
        return {"Output_0": {"mkt_orr_no": f"MOCK_BUY_MKT_{iem_cd}"}}

    def sell_limit(self, iem_cd, qty, price):
        return {"Output_0": {"mkt_orr_no": f"MOCK_SELL_{iem_cd}"}}

    def sell_market(self, iem_cd, qty):
        return {"Output_0": {"mkt_orr_no": f"MOCK_SELL_MKT_{iem_cd}"}}

    def cancel_order(self, order_no, iem_cd, qty):
        self.cancelled_orders.append((order_no, iem_cd, qty))
        return {"status": "CANCELLED"}


class TestExitOrderPolicy(unittest.TestCase):

    def setUp(self):
        self.test_dir = "data/test_exit_policy"
        os.makedirs(self.test_dir, exist_ok=True)
        self.db_path = os.path.join(self.test_dir, "test_exit.db")
        self.cold_dir = os.path.join(self.test_dir, "cold")
        if os.path.exists(self.db_path):
            os.remove(self.db_path)

        self.persistence = PersistenceManager(db_path=self.db_path, cold_dir=self.cold_dir)
        self.mock_client = MockClient(dry_run=True)
        self.order_router = OrderRouter(namu_client=self.mock_client, circuit_breaker=None)
        self.pos_manager = PositionManager(order_router=self.order_router, persistence_manager=self.persistence)
        self.now = datetime(2026, 9, 8, 10, 0, 0)

    def tearDown(self):
        if os.path.exists(self.test_dir):
            try:
                shutil.rmtree(self.test_dir)
            except Exception:
                pass

    # =========================================================================
    # Test 1: ExitReason 파싱 정밀도 검증
    # =========================================================================
    def test_01_exit_reason_parsing(self):
        engine = ExitOrderPolicyEngine
        self.assertEqual(engine.parse_exit_reason("TARGET_EXIT"), ExitReason.TARGET_EXIT)
        self.assertEqual(engine.parse_exit_reason("1차 익절 도달 (+1R)"), ExitReason.SCALE_OUT)
        self.assertEqual(engine.parse_exit_reason("+2R 도달 (30% 익절)"), ExitReason.TARGET_2_EXIT)
        self.assertEqual(engine.parse_exit_reason("+3R 도달 (20% 익절)"), ExitReason.TARGET_EXIT)
        self.assertEqual(engine.parse_exit_reason("스톱로스 도달 (68,500원)"), ExitReason.HARD_STOP)
        self.assertEqual(engine.parse_exit_reason("스윙 손절 도달"), ExitReason.HARD_STOP)
        self.assertEqual(engine.parse_exit_reason("Trailing Stop / EMA9 이탈 청산"), ExitReason.TRAILING_STOP)
        self.assertEqual(engine.parse_exit_reason("추세 이탈 (MA20 이탈)"), ExitReason.TREND_BREAK)
        self.assertEqual(engine.parse_exit_reason("시장 서킷브레이커 발동"), ExitReason.CIRCUIT_BREAKER)
        self.assertEqual(engine.parse_exit_reason("긴급 비상 청산"), ExitReason.EMERGENCY)
        self.assertEqual(engine.parse_exit_reason("시간 정체 타임스톱 청산"), ExitReason.TIME_STOP)
        self.assertEqual(engine.parse_exit_reason("장마감 단타 강제청산 (15:20)"), ExitReason.END_OF_DAY)
        self.assertEqual(engine.parse_exit_reason("장마감 단타 손실정리 (15:10)"), ExitReason.END_OF_DAY)

    # =========================================================================
    # Test 2: 수익 실현 계열 Order Type 매핑 (LIMIT 우선, Fallback AGGRESSIVE_LIMIT)
    # =========================================================================
    def test_02_profit_taking_mapping_limit(self):
        profit_reasons = [ExitReason.TARGET_EXIT, ExitReason.SCALE_OUT, ExitReason.TARGET_2_EXIT]
        for reason in profit_reasons:
            plan = ExitOrderPolicyEngine.determine_exit_plan(
                exit_reason=reason,
                qty=50,
                current_price=70000.0,
                bid=69900.0,
                ask=70100.0,
                is_fallback=False,
                now=self.now
            )
            self.assertEqual(plan.selected_order_type, OrderType.LIMIT)
            self.assertEqual(plan.fallback_order_type, OrderType.AGGRESSIVE_LIMIT)
            self.assertGreater(plan.limit_price, 0)
            self.assertEqual(plan.order_quantity, 50)
            self.assertEqual(plan.spread, 200.0)

    # =========================================================================
    # Test 3: 위험 회피 계열 Order Type 매핑 (MARKET 체결 우선, Fallback AGGRESSIVE_LIMIT)
    # =========================================================================
    def test_03_risk_avoidance_mapping_market(self):
        risk_reasons = [
            ExitReason.HARD_STOP,
            ExitReason.TRAILING_STOP,
            ExitReason.TREND_BREAK,
            ExitReason.EMERGENCY,
            ExitReason.CIRCUIT_BREAKER
        ]
        for reason in risk_reasons:
            plan = ExitOrderPolicyEngine.determine_exit_plan(
                exit_reason=reason,
                qty=100,
                current_price=68000.0,
                bid=67900.0,
                ask=68100.0,
                is_fallback=False,
                now=self.now
            )
            self.assertEqual(plan.selected_order_type, OrderType.MARKET)
            self.assertEqual(plan.fallback_order_type, OrderType.AGGRESSIVE_LIMIT)
            self.assertEqual(plan.limit_price, 0.0)

    # =========================================================================
    # Test 4: TIME_STOP 및 END_OF_DAY 매핑
    # =========================================================================
    def test_04_time_stop_and_end_of_day_mapping(self):
        # TIME_STOP: 기회비용 제거 (LIMIT -> AGGRESSIVE_LIMIT)
        time_plan = ExitOrderPolicyEngine.determine_exit_plan(
            exit_reason=ExitReason.TIME_STOP,
            qty=30,
            current_price=50000.0,
            bid=49950.0,
            ask=50050.0
        )
        self.assertEqual(time_plan.selected_order_type, OrderType.LIMIT)
        self.assertEqual(time_plan.fallback_order_type, OrderType.AGGRESSIVE_LIMIT)

        # END_OF_DAY: 단타 강제청산 (MARKET -> AGGRESSIVE_LIMIT)
        eod_plan = ExitOrderPolicyEngine.determine_exit_plan(
            exit_reason=ExitReason.END_OF_DAY,
            qty=30,
            current_price=50000.0,
            bid=49950.0,
            ask=50050.0
        )
        self.assertEqual(eod_plan.selected_order_type, OrderType.MARKET)
        self.assertEqual(eod_plan.fallback_order_type, OrderType.AGGRESSIVE_LIMIT)

    # =========================================================================
    # Test 5: Aggressive Limit KRX 호가 단위 기반 동적 계산
    # =========================================================================
    def test_05_aggressive_limit_tick_calculation(self):
        engine = ExitOrderPolicyEngine
        # Case 1: 10,000원 대 (KRX 틱: 10원) -> Bid 10,000원 - 3틱 = 9,970원
        p1 = engine.calculate_aggressive_limit_price(current_price=10000, bid=10000, aggressive_ticks=3)
        self.assertEqual(p1, 9970.0)

        # Case 2: 70,000원 대 (KRX 틱: 100원) -> Bid 70,000원 - 3틱 = 69,700원
        p2 = engine.calculate_aggressive_limit_price(current_price=70000, bid=70000, aggressive_ticks=3)
        self.assertEqual(p2, 69700.0)

        # Case 3: 180,000원 대 (KRX 5만~20만원 구간 틱: 100원) -> Bid 180,000원 - 3틱 = 179,700원
        p3 = engine.calculate_aggressive_limit_price(current_price=180000, bid=180000, aggressive_ticks=3)
        self.assertEqual(p3, 179700.0)

        # Case 4: 300,000원 대 (KRX 20만~50만원 구간 틱: 500원) -> Bid 300,000원 - 3틱 = 298,500원
        p4 = engine.calculate_aggressive_limit_price(current_price=300000, bid=300000, aggressive_ticks=3)
        self.assertEqual(p4, 298500.0)

        # Fallback Plan 산출 시 AGGRESSIVE_LIMIT 가격 검증
        plan = engine.determine_exit_plan(
            exit_reason=ExitReason.TARGET_EXIT,
            qty=50,
            current_price=70000.0,
            bid=70000.0,
            is_fallback=True
        )
        self.assertEqual(plan.selected_order_type, OrderType.AGGRESSIVE_LIMIT)
        self.assertEqual(plan.limit_price, 69700.0)

    # =========================================================================
    # Test 6: 중복 Exit 주문 방지 (active_exit_order_id)
    # =========================================================================
    def test_06_position_duplicate_exit_prevention(self):
        offline_router = OrderRouter(namu_client=None, circuit_breaker=None)
        pm = PositionManager(order_router=offline_router, persistence_manager=self.persistence)

        pos = pm.open_position(
            time_horizon=TimeHorizon.INTRADAY,
            strategy_id="INT_TEST",
            iem_cd="005930",
            name="삼성전자",
            qty=100,
            entry_price=70000.0,
            stop_price=69000,
            target_1r=71500,
            target_2r=73000,
            target_3r=74500,
            initial_risk=100000.0
        )

        # 1. 첫 번째 부분 익절 발주 (+1R: 30%)
        pm._partial_exit(pos, sell_qty=30, price=71500, reason="+1R 도달 (30% 익절)")
        self.assertIsNotNone(pos.active_exit_order_id)
        first_exit_id = pos.active_exit_order_id
        self.assertIn(first_exit_id, offline_router.pending_orders)
        self.assertEqual(pos.available_qty, 70)
        self.assertEqual(pos.pending_exit_qty, 30)
        self.assertEqual(pos.qty, 100)

        # 2. 미체결 상태에서 두 번째 Exit 시도 -> 차단되어야 함
        pm._partial_exit(pos, sell_qty=30, price=71600, reason="+1R 도달 (30% 익절)")
        self.assertEqual(pos.active_exit_order_id, first_exit_id)
        self.assertEqual(pos.available_qty, 70)
        self.assertEqual(pos.qty, 100)

        # 3. 비위험 익절 계열 _close_position 시도 또한 중복 차단되어야 함
        pm._close_position(pos, price=72000, exit_time=self.now, reason="+2R 전량 익절 마감")
        self.assertEqual(pos.available_qty, 70)
        self.assertEqual(pos.qty, 100)
        self.assertFalse(pos.is_closed)

        # 4. 반면, 긴급 위험회피 스톱로스 도달 시에는 기존 미체결 익절 주문을 취소하고 즉시 전량 청산 집행 (Section 4 & 7)
        pm._close_position(pos, price=68000, exit_time=self.now, reason="스톱로스 도달")
        self.assertNotIn(first_exit_id, offline_router.pending_orders)
        self.assertIsNotNone(pos.active_exit_order_id)
        # 브로커 실제 체결 통지 수신 시 포지션 완전 종료 및 수량 0 확정
        offline_router.on_fill(pos.active_exit_order_id, 100, 68000.0, is_cumulative=True)
        self.assertEqual(pos.qty, 0)
        self.assertTrue(pos.is_closed)

    # =========================================================================
    # Test 7: 주문 방식 결정 감사 로그 (Decision Audit Trail) DB 영구 저장
    # =========================================================================
    def test_07_decision_audit_trail_persistence(self):
        plan = ExitOrderPolicyEngine.determine_exit_plan(
            exit_reason=ExitReason.HARD_STOP,
            qty=120,
            current_price=68500.0,
            bid=68400.0,
            ask=68600.0,
            now=self.now
        )

        log_id = self.persistence.record_exit_execution(plan, iem_cd="005930", position_id="INT_005930_TEST")
        self.assertTrue(log_id.startswith("EXIT_"))

        # DB 조회 검증
        logs = self.persistence.get_exit_execution_logs(limit=10, iem_cd="005930")
        self.assertGreaterEqual(len(logs), 1)
        latest = logs[0]

        self.assertEqual(latest["iem_cd"], "005930")
        self.assertEqual(latest["position_id"], "INT_005930_TEST")
        self.assertEqual(latest["exit_reason"], "HARD_STOP")
        self.assertEqual(latest["selected_order_type"], "MARKET")
        self.assertEqual(latest["fallback_order_type"], "AGGRESSIVE_LIMIT")
        self.assertEqual(latest["reference_price"], 68500.0)
        self.assertEqual(latest["limit_price"], 0.0)
        self.assertEqual(latest["aggressive_ticks"], 3)
        self.assertEqual(latest["bid"], 68400.0)
        self.assertEqual(latest["ask"], 68600.0)
        self.assertEqual(latest["spread"], 200.0)
        self.assertEqual(latest["order_quantity"], 120)

    # =========================================================================
    # Test 8: 미체결 지정가 타임아웃 감지 및 Aggressive Limit 전환
    # =========================================================================
    def test_08_order_timeout_and_fallback_transition(self):
        offline_router = OrderRouter(namu_client=None, circuit_breaker=None)
        pm = PositionManager(order_router=offline_router, persistence_manager=self.persistence)

        pos = pm.open_position(
            time_horizon=TimeHorizon.INTRADAY,
            strategy_id="INT_TEST",
            iem_cd="005930",
            name="삼성전자",
            qty=50,
            entry_price=70000.0,
            stop_price=69000,
            target_1r=71500,
            target_2r=73000,
            target_3r=74500,
            initial_risk=50000.0
        )

        # 1. LIMIT Exit 주문 발주 (+1R: 30% = 15주)
        pm._partial_exit(pos, sell_qty=15, price=71500, reason="+1R 도달 (30% 익절)")
        initial_order_id = pos.active_exit_order_id
        self.assertIsNotNone(initial_order_id)
        self.assertIn(initial_order_id, offline_router.pending_orders)
        self.assertEqual(offline_router.pending_orders[initial_order_id].order_type, OrderType.LIMIT)

        # 2. 시간 경과 시뮬레이션: 35초 경과 (기준 30초 초과)
        pos.exit_order_sent_at = self.now - timedelta(seconds=35)
        offline_router.pending_orders[initial_order_id].sent_at = self.now - timedelta(seconds=35)

        # 3. 타임아웃 감지 및 전환 실행
        handled = pm.check_exit_order_timeouts(
            now=self.now,
            timeout_seconds=30.0,
            price_map={"005930": 71200.0},
            bid_map={"005930": 71100.0}
        )

        self.assertEqual(len(handled), 1)
        h = handled[0]
        self.assertEqual(h["cancelled_order_id"], initial_order_id)
        self.assertIsNotNone(h["new_order_id"])
        self.assertNotEqual(h["new_order_id"], initial_order_id)
        self.assertEqual(h["fallback_plan"].selected_order_type, OrderType.AGGRESSIVE_LIMIT)

        # 71,100원 기준 3틱 (틱당 100원) 아래 = 70,800원
        self.assertEqual(h["fallback_plan"].limit_price, 70800.0)

        # 기존 주문은 취소되었고 신규 주문이 등록되어 있어야 함
        self.assertNotIn(initial_order_id, offline_router.pending_orders)
        self.assertIn(h["new_order_id"], offline_router.pending_orders)
        self.assertEqual(pos.active_exit_order_id, h["new_order_id"])

        # 감사 로그 확인
        logs = self.persistence.get_exit_execution_logs(limit=5, iem_cd="005930")
        self.assertGreaterEqual(len(logs), 2)
        fallback_log = logs[0]
        self.assertEqual(fallback_log["selected_order_type"], "AGGRESSIVE_LIMIT")
        self.assertEqual(fallback_log["limit_price"], 70800.0)

    # =========================================================================
    # Test 9: 스톱로스 발동 시 Policy Engine 연동 및 MARKET 주문 발주
    # =========================================================================
    def test_09_stop_loss_watchdog_with_policy(self):
        pos = self.pos_manager.open_position(
            time_horizon=TimeHorizon.INTRADAY,
            strategy_id="INT_BREAKOUT",
            iem_cd="005930",
            name="삼성전자",
            qty=20,
            entry_price=70000.0,
            stop_price=69000,
            target_1r=71500,
            target_2r=73000,
            target_3r=74500,
            initial_risk=20000.0
        )

        # 68,500원 틱 발생 (손절가 69,000원 하회)
        exits = self.pos_manager.check_stops({"005930": 68500}, self.now)
        self.assertEqual(len(exits), 1)
        self.assertTrue(pos.is_closed)
        self.assertEqual(self.pos_manager.stop_watchdog_stats["stops_triggered"], 1)
        self.assertEqual(self.pos_manager.stop_watchdog_stats["stops_executed"], 1)

        # 감사 로그 검증
        logs = self.persistence.get_exit_execution_logs(limit=5, iem_cd="005930")
        self.assertGreaterEqual(len(logs), 1)
        self.assertEqual(logs[0]["exit_reason"], "HARD_STOP")
        self.assertEqual(logs[0]["selected_order_type"], "MARKET")

    # =========================================================================
    # Test 10: 분할 익절(+1R) 시 Policy Engine 연동 및 LIMIT 주문 발주
    # =========================================================================
    def test_10_partial_profit_take_with_policy(self):
        pos = self.pos_manager.open_position(
            time_horizon=TimeHorizon.INTRADAY,
            strategy_id="INT_BREAKOUT",
            iem_cd="005930",
            name="삼성전자",
            qty=20,
            entry_price=70000.0,
            stop_price=69000,
            target_1r=71500,
            target_2r=73000,
            target_3r=74500,
            initial_risk=20000.0
        )

        # 71,500원 틱 발생 (+1R 도달)
        self.pos_manager.update_price_and_manage(
            iem_cd="005930",
            current_price=71500,
            current_time=self.now,
            bid=71400.0,
            ask=71500.0
        )

        self.assertTrue(pos.target_1r_taken)
        self.assertEqual(pos.stop_price, 70000)  # Break-even stop
        self.assertEqual(self.pos_manager.stop_watchdog_stats["partial_exits"], 1)

        # 감사 로그 검증
        logs = self.persistence.get_exit_execution_logs(limit=5, iem_cd="005930")
        self.assertGreaterEqual(len(logs), 1)
        self.assertEqual(logs[0]["exit_reason"], "SCALE_OUT")
        self.assertEqual(logs[0]["selected_order_type"], "LIMIT")
        self.assertEqual(logs[0]["limit_price"], 71500.0)


if __name__ == "__main__":
    unittest.main()
