"""[CIRCUIT BREAKER ISOLATION & OFF-HOURS DEFENSE TEST SUITE]
Dual 모드에서 LIVE 및 MOCK 계좌 간 CircuitBreaker 완전 격리, 23962 비즈니스 거절 분류,
장외시간 SELL 차단 및 안전한 자동 복구 검증 (TEST A ~ TEST J 완벽 구현)

TEST A: LIVE 장외 SELL 발생 -> 실제 브로커 요청 없음
TEST B: MOCK 장외 SELL 조건 발생 -> 실제 브로커 요청 없음
TEST C: LIVE에서 23962 발생 -> LIVE CircuitBreaker 카운터 증가하지 않음
TEST D: MOCK에서 23962 발생 -> MOCK CircuitBreaker 카운터 증가하지 않음
TEST E: LIVE SYSTEM_FAILURE 3회 -> LIVE breaker만 TRIPPED
TEST F: LIVE breaker TRIPPED -> MOCK BUY 정상 가능
TEST G: MOCK breaker TRIPPED -> LIVE BUY 정상 가능
TEST H: SYSTEM_FAILURE recovery window 이후 -> 해당 계좌 breaker만 정상 복구
TEST I: 장외시간 PositionManager -> Exit Condition 기록 가능 -> SELL broker request 0건
TEST J: 정규 거래시간 -> 기존 SELL/Stop/Target/Trailing 로직 정상
"""

import unittest
from datetime import datetime, time, timedelta
from typing import Optional, Dict, Any, List

from core.models import (
    SymbolInfo, SymbolState, TradeSignal, Order, OrderSide, OrderType,
    OrderStatus, TimeHorizon, MarketRegime, Position
)
from core.api_gateway import ResponseClassifier, ErrorCategory
from core.after_hours_manager import MarketSessionManager, MarketSession
from execution.order_router import OrderRouter
from execution.position_manager import PositionManager
from risk.circuit_breaker import CircuitBreaker


class DummyBrokerClient:
    def __init__(self, mode: str = "mock", act_no: str = "50001003032"):
        self.mode = mode
        self.act_no = act_no
        self.cash = 10_000_000
        self.total_asset = 20_000_000
        self.sell_calls = []
        self.buy_calls = []
        self.simulate_error: Optional[Exception] = None

    def get_balance(self):
        return {
            "cash": self.cash,
            "total_asset": self.total_asset,
            "order_available": self.cash,
            "total_profit": 0,
            "total_profit_rate": 0.0,
            "holdings": []
        }

    def get_current_price(self, iem_cd: str):
        return {
            "iem_cd": iem_cd,
            "price": 10000,
            "bid": 9990,
            "ask": 10010,
            "is_valid": True,
            "quote_time": "100000"
        }

    def sell_limit(self, iem_cd: str, shares: int, price: int):
        self.sell_calls.append({"iem_cd": iem_cd, "shares": shares, "price": price, "type": "LIMIT"})
        if self.simulate_error:
            raise self.simulate_error
        return {"Output_0": {"mkt_orr_no": "ORD_SELL_123"}}

    def sell_market(self, iem_cd: str, shares: int):
        self.sell_calls.append({"iem_cd": iem_cd, "shares": shares, "type": "MARKET"})
        if self.simulate_error:
            raise self.simulate_error
        return {"Output_0": {"mkt_orr_no": "ORD_SELL_124"}}

    def buy_market(self, iem_cd: str, shares: int):
        self.buy_calls.append({"iem_cd": iem_cd, "shares": shares, "type": "MARKET"})
        if self.simulate_error:
            raise self.simulate_error
        return {"Output_0": {"mkt_orr_no": "ORD_BUY_125"}}


class DummyPersistenceManager:
    def __init__(self):
        self.stop_triggers = []
        self.recorded_orders = []

    def record_stop_trigger(self, pos_id, sym, stop_p, curr_p, trading_mode="live", account_no=""):
        self.stop_triggers.append({
            "pos_id": pos_id, "sym": sym, "stop_p": stop_p, "curr_p": curr_p,
            "trading_mode": trading_mode, "account_no": account_no
        })

    def record_exit_execution(self, plan, sym, pos_id):
        pass

    def record_order(self, order, trading_mode="live", account_no=""):
        self.recorded_orders.append(order)

    def record_fill(self, **kwargs):
        pass


class TestCircuitBreakerIsolation(unittest.TestCase):
    def setUp(self):
        # MOCK 환경
        self.mock_client = DummyBrokerClient(mode="mock", act_no="50001003032")
        self.mock_breaker = CircuitBreaker(account_name="MOCK", recovery_window_seconds=1.0)
        self.mock_router = OrderRouter(self.mock_client, self.mock_breaker)
        self.mock_pm_store = DummyPersistenceManager()
        self.mock_pos_mgr = PositionManager(self.mock_router, persistence_manager=self.mock_pm_store)

        # LIVE 환경
        self.live_client = DummyBrokerClient(mode="live", act_no="20201549311")
        self.live_breaker = CircuitBreaker(account_name="LIVE", recovery_window_seconds=1.0)
        self.live_router = OrderRouter(self.live_client, self.live_breaker)
        self.live_pm_store = DummyPersistenceManager()
        self.live_pos_mgr = PositionManager(self.live_router, persistence_manager=self.live_pm_store)

        # 공통 종목
        self.sym_code = "005930"
        self.sym_name = "삼성전자"

    def _open_test_position(self, mgr: PositionManager, entry_price: float = 10000.0, qty: int = 10) -> Position:
        pos = mgr.open_position(
            TimeHorizon.INTRADAY, "INT_BREAKOUT", self.sym_code, self.sym_name,
            qty, entry_price, 9500.0, 10500.0, 11000.0, 11500.0, 5000.0
        )
        return pos

    # TEST A: LIVE 장외 SELL 발생 -> 실제 브로커 요청 없음
    def test_A_live_after_hours_sell_blocked(self):
        pos = self._open_test_position(self.live_pos_mgr, entry_price=10000.0, qty=10)
        # 장외 시간 (23:43:00)
        off_hours_dt = datetime(2026, 9, 16, 23, 43, 0)
        # 현재가 10,600원 (+1R 돌파)
        self.live_pos_mgr.update_price_and_manage(self.sym_code, 10600, off_hours_dt)

        # 브로커로 SELL 주문이 전송되지 않아야 함
        self.assertEqual(len(self.live_client.sell_calls), 0)
        # 포지션 수량은 보존되어야 함 (차감되지 않음)
        self.assertEqual(pos.qty, 10)
        self.assertFalse(pos.is_closed)

    # TEST B: MOCK 장외 SELL 조건 발생 -> 실제 브로커 요청 없음
    def test_B_mock_after_hours_sell_blocked(self):
        pos = self._open_test_position(self.mock_pos_mgr, entry_price=10000.0, qty=10)
        off_hours_dt = datetime(2026, 9, 16, 23, 43, 0)
        # 현재가 9,000원 (Stop-Loss 이탈)
        self.mock_pos_mgr.update_price_and_manage(self.sym_code, 9000, off_hours_dt)

        self.assertEqual(len(self.mock_client.sell_calls), 0)
        self.assertEqual(pos.qty, 10)
        self.assertFalse(pos.is_closed)

    # TEST C: LIVE에서 23962 발생 -> LIVE CircuitBreaker 카운터 증가하지 않음
    def test_C_live_business_rejection_does_not_increment_breaker(self):
        sig = TradeSignal(
            strategy_id="TEST", time_horizon=TimeHorizon.INTRADAY,
            iem_cd=self.sym_code, name=self.sym_name, side=OrderSide.SELL,
            strategy_price=10000, stop_price=0, score=100.0, reason="TEST",
            timestamp=datetime(2026, 9, 17, 10, 0, 0)
        )
        reg_time = datetime(2026, 9, 17, 10, 0, 0)
        # 23962 에러 시뮬레이션
        self.live_client.simulate_error = RuntimeError("[business] 23962 KRX의 매매가능 시간이 아닙니다. (/krstock/order/v1/cashSell)")

        self.live_router.submit_order(sig, 5, OrderType.LIMIT, 10000, now=reg_time)

        # 23962는 비즈니스 거절이므로 consecutive_order_errors가 0이어야 함
        self.assertEqual(self.live_breaker.consecutive_order_errors, 0)
        self.assertFalse(self.live_breaker.is_tripped)

    # TEST D: MOCK에서 23962 발생 -> MOCK CircuitBreaker 카운터 증가하지 않음
    def test_D_mock_business_rejection_does_not_increment_breaker(self):
        sig = TradeSignal(
            strategy_id="TEST", time_horizon=TimeHorizon.INTRADAY,
            iem_cd=self.sym_code, name=self.sym_name, side=OrderSide.SELL,
            strategy_price=10000, stop_price=0, score=100.0, reason="TEST",
            timestamp=datetime(2026, 9, 17, 10, 0, 0)
        )
        reg_time = datetime(2026, 9, 17, 10, 0, 0)
        self.mock_client.simulate_error = RuntimeError("[business] 23962 KRX의 매매가능 시간이 아닙니다.")

        self.mock_router.submit_order(sig, 5, OrderType.LIMIT, 10000, now=reg_time)

        self.assertEqual(self.mock_breaker.consecutive_order_errors, 0)
        self.assertFalse(self.mock_breaker.is_tripped)

    # TEST E: LIVE SYSTEM_FAILURE 3회 -> LIVE breaker만 TRIPPED
    def test_E_live_system_failure_trips_live_only(self):
        sig = TradeSignal(
            strategy_id="TEST", time_horizon=TimeHorizon.INTRADAY,
            iem_cd=self.sym_code, name=self.sym_name, side=OrderSide.SELL,
            strategy_price=10000, stop_price=0, score=100.0, reason="TEST",
            timestamp=datetime(2026, 9, 17, 10, 0, 0)
        )
        reg_time = datetime(2026, 9, 17, 10, 0, 0)
        # 시스템 통신 에러 (500 Server Error)
        self.live_client.simulate_error = ConnectionError("500 Broker Gateway Connection Reset")

        for _ in range(3):
            self.live_router.submit_order(sig, 5, OrderType.LIMIT, 10000, now=reg_time)

        # LIVE 브레이커는 발동되어야 함
        self.assertTrue(self.live_breaker.is_tripped)
        self.assertEqual(self.live_breaker.consecutive_order_errors, 3)
        self.assertEqual(self.live_breaker.trip_type, "SYSTEM_FAILURE")

        # MOCK 브레이커는 완전히 분리되어 영향이 없어야 함
        self.assertFalse(self.mock_breaker.is_tripped)
        self.assertEqual(self.mock_breaker.consecutive_order_errors, 0)

    # TEST F: LIVE breaker TRIPPED -> MOCK BUY 정상 가능
    def test_F_live_tripped_mock_buy_allowed(self):
        # LIVE 브레이커 발동
        self.live_breaker.trip("LIVE 치명적 시스템 장애", trip_type="SYSTEM_FAILURE")
        self.assertTrue(self.live_breaker.is_tripped)

        # MOCK BUY 신호 검증
        buy_sig = TradeSignal(
            strategy_id="MOMENTUM", time_horizon=TimeHorizon.INTRADAY,
            iem_cd=self.sym_code, name=self.sym_name, side=OrderSide.BUY,
            strategy_price=10000, stop_price=9500, score=85.0, reason="BUY_SETUP",
            timestamp=datetime(2026, 9, 17, 10, 0, 0)
        )
        reg_time = datetime(2026, 9, 17, 10, 0, 0)
        balance = {"cash": 10_000_000, "total_asset": 10_000_000}

        passed, msg = self.mock_router.run_pre_order_checks(buy_sig, 10, 10000, balance, "NORMAL", now=reg_time)
        self.assertTrue(passed, f"MOCK BUY가 허용되어야 하나 차단됨: {msg}")

    # TEST G: MOCK breaker TRIPPED -> LIVE BUY 정상 가능
    def test_G_mock_tripped_live_buy_allowed(self):
        # MOCK 브레이커 발동
        self.mock_breaker.trip("MOCK 치명적 시스템 장애", trip_type="SYSTEM_FAILURE")
        self.assertTrue(self.mock_breaker.is_tripped)

        buy_sig = TradeSignal(
            strategy_id="MOMENTUM", time_horizon=TimeHorizon.INTRADAY,
            iem_cd=self.sym_code, name=self.sym_name, side=OrderSide.BUY,
            strategy_price=10000, stop_price=9500, score=85.0, reason="BUY_SETUP",
            timestamp=datetime(2026, 9, 17, 10, 0, 0)
        )
        reg_time = datetime(2026, 9, 17, 10, 0, 0)
        balance = {"cash": 10_000_000, "total_asset": 10_000_000}

        passed, msg = self.live_router.run_pre_order_checks(buy_sig, 10, 10000, balance, "NORMAL", now=reg_time)
        self.assertTrue(passed, f"LIVE BUY가 허용되어야 하나 차단됨: {msg}")

    # TEST H: SYSTEM_FAILURE recovery window 이후 -> 해당 계좌 breaker만 정상 복구
    def test_H_system_failure_recovery_window_passed(self):
        self.live_breaker.recovery_window_seconds = 0.1  # 빠른 테스트용 윈도우
        self.live_breaker.trip("SYSTEM_FAIL", trip_type="SYSTEM_FAILURE")
        self.assertTrue(self.live_breaker.is_tripped)

        # 과거 시간으로 tripped_at 조작 (0.2초 전)
        self.live_breaker.tripped_at = datetime.now() - timedelta(seconds=0.2)

        # 헬스체크 통과 시 자동 복구 검증
        recovered = self.live_breaker.attempt_recovery(health_check_fn=lambda: True)
        self.assertTrue(recovered)
        self.assertFalse(self.live_breaker.is_tripped)
        self.assertEqual(self.live_breaker.consecutive_order_errors, 0)

    # TEST I: 장외시간 PositionManager -> Exit Condition 기록 가능 -> SELL broker request 0건
    def test_I_off_hours_exit_condition_logged_no_broker_call(self):
        pos = self._open_test_position(self.live_pos_mgr, entry_price=10000.0, qty=10)
        off_hours_dt = datetime(2026, 9, 16, 23, 43, 0)

        # 스톱로스 발동 조건 (가격 9,000 <= stop_price 9,500)
        self.live_pos_mgr.update_price_and_manage(self.sym_code, 9000, off_hours_dt)

        # persistence manager에 스톱 트리거는 영구 기록되어야 함
        self.assertEqual(len(self.live_pm_store.stop_triggers), 1)
        self.assertEqual(self.live_pm_store.stop_triggers[0]["sym"], self.sym_code)

        # 하지만 브로커 매도 주문은 0건이어야 함
        self.assertEqual(len(self.live_client.sell_calls), 0)

    # TEST J: 정규 거래시간 -> 기존 SELL/Stop/Target/Trailing 로직 정상
    def test_J_regular_market_hours_exit_logic_normal(self):
        pos = self._open_test_position(self.live_pos_mgr, entry_price=10000.0, qty=10)
        reg_time = datetime(2026, 9, 17, 10, 30, 0)

        # 정규장 중 +1R 익절 도달 (10,600 >= 10,500)
        self.live_pos_mgr.update_price_and_manage(self.sym_code, 10600, reg_time)

        # 정규장 중이므로 정상 브로커 매도 발주 발생 (10주의 30% = 3주)
        self.assertEqual(len(self.live_client.sell_calls), 1)
        self.assertEqual(self.live_client.sell_calls[0]["shares"], 3)
        self.assertTrue(pos.target_1r_taken)
        self.assertEqual(pos.qty, 7)


if __name__ == "__main__":
    unittest.main()
