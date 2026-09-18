"""[FINAL MASTER v16.0] 구매 종목 수 제한 해제 및 현금 기반 매수 통제 정책 단위 테스트
tests/test_portfolio_cash_governance.py

검증 항목:
1. 종목 개수 인위적 제한 완전 해제: 충분한 현금이 있으면 여러 종목 동시 매수 가능
2. 현금 계산 정밀성: required_order_value + fee + tax + slippage = total_required_cash
3. 현금 부족 시 동작: 마이너스 잔고/신용 금지, NO_TRADE, REASON=INSUFFICIENT_CASH
4. 주문 수량 산출: Risk 기반 기본 수량 및 Cash 정밀 제약
5. 현금 부족 시 강제 부분 축소 매수 금지 (ALLOW_PARTIAL_CASH_BUY = False)
6. 주문 직전 최종 Cash Revalidation 검증
7. 중앙 Cash Reservation: 동시 다중 주문 시 총합 현금 초과 방지
8. 주문 우선순위: 다중 신호 시 Expected Net Return, Score, ML Prob 순 자금 배분
9. 현금 부족 감사 로그: CashShortfallRecord 필드 무결성 및 영구 저장
10. Dashboard 메트릭 표출 포맷 검증
"""

import os
import unittest
from datetime import datetime
from core.models import TradeSignal, OrderSide, TimeHorizon, OrderType, OrderStatus
from risk.portfolio_cash import PortfolioCashManager, CashRequirement, CashShortfallRecord
from risk.position_sizer import PositionSizer
from execution.persistence_manager import PersistenceManager
import config.settings as settings


class TestPortfolioCashGovernance(unittest.TestCase):
    def setUp(self):
        self.now = datetime.now()
        self.test_dir = "data/test_cash_tmp"
        os.makedirs(self.test_dir, exist_ok=True)
        self.db_path = os.path.join(self.test_dir, "test_cash.db")
        self.cold_dir = os.path.join(self.test_dir, "test_cold")
        self.persistence = PersistenceManager(db_path=self.db_path, cold_dir=self.cold_dir)

    def tearDown(self):
        import shutil
        if os.path.exists(self.test_dir):
            try:
                shutil.rmtree(self.test_dir)
            except Exception:
                pass

    def test_01_unlimited_positions_with_sufficient_cash(self):
        """1. 종목 수 인위적 제한 해제: 현금이 충분하면 15개 종목도 연속 매수 승인"""
        cash_mgr = PortfolioCashManager(initial_cash=100_000_000.0) # 1억 원
        approved_count = 0

        # 15개 서로 다른 종목 매수 시도 (각 200만 원)
        for i in range(15):
            sig = TradeSignal(
                strategy_id="INT_MOMENTUM", time_horizon=TimeHorizon.INTRADAY,
                iem_cd=f"CODE_{i:03d}", name=f"종목_{i}", side=OrderSide.BUY,
                strategy_price=50000, stop_price=49000, score=85.0,
                reason="Test", timestamp=self.now, target_1r=51000, target_2r=52000
            )
            # 40주 @ 50,000원 = 200만 원
            ok, msg, req, _ = cash_mgr.revalidate_and_reserve_cash(f"ORD_{i}", sig, shares=40, order_price=50000)
            if ok:
                approved_count += 1
                cash_mgr.on_order_fill(f"ORD_{i}", filled_qty=40, filled_price=50000)

        # 15개 종목 전체 매수 승인 확인 (인위적 5개, 10개 컷오프 없음)
        self.assertEqual(approved_count, 15)
        self.assertLess(cash_mgr.cash_available, 75_000_000.0)

    def test_02_accurate_cash_requirement_calculation(self):
        """2. 수수료 및 슬리피지 버퍼 포함 총 필요 현금 정밀 계산"""
        # 10주 @ 70,000원 = 700,000원
        # fee (0.015%): 105원
        # tax (0%): 0원
        # slippage buffer (0.05%): 350원
        # total: 700,455원
        req = PortfolioCashManager.calculate_total_required_cash(shares=10, order_price=70000)
        self.assertEqual(req.required_order_value, 700000.0)
        self.assertAlmostEqual(req.estimated_fee, 105.0, delta=0.01)
        self.assertEqual(req.estimated_tax, 0.0)
        self.assertAlmostEqual(req.estimated_slippage, 350.0, delta=0.01)
        self.assertAlmostEqual(req.total_required_cash, 700455.0, delta=0.01)

    def test_03_insufficient_cash_blocks_buy_strictly(self):
        """3. 현금 부족 시 마이너스/신용 잔고 금지 및 INSUFFICIENT_CASH 기각"""
        cash_mgr = PortfolioCashManager(initial_cash=500_000.0) # 가용 현금 50만 원

        sig = TradeSignal(
            strategy_id="INT_BREAKOUT", time_horizon=TimeHorizon.INTRADAY,
            iem_cd="005930", name="삼성전자", side=OrderSide.BUY,
            strategy_price=70000, stop_price=69000, score=88.0,
            reason="Test", timestamp=self.now, target_1r=71000, target_2r=72000
        )
        # 10주 필요금액 약 700,455원 > 가용 500,000원
        ok, msg, req, shortfall_rec = cash_mgr.revalidate_and_reserve_cash("ORD_FAIL", sig, shares=10, order_price=70000)

        self.assertFalse(ok)
        self.assertEqual(msg, "INSUFFICIENT_CASH")
        self.assertIsNotNone(shortfall_rec)
        self.assertEqual(shortfall_rec.decision, "NO_TRADE")
        self.assertEqual(shortfall_rec.reason, "INSUFFICIENT_CASH")
        self.assertGreater(shortfall_rec.cash_shortfall, 200000.0)
        # 가용 현금은 차감되지 않고 온전히 보존되어야 함
        self.assertEqual(cash_mgr.effective_available_cash, 500000.0)

    def test_04_position_sizer_respects_no_partial_cash_buy(self):
        """4 & 5. ALLOW_PARTIAL_CASH_BUY = False 시 강제 축소 매수 금지 확인"""
        # Risk 기준 계산 시 500주(3,500만원 필요)이나 가용현금은 1,000만원뿐인 경우
        equity = 100_000_000.0
        available_cash = 10_000_000.0

        # 기본 정책 (ALLOW_PARTIAL_CASH_BUY = False)
        settings.ALLOW_PARTIAL_CASH_BUY = False
        shares, risk, reason = PositionSizer.calculate_shares(
            TimeHorizon.INTRADAY, equity, available_cash, 70000, 69000
        )
        # 3,500만원이 필요한데 1,000만원만 있다고 해서 142주로 강제 축소 매수하지 않고 0주 및 기각이어야 함
        self.assertEqual(shares, 0)
        self.assertIn("INSUFFICIENT_CASH", reason)

    def test_05_central_cash_reservation_multi_order_concurrency(self):
        """7. 동시 다중 주문 시 중앙 Cash Reservation으로 전체 현금 초과 발주 원천 차단"""
        # 가용현금 1,000만원
        cash_mgr = PortfolioCashManager(initial_cash=10_000_000.0)

        sig_a = TradeSignal(
            strategy_id="INT_MOMENTUM", time_horizon=TimeHorizon.INTRADAY,
            iem_cd="005930", name="종목A", side=OrderSide.BUY,
            strategy_price=60000, stop_price=59000, score=90.0,
            reason="Test", timestamp=self.now, target_1r=61000, target_2r=62000
        )
        sig_b = TradeSignal(
            strategy_id="INT_BREAKOUT", time_horizon=TimeHorizon.INTRADAY,
            iem_cd="000660", name="종목B", side=OrderSide.BUY,
            strategy_price=50000, stop_price=49000, score=85.0,
            reason="Test", timestamp=self.now, target_1r=51000, target_2r=52000
        )

        # 주문 A: 100주 @ 60,000원 = 600만 원 (통과)
        ok_a, msg_a, req_a, _ = cash_mgr.revalidate_and_reserve_cash("ORD_A", sig_a, shares=100, order_price=60000)
        self.assertTrue(ok_a)
        self.assertAlmostEqual(cash_mgr.reserved_cash, 6003900.0, delta=100)
        self.assertAlmostEqual(cash_mgr.effective_available_cash, 3996100.0, delta=100)

        # 주문 B: 100주 @ 50,000원 = 500만 원 (개별로는 1000만원 안에 들지만, A 예약 후 잔여 400만원 미만이므로 차단되어야 함)
        ok_b, msg_b, req_b, short_b = cash_mgr.revalidate_and_reserve_cash("ORD_B", sig_b, shares=100, order_price=50000)
        self.assertFalse(ok_b)
        self.assertEqual(msg_b, "INSUFFICIENT_CASH")
        self.assertGreater(short_b.cash_shortfall, 1000000.0)

        # 주문 A 취소 시 예약금 환원 검증
        cash_mgr.on_order_cancel_or_reject("ORD_A")
        self.assertEqual(cash_mgr.reserved_cash, 0.0)
        self.assertEqual(cash_mgr.effective_available_cash, 10_000_000.0)

        # 환원 후 다시 주문 B 시도 -> 이제는 통과
        ok_b2, _, _, _ = cash_mgr.revalidate_and_reserve_cash("ORD_B2", sig_b, shares=100, order_price=50000)
        self.assertTrue(ok_b2)

    def test_06_order_priority_sorting(self):
        """8. 다중 매수 신호 발생 시 주문 우선순위(Expected Net R > Score > ML Prob) 정렬 검증"""
        sig_low = TradeSignal(
            strategy_id="INT_MOMENTUM", time_horizon=TimeHorizon.INTRADAY,
            iem_cd="001", name="낮은우선순위", side=OrderSide.BUY,
            strategy_price=10000, stop_price=9800, score=65.0,
            reason="Test", timestamp=self.now, target_1r=10200, target_2r=10400
        )
        sig_mid = TradeSignal(
            strategy_id="INT_BREAKOUT", time_horizon=TimeHorizon.INTRADAY,
            iem_cd="002", name="중간우선순위", side=OrderSide.BUY,
            strategy_price=20000, stop_price=19600, score=80.0,
            reason="Test", timestamp=self.now, target_1r=20400, target_2r=20800
        )
        sig_high = TradeSignal(
            strategy_id="INT_VWAP", time_horizon=TimeHorizon.INTRADAY,
            iem_cd="003", name="최상위우선순위", side=OrderSide.BUY,
            strategy_price=30000, stop_price=29400, score=92.0,
            reason="Test", timestamp=self.now, target_1r=30600, target_2r=31200
        )

        class MockEdge:
            def __init__(self, exp_r):
                self.expected_net_r = exp_r

        class MockMeta:
            def __init__(self, p_target):
                self.p_target = p_target

        signals = [
            (sig_low, None, MockEdge(0.08), MockMeta(0.55)),
            (sig_high, None, MockEdge(0.45), MockMeta(0.82)),
            (sig_mid, None, MockEdge(0.25), MockMeta(0.70))
        ]

        sorted_signals = PortfolioCashManager.sort_signals_by_priority(signals)
        # 최상위(Expected Net R=0.45)가 1위여야 함
        self.assertEqual(sorted_signals[0][0].name, "최상위우선순위")
        self.assertEqual(sorted_signals[1][0].name, "중간우선순위")
        self.assertEqual(sorted_signals[2][0].name, "낮은우선순위")

    def test_07_cash_shortfall_audit_logging(self):
        """9. 현금 부족 기각 시 영구 감사 로그(cash_shortfall_records) 저장 무결성 검증"""
        rec = CashShortfallRecord(
            symbol="005930",
            signal_id="SIG_005930_12345",
            strategy_id="INT_BREAKOUT",
            decision="NO_TRADE",
            cash_available=3200000.0,
            reserved_cash=0.0,
            effective_available_cash=3200000.0,
            required_order_value=4150000.0,
            estimated_cost=2718.0,
            cash_shortfall=952718.0,
            timestamp=self.now.isoformat(),
            reason="INSUFFICIENT_CASH"
        )

        self.persistence.record_cash_shortfall(rec)

        # WARM DB에서 조회 검증
        with self.persistence._get_conn() as conn:
            cursor = conn.execute("SELECT * FROM cash_shortfall_records WHERE iem_cd = ?", ("005930",))
            row = cursor.fetchone()
            self.assertIsNotNone(row)
            self.assertEqual(row["reason"], "INSUFFICIENT_CASH")
            self.assertEqual(row["decision"], "NO_TRADE")
            self.assertAlmostEqual(row["cash_shortfall"], 952718.0, delta=1.0)

    def test_08_dashboard_summary_formatting(self):
        """10. 대시보드 실시간 현금 및 주문 상태 표출 텍스트 포맷 검증"""
        cash_mgr = PortfolioCashManager(initial_cash=10_000_000.0)
        cash_mgr.reserved_cash = 3_500_000.0
        cash_mgr.reservations["ORD_TEST_1"] = {"amount": 2_000_000.0}
        cash_mgr.reservations["ORD_TEST_2"] = {"amount": 1_500_000.0}

        dash_text = cash_mgr.format_dashboard_text(open_positions_count=7)
        self.assertIn("PORTFOLIO CASH & EXECUTION STATUS", dash_text)
        self.assertIn("10,000,000", dash_text)
        self.assertIn("3,500,000", dash_text)
        self.assertIn("6,500,000", dash_text)
        self.assertIn("7", dash_text)
        self.assertIn("2", dash_text)


if __name__ == "__main__":
    unittest.main()
