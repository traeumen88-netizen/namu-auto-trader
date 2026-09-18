"""[FINAL MASTER v16.0] 보유 종목 수 제한 완전 제거 및 자금/위험 기반 매수 통제 단위 테스트
tests/test_unlimited_positions_policy.py

검증 항목:
- TEST 1: 현재 보유 종목 10개 상태에서 정상 BUY 신호 발생 시 BUY_APPROVED = TRUE 및 11번째 주문 생성
- TEST 2: 보유 종목 11개 상태에서 또 다른 정상 BUY 신호 발생 시 BUY_APPROVED = TRUE 및 12번째 주문 생성
- TEST 3: 보유 종목 20개 상태에서도 현금과 Risk가 허용하면 신규 매수를 허용
- TEST 4: 보유 종목 수는 무관하게 가용 현금 부족 시 BUY_APPROVED = FALSE (REASON = INSUFFICIENT_CASH)
- TEST 5: 현금은 충분하지만 Portfolio Risk Limit 초과 시 BUY_APPROVED = FALSE (REASON = PORTFOLIO_RISK_LIMIT)
- TEST 6: 대시보드 및 시작 시 메트릭 표출 검증 (MAX_POSITION_COUNT = UNLIMITED, POSITION_COUNT_BLOCK = FALSE, POSITION_COUNT_CHECK = BYPASSED / NOT_USED)
- TEST 7: 동일 시간대 다중 종목 BUY 신호 동시 평가 및 우선순위 집행
"""

import os
import unittest
from datetime import datetime, timedelta
from typing import Dict, Any, List

from core.models import (
    TradeSignal, OrderSide, TimeHorizon, OrderType, OrderStatus,
    Position, MarketRegime, SymbolInfo
)
from risk.portfolio_risk import PortfolioRiskManager
from risk.portfolio_cash import PortfolioCashManager
from risk.position_sizer import PositionSizer
from risk.circuit_breaker import CircuitBreaker
from risk.trading_firewall import TradingFirewall
from execution.order_router import OrderRouter
from execution.position_manager import PositionManager
import config.settings as settings


class MockClient:
    def __init__(self, cash=100_000_000):
        self.cash = cash

    def buy_limit(self, code, qty, price):
        return {"rt_cd": "0", "ord_no": "ORD_TEST_9999"}

    def buy_market(self, code, qty):
        return {"rt_cd": "0", "ord_no": "ORD_TEST_9999"}

    def sell_market(self, code, qty):
        return {"rt_cd": "0", "ord_no": "ORD_TEST_8888"}

    def sell_limit(self, code, qty, price):
        return {"rt_cd": "0", "ord_no": "ORD_TEST_8888"}


class TestUnlimitedPositionsPolicy(unittest.TestCase):
    def setUp(self):
        self.now = datetime.now()
        self.equity = 100_000_000.0  # 1억 원
        self.client = MockClient(cash=int(self.equity))
        self.cb = CircuitBreaker()
        self.cash_mgr = PortfolioCashManager(initial_cash=self.equity)
        self.order_router = OrderRouter(self.client, self.cb, cash_manager=self.cash_mgr)
        self.pos_mgr = PositionManager(self.order_router)

    def _create_dummy_positions(self, count: int, risk_per_position: float = 10000.0) -> List[Position]:
        """지정된 개수의 더미 활성 포지션을 생성하여 등록"""
        positions = []
        for i in range(1, count + 1):
            code = f"CODE_{i:03d}"
            pos = self.pos_mgr.open_position(
                time_horizon=TimeHorizon.INTRADAY,
                strategy_id="INT_MOMENTUM",
                iem_cd=code,
                name=f"보유종목_{i}",
                qty=10,
                entry_price=50000,
                stop_price=49000,
                target_1r=51000,
                target_2r=52000,
                target_3r=53000,
                initial_risk=risk_per_position
            )
            positions.append(pos)
        return positions

    def test_01_buy_approved_at_10_positions(self):
        """TEST 1: 현재 보유 종목 10개 상태에서 정상 BUY 신호 발생 시 BUY_APPROVED = TRUE 및 11번째 주문 생성"""
        # 1. 10개 포지션 사전 등록 (총 리스크 10만원 = 0.1% of 1억원, Risk PASS)
        self._create_dummy_positions(10, risk_per_position=10000.0)
        self.assertEqual(len(self.pos_mgr.positions), 10)

        # 2. 11번째 종목 BUY 신호 발생
        sig_11 = TradeSignal(
            strategy_id="INT_BREAKOUT",
            time_horizon=TimeHorizon.INTRADAY,
            iem_cd="005930",
            name="삼성전자",
            side=OrderSide.BUY,
            strategy_price=70000,
            stop_price=69000,
            score=88.0,
            reason="10개 보유 상태 11번째 신규 매수 테스트",
            timestamp=self.now,
            target_1r=71000,
            target_2r=72000
        )

        # 3. 리스크 평가 (위험 비율 0.1% -> NORMAL 통과)
        tot_amt, risk_ratio, status = PortfolioRiskManager.calculate_total_open_risk(
            list(self.pos_mgr.positions.values()), self.equity
        )
        self.assertEqual(status, "NORMAL")
        self.assertLess(risk_ratio, 0.03)

        # 4. 수량 산출
        effective_cash = self.cash_mgr.effective_available_cash
        shares, risk_amt, rationale = PositionSizer.calculate_shares(
            sig_11.time_horizon, self.equity, effective_cash, sig_11.strategy_price, sig_11.stop_price
        )
        self.assertGreater(shares, 0)

        # 5. 주문 전 안전점검 (10개 초과라도 개수 단독 Hard Gate 없이 통과)
        passed, msg = self.order_router.run_pre_order_checks(
            sig_11, shares, sig_11.strategy_price, {"cash": effective_cash}, status
        )
        self.assertTrue(passed)
        self.assertIn("POSITION_COUNT_CHECK: BYPASSED / NOT_USED", msg)

        # 6. 주문 발주 및 11번째 포지션 진입
        order = self.order_router.submit_order(
            sig_11, shares, OrderType.LIMIT, sig_11.strategy_price, {"cash": effective_cash}, status
        )
        self.assertIsNotNone(order)
        self.assertEqual(order.status, OrderStatus.FILLED)

        pos_11 = self.pos_mgr.open_position(
            sig_11.time_horizon, sig_11.strategy_id, sig_11.iem_cd, sig_11.name,
            shares, order.filled_avg_price, sig_11.stop_price, sig_11.target_1r, sig_11.target_2r, 0, risk_amt
        )
        # 총 11개 보유 종목 확인
        self.assertEqual(len(self.pos_mgr.positions), 11)

    def test_02_buy_approved_at_11_positions(self):
        """TEST 2: 보유 종목이 11개인 상태에서 또 다른 정상 BUY 신호 발생 시 BUY_APPROVED = TRUE 및 12번째 주문 생성"""
        # 11개 포지션 등록
        self._create_dummy_positions(11, risk_per_position=10000.0)
        self.assertEqual(len(self.pos_mgr.positions), 11)

        sig_12 = TradeSignal(
            strategy_id="INT_MOMENTUM",
            time_horizon=TimeHorizon.INTRADAY,
            iem_cd="000660",
            name="SK하이닉스",
            side=OrderSide.BUY,
            strategy_price=180000,
            stop_price=176000,
            score=90.0,
            reason="11개 보유 상태 12번째 신규 매수 테스트",
            timestamp=self.now,
            target_1r=184000,
            target_2r=188000
        )

        tot_amt, risk_ratio, status = PortfolioRiskManager.calculate_total_open_risk(
            list(self.pos_mgr.positions.values()), self.equity
        )
        self.assertEqual(status, "NORMAL")

        shares, risk_amt, _ = PositionSizer.calculate_shares(
            sig_12.time_horizon, self.equity, self.cash_mgr.effective_available_cash, sig_12.strategy_price, sig_12.stop_price
        )
        self.assertGreater(shares, 0)

        passed, msg = self.order_router.run_pre_order_checks(
            sig_12, shares, sig_12.strategy_price, {"cash": self.cash_mgr.effective_available_cash}, status
        )
        self.assertTrue(passed)

        order = self.order_router.submit_order(
            sig_12, shares, OrderType.LIMIT, sig_12.strategy_price, {"cash": self.cash_mgr.effective_available_cash}, status
        )
        self.assertIsNotNone(order)
        self.assertEqual(order.status, OrderStatus.FILLED)

        self.pos_mgr.open_position(
            sig_12.time_horizon, sig_12.strategy_id, sig_12.iem_cd, sig_12.name,
            shares, order.filled_avg_price, sig_12.stop_price, sig_12.target_1r, sig_12.target_2r, 0, risk_amt
        )
        self.assertEqual(len(self.pos_mgr.positions), 12)

    def test_03_buy_approved_at_20_positions(self):
        """TEST 3: 보유 종목이 20개인 상태에서도 현금과 Risk가 허용하면 신규 매수를 허용 (21번째 매수 승인)"""
        # 20개 포지션 등록 (각 1만원 위험 = 총 20만원 = 0.2% 리스크, 허용 리스크 4% 이내)
        self._create_dummy_positions(20, risk_per_position=10000.0)
        self.assertEqual(len(self.pos_mgr.positions), 20)

        sig_21 = TradeSignal(
            strategy_id="INT_VWAP",
            time_horizon=TimeHorizon.INTRADAY,
            iem_cd="005380",
            name="현대차",
            side=OrderSide.BUY,
            strategy_price=240000,
            stop_price=236000,
            score=86.0,
            reason="20개 보유 상태 21번째 신규 매수 테스트",
            timestamp=self.now,
            target_1r=244000,
            target_2r=248000
        )

        tot_amt, risk_ratio, status = PortfolioRiskManager.calculate_total_open_risk(
            list(self.pos_mgr.positions.values()), self.equity
        )
        self.assertEqual(status, "NORMAL")

        shares, risk_amt, _ = PositionSizer.calculate_shares(
            sig_21.time_horizon, self.equity, self.cash_mgr.effective_available_cash, sig_21.strategy_price, sig_21.stop_price
        )
        self.assertGreater(shares, 0)

        passed, msg = self.order_router.run_pre_order_checks(
            sig_21, shares, sig_21.strategy_price, {"cash": self.cash_mgr.effective_available_cash}, status
        )
        self.assertTrue(passed)

        order = self.order_router.submit_order(
            sig_21, shares, OrderType.LIMIT, sig_21.strategy_price, {"cash": self.cash_mgr.effective_available_cash}, status
        )
        self.assertIsNotNone(order)
        self.assertEqual(order.status, OrderStatus.FILLED)

        self.pos_mgr.open_position(
            sig_21.time_horizon, sig_21.strategy_id, sig_21.iem_cd, sig_21.name,
            shares, order.filled_avg_price, sig_21.stop_price, sig_21.target_1r, sig_21.target_2r, 0, risk_amt
        )
        self.assertEqual(len(self.pos_mgr.positions), 21)

    def test_04_insufficient_cash_blocks_buy(self):
        """TEST 4: 보유 종목 수는 충분/무관하지만 가용 현금이 부족하면 BUY_APPROVED = FALSE (REASON = INSUFFICIENT_CASH)"""
        # 현재 보유 종목 10개, 가용현금 50만원뿐
        self._create_dummy_positions(10, risk_per_position=10000.0)
        self.cash_mgr.cash_available = 500_000.0  # 가용현금 50만 원

        sig_fail = TradeSignal(
            strategy_id="INT_MOMENTUM",
            time_horizon=TimeHorizon.INTRADAY,
            iem_cd="012450",
            name="한화에어로스페이스",
            side=OrderSide.BUY,
            strategy_price=300000,
            stop_price=295000,
            score=91.0,
            reason="현금 부족 차단 테스트",
            timestamp=self.now,
            target_1r=305000,
            target_2r=310000
        )

        # 40주(1,200만원 소요) 또는 2주(60만원 소요 > 50만원 가용)
        # PositionSizer 계산 시 가용현금 부족으로 shares = 0 또는 에러
        shares, risk_amt, reason = PositionSizer.calculate_shares(
            sig_fail.time_horizon, self.equity, self.cash_mgr.effective_available_cash, sig_fail.strategy_price, sig_fail.stop_price
        )
        self.assertEqual(shares, 0)
        self.assertIn("INSUFFICIENT_CASH", reason)

        # OrderRouter에서 1주(30만원)를 강제 시도하더라도 cash_manager에 의해 초과 시 차단
        self.cash_mgr.cash_available = 200_000.0  # 1주 30만원보다 적은 20만원
        passed, msg = self.order_router.run_pre_order_checks(
            sig_fail, shares=1, order_price=300000, balance={"cash": 200000}, portfolio_risk_status="NORMAL"
        )
        self.assertFalse(passed)
        self.assertIn("INSUFFICIENT_CASH", msg)

    def test_05_portfolio_risk_limit_blocks_buy(self):
        """TEST 5: 현금은 충분하지만 Portfolio Risk Limit 초과 시 BUY_APPROVED = FALSE (REASON = PORTFOLIO_RISK_LIMIT)"""
        # 현금은 1억원으로 충분하나, 기존 포지션들의 리스크 합계가 450만원 (4.5% > 4.0% 리스크 천장 초과)
        self.cash_mgr.cash_available = 100_000_000.0
        # 5개 포지션이 각각 90만원씩 위험 노출 (총 450만원 = 4.5%)
        self._create_dummy_positions(5, risk_per_position=900_000.0)

        tot_amt, risk_ratio, status = PortfolioRiskManager.calculate_total_open_risk(
            list(self.pos_mgr.positions.values()), self.equity
        )
        self.assertEqual(status, "BLOCKED")
        self.assertGreater(risk_ratio, 0.04)

        sig_risk_blocked = TradeSignal(
            strategy_id="INT_MOMENTUM",
            time_horizon=TimeHorizon.INTRADAY,
            iem_cd="034020",
            name="두산에너빌리티",
            side=OrderSide.BUY,
            strategy_price=20000,
            stop_price=19600,
            score=89.0,
            reason="리스크 한도 초과 차단 테스트",
            timestamp=self.now,
            target_1r=20400,
            target_2r=20800
        )

        passed, msg = self.order_router.run_pre_order_checks(
            sig_risk_blocked, shares=100, order_price=20000, balance={"cash": 100_000_000}, portfolio_risk_status=status
        )
        self.assertFalse(passed)
        self.assertEqual(msg, "PORTFOLIO_RISK_LIMIT")

    def test_06_dashboard_and_startup_metrics(self):
        """TEST 6: 종목 수 제한 여부 자동 검증 메트릭 표시 확인"""
        summary = self.cash_mgr.get_dashboard_summary(open_positions_count=12)
        self.assertEqual(summary["max_position_count"], "UNLIMITED")
        self.assertEqual(summary["current_position_count"], 12)
        self.assertFalse(summary["position_count_block"])
        self.assertEqual(summary["position_count_check"], "BYPASSED / NOT_USED")

        dash_text = self.cash_mgr.format_dashboard_text(open_positions_count=12)
        self.assertIn("MAX_POSITION_COUNT      :      UNLIMITED", dash_text)
        self.assertIn("CURRENT_POSITION_COUNT  :             12개", dash_text)
        self.assertIn("POSITION_COUNT_BLOCK    :          FALSE", dash_text)
        self.assertIn("POSITION_COUNT_CHECK    : BYPASSED / NOT_USED", dash_text)

    def test_07_concurrent_multi_signal_evaluation(self):
        """TEST 7: 동일 시간대 여러 종목의 BUY 신호 동시 평가 및 우선순위 집행"""
        # 5개 종목 신호 생성 (각 100주 @ 50,000 = 500만원; 5종목 총 2,500만원 < 1억원)
        signals = []
        codes = ["005930", "000660", "005380", "012450", "034020"]
        names = ["삼성전자", "SK하이닉스", "현대차", "한화에어로", "두산에너빌"]

        class MockEdge:
            def __init__(self, exp_r, entry_p):
                self.expected_net_r = exp_r
                self.entry_price = entry_p

        class MockMeta:
            def __init__(self, p_target):
                self.p_target = p_target
                self.decision = "BUY"

        for i in range(5):
            sig = TradeSignal(
                strategy_id=f"INT_STRAT_{i}",
                time_horizon=TimeHorizon.INTRADAY,
                iem_cd=codes[i],
                name=names[i],
                side=OrderSide.BUY,
                strategy_price=50000,
                stop_price=49000,  # 2% stop <= 3.5% 한도
                score=80.0 + i * 2,
                reason="Multi Signal",
                timestamp=self.now,
                target_1r=51000,
                target_2r=52000
            )
            signals.append((sig, None, MockEdge(0.20 + i * 0.05, 50000), MockMeta(0.70)))

        # 우선순위 정렬 (Expected Net R 내림차순)
        sorted_sigs = PortfolioCashManager.sort_signals_by_priority(signals)
        self.assertEqual(sorted_sigs[0][0].name, "두산에너빌")  # 0.40R로 최고

        # 가용 현금과 위험 한도 내에서 5개 전원 정상 평가 및 집행 가능 확인 (종목수 제한 없이 모두 승인)
        approved = 0
        for sig, _, edge, meta in sorted_sigs:
            # 40주 @ 50,000원 = 200만원씩 자금 예약 및 집행
            shares = 40
            ok, _, req, _ = self.cash_mgr.revalidate_and_reserve_cash(
                f"ORD_{sig.iem_cd}", sig, shares, edge.entry_price
            )
            if ok:
                approved += 1
                self.cash_mgr.on_order_fill(f"ORD_{sig.iem_cd}", shares, edge.entry_price)

        self.assertEqual(approved, 5)


if __name__ == "__main__":
    unittest.main()
