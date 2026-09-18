"""[MASTER TEST SUITE: PROFIT OPPORTUNITY GATE & SCALP CHURN PREVENTER]
Tests covering all 11 requirements from Section 77:
1. test_micro_profit_opportunity_rejected
2. test_small_expected_move_rejected
3. test_cost_dominates_expected_move_rejected
4. test_strong_trend_not_blocked
5. test_strong_breakout_not_blocked
6. test_valid_pullback_not_blocked
7. test_one_tick_rebound_not_enough
8. test_rebound_strength_required
9. test_profit_opportunity_recalculated_at_order_time
10. test_profit_opportunity_respects_signal_ttl
11. test_blocked_trade_counterfactual_mfe_logged
"""

import unittest
from datetime import datetime, timedelta

from strategies.profit_opportunity_gate import (
    validate_profit_opportunity,
    calculate_rebound_strength,
    ProfitOpportunityGate,
    CounterfactualTracker,
    REJECT_MICRO_PROFIT,
    REJECT_LOW_EXPECTED_MOVE,
    REJECT_COST_TO_MOVE_RATIO_HIGH,
    REJECT_EXPECTED_MFE_LOW,
    REJECT_ONE_TICK_REBOUND,
    REJECT_REBOUND_STRENGTH_LOW,
    REJECT_SIGNAL_EXPIRED,
    APPROVE_PROFIT_OPPORTUNITY
)
from strategies.breakout_gate import EntryTimingQualityGate


class TestProfitOpportunityGate(unittest.TestCase):
    """최소 유효 상승폭 검증 및 초단타 짤짤이(Scalp Churn) 방지 테스트 스위트"""

    def setUp(self):
        self.now = datetime(2026, 9, 17, 10, 0, 0)
        self.tracker = CounterfactualTracker.get_instance()
        self.tracker.records.clear()

    # 1. Micro Profit Opportunity 차단 검증
    def test_micro_profit_opportunity_rejected(self):
        """기대 총상승폭 또는 순상승폭이 극미한 경우 (0.3% 상승 등) MICRO_PROFIT_OPPORTUNITY로 기각되어야 함"""
        ok, reason, metrics = validate_profit_opportunity(
            current_price=10000,
            stop_price=9800,
            target_price_1r=10030,  # 0.30% gross move
            strategy_id="INT_BREAKOUT",
            now=self.now
        )
        self.assertFalse(ok)
        self.assertIn(REJECT_MICRO_PROFIT, reason)
        self.assertLess(metrics["expected_gross_move"], 0.0040)

    # 2. ATR 대비 지나치게 작은 기대 이동폭 차단 검증
    def test_small_expected_move_rejected(self):
        """정상 일중 변동성(ATR 3%) 대비 기대 이동폭(0.6%)이 현저히 부족한 경우 LOW_EXPECTED_MOVE로 기각되어야 함"""
        ok, reason, metrics = validate_profit_opportunity(
            current_price=10000,
            stop_price=9800,
            target_price_1r=10060,  # 0.60% gross move
            atr14=300.0,            # ATR = 3.0%, ATR_normalized_move = 0.6 / 3.0 = 0.20 (< 0.30)
            strategy_id="INT_VWAP_PULLBACK",
            now=self.now
        )
        self.assertFalse(ok)
        self.assertIn(REJECT_LOW_EXPECTED_MOVE, reason)
        self.assertLess(metrics["atr_normalized_move"], 0.30)

    # 3. 거래비용이 기대수익을 잠식하는 경우 차단 검증
    def test_cost_dominates_expected_move_rejected(self):
        """수수료, 세금, 슬리피지, 스프레드 등 거래비용 합계가 기대이익의 60%를 초과하는 경우 차단"""
        # 총비용 약 0.35%, 기대상승폭 0.45% -> cost_to_opportunity_ratio = 0.35 / 0.45 = 77.8% (> 60%)
        ok, reason, metrics = validate_profit_opportunity(
            current_price=50000,
            stop_price=49000,
            target_price_1r=50225,  # 0.45% gross move
            spread_pct=0.0015,
            strategy_id="INT_BREAKOUT",
            now=self.now
        )
        self.assertFalse(ok)
        self.assertIn(REJECT_COST_TO_MOVE_RATIO_HIGH, reason)
        self.assertGreater(metrics["cost_to_opportunity_ratio"], 0.60)

    # 4. 건전한 강력 추세 거래 통과 검증
    def test_strong_trend_not_blocked(self):
        """충분한 기대 상승폭과 양호한 R비율을 가진 추세 진입은 정상 승인되어야 함"""
        ok, reason, metrics = validate_profit_opportunity(
            current_price=70000,
            stop_price=68500,       # stop dist = 2.14%
            target_price_1r=72000,  # target dist = 2.85%
            target_price_2r=74000,  # target 2 = 5.71%
            atr14=1500.0,           # ATR = 2.14%
            strategy_id="INT_TREND_MOMENTUM",
            now=self.now
        )
        self.assertTrue(ok)
        self.assertEqual(reason, APPROVE_PROFIT_OPPORTUNITY)
        self.assertGreaterEqual(metrics["expected_gross_move"], 0.02)
        self.assertLessEqual(metrics["cost_to_opportunity_ratio"], 0.30)

    # 5. 강력한 고점 돌파 거래 통과 검증
    def test_strong_breakout_not_blocked(self):
        """고점 돌파와 충분한 후속 상승 여력이 확보된 돌파 신호는 정상 승인되어야 함"""
        ok, reason, metrics = validate_profit_opportunity(
            current_price=50000,
            stop_price=48800,
            target_price_1r=51800,  # target 1 = 3.6%
            target_price_2r=53000,  # target 2 = 6.0%
            atr14=1200.0,
            strategy_id="INT_BREAKOUT",
            now=self.now
        )
        self.assertTrue(ok)
        self.assertEqual(reason, APPROVE_PROFIT_OPPORTUNITY)
        self.assertGreater(metrics["expected_net_r"], 0.50)

    # 6. 유효한 눌림목 반등 거래 통과 검증
    def test_valid_pullback_not_blocked(self):
        """실질적 반등 강도가 확인되고 목표가까지 충분한 여력이 있는 눌림목은 정상 승인되어야 함"""
        ok, reason, metrics = validate_profit_opportunity(
            current_price=25000,
            stop_price=24400,       # 2.4% stop
            target_price_1r=25600,  # 2.4% 1R
            target_price_2r=26200,  # 4.8% 2R
            atr14=600.0,
            strategy_id="INT_VWAP_PULLBACK",
            now=self.now
        )
        self.assertTrue(ok)
        self.assertEqual(reason, APPROVE_PROFIT_OPPORTUNITY)

    # 7. 1틱 미세 반등 차단 검증 (Section 63)
    def test_one_tick_rebound_not_enough(self):
        """1틱 상승(10원 반등 등)만으로 rebound_confirmed 처리된 가짜 반등은 즉각 기각되어야 함"""
        # 현재가 10,010원, 저점 10,000원 -> 1틱(10원, 0.10%) 반등
        ok, strength, reason, metrics = calculate_rebound_strength(
            curr_price=10010,
            lowest_price=10000,
            rvol=1.2,
            current_1m_open=10000,
            current_1m_high=10010,
            current_1m_low=10000
        )
        self.assertFalse(ok)
        self.assertIn(REJECT_ONE_TICK_REBOUND, reason)
        self.assertLess(strength, 0.40)

        # EntryTimingQualityGate에서도 1틱 반등은 최종 탈락해야 함
        timing_ok, timing_reason, _ = EntryTimingQualityGate.validate_entry_timing_quality(
            curr_price=10010,
            strategy_id="INT_VWAP_PULLBACK",
            rvol=1.2,
            vwap=10000,
            current_1m_open=10000,
            current_1m_high=10010,
            current_1m_low=10000,
            lowest_price=10000,
            rebound_confirmed=True  # 겉으로는 True 플래그여도
        )
        self.assertFalse(timing_ok)
        self.assertIn(REJECT_ONE_TICK_REBOUND, timing_reason)

    # 8. 종합 Rebound Strength 요구 검증 (Section 64)
    def test_rebound_strength_required(self):
        """음수 모멘텀 및 윗꼬리 도지형 캔들 등 약한 반등은 기각, 강한 반등(양봉+거래량+모멘텀)은 승인"""
        # 약한 반등: 거래량 저조(RVOL=0.6), 음수 모멘텀(-0.8%), 캔들 몸통 작음
        weak_ok, weak_str, weak_reason, _ = calculate_rebound_strength(
            curr_price=10050,
            lowest_price=10000,
            rvol=0.6,
            ret_3m=-0.008,
            current_1m_open=10045,
            current_1m_high=10090,
            current_1m_low=10000
        )
        self.assertFalse(weak_ok)
        self.assertLess(weak_str, 0.40)

        # 강한 반등: 저점 대비 1.0% 반등, 거래량(RVOL=1.8), 양봉(몸통 70%), 양수 모멘텀
        strong_ok, strong_str, strong_reason, _ = calculate_rebound_strength(
            curr_price=10100,
            lowest_price=10000,
            rvol=1.8,
            ret_3m=0.005,
            current_1m_open=10030,
            current_1m_high=10100,
            current_1m_low=10020
        )
        self.assertTrue(strong_ok)
        self.assertGreaterEqual(strong_str, 0.40)

    # 9. 주문 시점 최신 호가 기준 재계산 검증 (Section 72)
    def test_profit_opportunity_recalculated_at_order_time(self):
        """Candidate 시점에는 유효했으나 주문 직전 가격 급등으로 잔여 이익 여력이 없어진 경우 기각"""
        # 발굴 당시: 10,000원 -> 목표 10,300원 (3.0% 여력 -> PASS)
        cand_ok, _, _ = validate_profit_opportunity(
            current_price=10000,
            stop_price=9800,
            target_price_1r=10300,
            strategy_id="INT_BREAKOUT",
            now=self.now
        )
        self.assertTrue(cand_ok)

        # 실제 주문 직전 호가: 이미 10,270원까지 올라 목표가까지 잔여폭 30원 (0.29% < 0.40% -> REJECT)
        order_time = self.now + timedelta(seconds=2)
        order_ok, order_reason, order_metrics = validate_profit_opportunity(
            current_price=10270,
            stop_price=9800,
            target_price_1r=10300,
            strategy_id="INT_BREAKOUT",
            now=order_time
        )
        self.assertFalse(order_ok)
        self.assertIn(REJECT_MICRO_PROFIT, order_reason)
        self.assertLess(order_metrics["expected_gross_move"], 0.0040)

    # 10. Signal TTL 연동 만료 검증 (Section 73)
    def test_profit_opportunity_respects_signal_ttl(self):
        """신호 생성 후 30초(TTL)가 초과된 신호는 SIGNAL_EXPIRED로 즉시 기각 후 재평가 요구"""
        sig_created = self.now - timedelta(seconds=35)
        ok, reason, metrics = validate_profit_opportunity(
            current_price=10000,
            stop_price=9800,
            target_price_1r=10400,
            strategy_id="INT_BREAKOUT",
            signal_created_at=sig_created,
            now=self.now
        )
        self.assertFalse(ok)
        self.assertIn(REJECT_SIGNAL_EXPIRED, reason)
        self.assertGreater(metrics["signal_age_ms"], 30000.0)

    # 11. 차단된 거래의 사후 MFE/MAE Counterfactual 추적 로깅 검증 (Section 74)
    def test_blocked_trade_counterfactual_mfe_logged(self):
        """차단된 거래가 CounterfactualTracker에 정상 등록되고 사후 가격에 따라 MFE/MAE가 갱신되어야 함"""
        # 1. 짤짤이 기각 발생
        ok, reason, metrics = validate_profit_opportunity(
            current_price=10000,
            stop_price=9800,
            target_price_1r=10020,
            strategy_id="INT_BREAKOUT",
            symbol="005930",
            now=self.now
        )
        self.assertFalse(ok)

        # 2. Counterfactual 등록
        rec = self.tracker.record_blocked_trade(
            symbol="005930",
            strategy_id="INT_BREAKOUT",
            ref_price=10000,
            reason=reason,
            metrics=metrics,
            now=self.now
        )
        self.assertIsNotNone(rec)
        self.assertEqual(rec["symbol"], "005930")
        self.assertEqual(rec["ref_price"], 10000.0)
        self.assertEqual(rec["reject_category"], "MICRO_PROFIT_BLOCKED")

        # 3. 5분 후 사후 가격 업데이트 (10,150원 도달)
        time_5m = self.now + timedelta(minutes=5)
        self.tracker.update_price("005930", current_price=10150, current_time=time_5m)
        self.assertAlmostEqual(rec["counterfactual_mfe"], 0.015, places=3)
        self.assertAlmostEqual(rec["counterfactual_5m_return"], 0.015, places=3)


if __name__ == "__main__":
    unittest.main()
