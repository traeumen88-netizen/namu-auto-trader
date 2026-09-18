# -*- coding: utf-8 -*-
"""
[UNIT TEST SUITE] tests/test_after_hours_governance.py
=============================================================================
장후 시간외 급등 감지, 익일 관찰 후보 등록, 정규장 재검증 및 세션 태깅 거버넌스 검증 테스트 (8대 필수 테스트)

1. test_after_hours_spike_no_direct_buy:
   - 시간외 급등(+8%) 발생 시 브로커로 BUY 주문이 직접 전송되지 않고 차단됨을 검증
2. test_after_hours_watchlist_promotion:
   - 시간외 급등 종목이 NEXT_SESSION_WATCHLIST에 정상 등록되고 사전 특징값(AFTER_HOURS_RETURN 등)이 보존됨을 검증
3. test_next_session_revalidation:
   - 익일 정규장 개장 후 전략 Setup 조건 미충족 시 NO_TRADE로 정상 차단됨을 검증
4. test_gap_up_rejection:
   - 시초가 과도한 갭상승(+12%) 후 거래량 고갈/매도세 우세 시 Overextended/Exhaustion Gap으로 NO_TRADE 차단 검증
5. test_gap_up_valid_entry:
   - 적정 갭상승(+4%) + 거래량 유입 + VWAP 지지 + 전략 Setup + ML/Edge 통과 시 정상 BUY 승인 검증
6. test_after_hours_overnight_position_block:
   - 시간외 급등 신호만으로 신규 오버나잇 포지션 생성이 엄격히 차단됨을 검증
7. test_after_hours_regular_entry:
   - 시간외 급등 -> 익일 정규장 승인 -> 매수 체결 태깅 (AFTER_HOURS_SIGNAL / REGULAR_ENTRY) 전 과정 검증
8. test_after_hours_tagging:
   - 거래 세션 태깅 체계 검증 및 AFTER_HOURS_SIGNAL / AFTER_HOURS_ENTRY 발생 시 치명적 경고 및 주문 차단 검증
=============================================================================
"""

import unittest
import os
import shutil
from datetime import datetime, time, timedelta
from unittest.mock import MagicMock

from core.models import TradeSignal, Order, Position, TimeHorizon, OrderSide, OrderType, OrderStatus
from core.after_hours_manager import (
    AfterHoursManager,
    MarketSessionManager,
    MarketSession,
    AfterHoursCandidate,
    AfterHoursOutcomeRecord
)
from execution.order_router import OrderRouter
from execution.position_manager import PositionManager


class TestAfterHoursGovernance(unittest.TestCase):

    def setUp(self):
        self.test_dir = "data/test_ah_db"
        os.makedirs(self.test_dir, exist_ok=True)
        self.db_path = os.path.join(self.test_dir, "test_ah_operational.db")
        self.mock_notifier = MagicMock()
        self.manager = AfterHoursManager(db_path=self.db_path, telegram_notifier=self.mock_notifier)

        # Router & Client setup
        self.mock_client = MagicMock()
        self.mock_client.dry_run = True
        self.mock_cb = MagicMock()
        self.mock_cb.is_tripped = False
        self.router = OrderRouter(namu_client=self.mock_client, circuit_breaker=self.mock_cb)

    def tearDown(self):
        if os.path.exists(self.test_dir):
            shutil.rmtree(self.test_dir, ignore_errors=True)

    def test_after_hours_spike_no_direct_buy(self):
        """
        1. test_after_hours_spike_no_direct_buy
        - 시간외 급등(+8%) 발생 시 브로커로 BUY 주문 전송되지 않음 검증
        """
        # 16:30 시간외 세션 시뮬레이션
        ah_time = datetime(2026, 9, 14, 16, 30, 0)
        session = MarketSessionManager.get_market_session(ah_time)
        self.assertEqual(session, MarketSession.AFTER_HOURS)

        # 시간외 급등 감지 (10,000 -> 10,800: +8.0%)
        cand = self.manager.detect_after_hours_spike(
            iem_cd="490470",
            name="세미파이브",
            regular_close=10000.0,
            current_price=10800.0,
            volume=25000,
            turnover=270000000,
            dt=ah_time
        )
        self.assertIsNotNone(cand)
        self.assertIn(cand.status, ("ACTIVE", "WATCHLIST"))

        # 시간외 세션에서는 신규 BUY 주문 발주가 허용되지 않아야 함
        allowed, reason = MarketSessionManager.is_order_entry_allowed(TimeHorizon.INTRADAY, dt=ah_time, is_buy=True)
        self.assertFalse(allowed)
        self.assertIn("BLOCK_NON_REGULAR_SESSION", reason)

        # Router 주문 전 사전 점검에서도 차단되어야 함
        sig = TradeSignal(
            strategy_id="INT_VWAP_PULLBACK",
            time_horizon=TimeHorizon.INTRADAY,
            iem_cd="490470",
            name="세미파이브",
            side=OrderSide.BUY,
            strategy_price=10800,
            stop_price=10500,
            score=85.0,
            reason="AFTER_HOURS_MOMENTUM",
            timestamp=ah_time
        )
        passed, chk_msg = self.router.run_pre_order_checks(
            signal=sig,
            shares=10,
            order_price=10800,
            balance={"cash": 10000000},
            portfolio_risk_status="NORMAL",
            now=ah_time
        )
        self.assertFalse(passed)
        self.assertIn("BLOCK_NON_REGULAR_SESSION", chk_msg)

        # submit_order 수행 시 None 반환 및 브로커 전송 0건 검증
        order = self.router.submit_order(sig, shares=10, now=ah_time)
        self.assertIsNone(order)
        self.mock_client.send_order.assert_not_called()

    def test_after_hours_watchlist_promotion(self):
        """
        2. test_after_hours_watchlist_promotion
        - 시간외 급등 시 NEXT_SESSION_WATCHLIST에 등록 및 AFTER_HOURS_RETURN 등 속성 보존 검증
        """
        ah_time = datetime(2026, 9, 14, 17, 10, 0)
        cand = self.manager.detect_after_hours_spike(
            iem_cd="005930",
            name="삼성전자",
            regular_close=70000.0,
            current_price=74550.0,  # +6.5%
            volume=50000,
            turnover=3727500000,
            regime="BULL",
            dt=ah_time
        )
        self.assertIsNotNone(cand)
        self.assertTrue(cand.next_session_watchlist)
        self.assertEqual(cand.after_hours_return, 6.5)
        self.assertEqual(cand.spike_bracket, "+5%~+10%")

        # 익일 정규장을 위한 사전 특징값(Prior Feature / Context) 보존 확인
        feats = self.manager.get_next_session_features("005930")
        self.assertTrue(feats["AFTER_HOURS_SIGNAL"])
        self.assertEqual(feats["AFTER_HOURS_RETURN"], 6.5)
        self.assertEqual(feats["AFTER_HOURS_VOLUME"], 50000)
        self.assertEqual(feats["AFTER_HOURS_REGIME"], "BULL")
        self.assertTrue(feats["NEXT_SESSION_WATCHLIST"])

    def test_next_session_revalidation(self):
        """
        3. test_next_session_revalidation
        - 익일 정규장 조건 미충족 시(거래량 미달, 셋업 미충족 등) NO_TRADE 처리 및 주문 미발주 검증
        """
        # 전일 시간외 등록
        cand = self.manager.detect_after_hours_spike(
            iem_cd="042700",
            name="한미반도체",
            regular_close=50000.0,
            current_price=53000.0,  # +6.0%
            volume=10000,
            dt=datetime(2026, 9, 14, 16, 50, 0)
        )

        # 익일 09:15 정규장 개장
        reg_time = datetime(2026, 9, 15, 9, 15, 0)
        self.assertEqual(MarketSessionManager.get_market_session(reg_time), MarketSession.REGULAR)

        # 전략 Setup 미충족 (예: 거래량 유입 부족, 눌림목 지지 실패)
        approved, reason = self.manager.revalidate_in_regular_session(
            iem_cd="042700",
            open_price=52000.0,
            prev_close=50000.0,
            current_price=51800.0,
            vwap=52200.0,       # 현재가가 VWAP을 하회
            rvol=0.8,           # 거래량 미달
            setup_pass=False,   # 전략 셋업 미충족
            setup_name="INT_VWAP_PULLBACK",
            ml_approved=True,
            edge_approved=True,
            risk_approved=True,
            now=reg_time
        )
        self.assertFalse(approved)
        self.assertTrue("VWAP_SUPPORT_FAILED" in reason or "REGULAR_SETUP_NOT_SATISFIED" in reason)

        # Candidate 상태가 NO_TRADE로 업데이트되었는지 확인
        updated_cand = self.manager.watchlist["042700"]
        self.assertTrue(updated_cand.next_day_revalidated)
        self.assertEqual(updated_cand.next_day_decision, "NO_TRADE")

    def test_gap_up_rejection(self):
        """
        4. test_gap_up_rejection
        - 시초가 과도한 갭상승(+12%) 후 수급 이탈 시 NO_TRADE (Overextended/Exhaustion Gap) 차단 검증
        """
        reg_time = datetime(2026, 9, 15, 9, 5, 0)

        # 전일 종가 10,000원 -> 시초가 11,200원 (+12% 극단적 갭상승)
        # 하지만 거래량 고갈 (RVOL 0.7), 음봉 매도세 (VWAP 하회)
        valid, reason = self.manager.evaluate_next_session_gap(
            iem_cd="123450",
            open_price=11200.0,
            prev_close=10000.0,
            current_price=11000.0,
            vwap=11150.0,
            rvol=0.7,
            is_selling_pressure=True
        )
        self.assertFalse(valid)
        self.assertIn("OVEREXTENDED_GAP_EXHAUSTION", reason)

    def test_gap_up_valid_entry(self):
        """
        5. test_gap_up_valid_entry
        - 적정 갭상승(+4%) + 거래량 유입 + VWAP 지지 + 전략 Setup + ML/Edge 통과 시 정상 매수 승인 검증
        """
        # 전일 시간외 등록
        self.manager.detect_after_hours_spike(
            iem_cd="000660",
            name="SK하이닉스",
            regular_close=150000.0,
            current_price=157500.0,  # +5.0%
            volume=30000,
            dt=datetime(2026, 9, 14, 17, 0, 0)
        )

        reg_time = datetime(2026, 9, 15, 9, 10, 0)
        # 익일: 시초가 +4% (156,000), 현재가 156,500, VWAP 156,200 지지, RVOL 2.5
        approved, reason = self.manager.revalidate_in_regular_session(
            iem_cd="000660",
            open_price=156000.0,
            prev_close=150000.0,
            current_price=156500.0,
            vwap=156200.0,
            rvol=2.5,
            setup_pass=True,
            setup_name="INT_VWAP_PULLBACK",
            ml_approved=True,
            edge_approved=True,
            risk_approved=True,
            is_selling_pressure=False,
            now=reg_time
        )
        self.assertTrue(approved)
        self.assertIn("REGULAR_REVALIDATION_PASSED", reason)
        self.assertEqual(self.manager.watchlist["000660"].next_day_decision, "BUY_APPROVED")

    def test_after_hours_overnight_position_block(self):
        """
        6. test_after_hours_overnight_position_block
        - 시간외 급등으로 인한 신규 오버나잇 포지션 생성 차단 검증
        """
        # 시간외 신호만으로 오버나잇 포지션 생성 시도 -> 차단 판정
        allowed, reason = MarketSessionManager.is_overnight_position_allowed(after_hours_signal=True)
        self.assertFalse(allowed)
        self.assertIn("BLOCK_OVERNIGHT_FROM_AFTER_HOURS", reason)

        # 18:03 장 마감 후 주문 라우팅 시도
        late_time = datetime(2026, 9, 14, 18, 3, 42)
        sig = TradeSignal(
            strategy_id="INT_VWAP_PULLBACK",
            time_horizon=TimeHorizon.INTRADAY,
            iem_cd="490470",
            name="세미파이브",
            side=OrderSide.BUY,
            strategy_price=15000,
            stop_price=14700,
            score=90.0,
            reason="OVERNIGHT_ATTEMPT",
            timestamp=late_time
        )
        passed, msg = self.router.run_pre_order_checks(
            signal=sig,
            shares=34,
            order_price=15000,
            balance={"cash": 10000000},
            portfolio_risk_status="NORMAL",
            now=late_time
        )
        self.assertFalse(passed)
        self.assertIn("BLOCK_NON_REGULAR_SESSION", msg)

    def test_after_hours_regular_entry(self):
        """
        7. test_after_hours_regular_entry
        - 시간외 급등 -> 익일 정규장 승인 -> 매수 체결 태깅 (AFTER_HOURS_SIGNAL / REGULAR_ENTRY) 전 과정 검증
        """
        # 1. 시간외 급등 등록
        self.manager.detect_after_hours_spike(
            iem_cd="051910",
            name="LG화학",
            regular_close=300000.0,
            current_price=318000.0,  # +6.0%
            volume=8000,
            dt=datetime(2026, 9, 14, 16, 20, 0)
        )

        # 2. 익일 정규장 재검증 통과 (09:20)
        reg_time = datetime(2026, 9, 15, 9, 20, 0)
        approved, _ = self.manager.revalidate_in_regular_session(
            iem_cd="051910",
            open_price=310000.0,
            prev_close=300000.0,
            current_price=312000.0,
            vwap=311000.0,
            rvol=2.0,
            setup_pass=True,
            setup_name="INT_BREAKOUT",
            ml_approved=True,
            edge_approved=True,
            risk_approved=True,
            now=reg_time
        )
        self.assertTrue(approved)

        # 3. 정규장 신호 생성 및 세션 태깅 부여
        sig = TradeSignal(
            strategy_id="INT_BREAKOUT",
            time_horizon=TimeHorizon.INTRADAY,
            iem_cd="051910",
            name="LG화학",
            side=OrderSide.BUY,
            strategy_price=312000,
            stop_price=307000,
            score=92.0,
            reason="REGULAR_ENTRY_AFTER_HOURS_PRIOR",
            timestamp=reg_time,
            signal_session="AFTER_HOURS",
            entry_session="REGULAR"
        )

        # 4. 정규장 주문 집행 검증
        passed, msg = self.router.run_pre_order_checks(
            signal=sig,
            shares=10,
            order_price=312000,
            balance={"cash": 50000000},
            portfolio_risk_status="NORMAL",
            now=reg_time
        )
        self.assertTrue(passed)

        order = self.router.submit_order(sig, shares=10, now=reg_time)
        self.assertIsNotNone(order)
        self.assertEqual(order.signal_session, "AFTER_HOURS")
        self.assertEqual(order.entry_session, "REGULAR")

        # 5. 포지션 관리자 등록 및 태깅 보존 확인
        pos_mgr = PositionManager(order_router=self.router)
        pos = pos_mgr.open_position(
            time_horizon=sig.time_horizon,
            strategy_id=sig.strategy_id,
            iem_cd=sig.iem_cd,
            name=sig.name,
            qty=10,
            entry_price=312000.0,
            stop_price=307000,
            target_1r=317000,
            target_2r=322000,
            target_3r=327000,
            initial_risk=50000.0,
            signal_session=sig.signal_session,
            entry_session=order.entry_session
        )
        self.assertEqual(pos.signal_session, "AFTER_HOURS")
        self.assertEqual(pos.entry_session, "REGULAR")

    def test_after_hours_tagging(self):
        """
        8. test_after_hours_tagging
        - 거래 세션 태깅 체계 검증 및 AFTER_HOURS_SIGNAL / AFTER_HOURS_ENTRY 발생 시 치명적 경고 및 주문 차단 검증
        """
        # 정상 태깅 조합
        v1, msg1 = AfterHoursManager.validate_trade_tagging("REGULAR", "REGULAR")
        self.assertTrue(v1)
        self.assertIn("REGULAR_SIGNAL / REGULAR_ENTRY", msg1)

        v2, msg2 = AfterHoursManager.validate_trade_tagging("AFTER_HOURS", "REGULAR")
        self.assertTrue(v2)
        self.assertIn("AFTER_HOURS_SIGNAL / REGULAR_ENTRY", msg2)

        # 비정상 금지 조합: 시간외 급등 신호로 시간외에 직접 진입 (AFTER_HOURS / AFTER_HOURS)
        v3, msg3 = AfterHoursManager.validate_trade_tagging("AFTER_HOURS", "AFTER_HOURS")
        self.assertFalse(v3)
        self.assertIn("CRITICAL_SESSION_VIOLATION", msg3)

        # 시간외 시각에 AFTER_HOURS_SIGNAL 주문 발주 시 Router가 차단하는지 검증
        ah_time = datetime(2026, 9, 14, 16, 45, 0)
        sig = TradeSignal(
            strategy_id="INT_VWAP_PULLBACK",
            time_horizon=TimeHorizon.INTRADAY,
            iem_cd="490470",
            name="세미파이브",
            side=OrderSide.BUY,
            strategy_price=15000,
            stop_price=14700,
            score=85.0,
            reason="ILLEGAL_AFTER_HOURS_BUY",
            timestamp=ah_time,
            signal_session="AFTER_HOURS"
        )
        passed, msg = self.router.run_pre_order_checks(
            signal=sig,
            shares=20,
            order_price=15000,
            balance={"cash": 10000000},
            portfolio_risk_status="NORMAL",
            now=ah_time
        )
        self.assertFalse(passed)
        # 차단 사유는 세션 제한 또는 태깅 위반
        self.assertTrue("BLOCK_NON_REGULAR_SESSION" in msg or "CRITICAL_SESSION_VIOLATION" in msg)


if __name__ == "__main__":
    unittest.main()
