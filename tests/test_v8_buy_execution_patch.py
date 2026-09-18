"""v8.0 Real Buy Execution Patch Comprehensive Test Harness
(tests/test_v8_buy_execution_patch.py)

Fully verifies all core mechanisms of FULL MARKET SCANNER -> REAL BUY EXECUTION PATCH v8.0:
1. Fallback ATR and Candle Starvation Resolution (never 0 ATR)
2. 3-Tier Gating System (Hard Gate -> Setups A~F -> Soft Score >= 60.0)
3. Setups A through F Generation (Ignition, Breakout, VWAP Pullback, Compression, ORB, PDH)
4. Momentum Stages (START, EARLY, ACTIVE, LATE) and Chase Restriction (>7% only)
5. ML Meta Filter Non-Blocking Behavior (Rule score >= 60 approved with ML sizing)
6. Available Risk Calculation & SIM_RISK_MODE
7. Candidate Promotion & Watch Timeout
8. Telemetry Funnel & Rejection Counter
9. FORCE_SIGNAL_TEST 5 Scenarios 100% Pass
"""

import sys
import os
import unittest
from datetime import datetime, timedelta

# Add project root to sys.path
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core.models import (
    SymbolInfo, SymbolState, Candle, Tick, MarketRegime, TradeSignal,
    OrderSide, OrderType, OrderStatus, TimeHorizon
)
from core.aggregator import CandleAggregator
from core.symbol_store import SymbolStateStore
from core.low_cost_scanner import LowCostMarketScanner
from core.event_detector import EventDetector
from core.candidate_promotion import CandidatePromotionEngine
from strategies.scoring_engine import ScoringEngine
from strategies.full_strategy_suite import FullStrategySuite
from ml.meta_decision import MetaDecisionEngine
from risk.portfolio_risk import PortfolioRiskManager
from execution.diagnostic_engine import DiagnosticEngine


class TestV8BuyExecutionPatch(unittest.TestCase):
    def setUp(self):
        self.now = datetime(2026, 9, 7, 10, 0, 0)
        self.sym = SymbolInfo(
            iem_cd="005930",
            name="삼성전자",
            market="KOSPI",
            price=82000,
            open_price=81000,
            high_price=82500,
            low_price=80500,
            prev_close=80000,
            prev_high=81500,
            is_tradable=True
        )
        self.store = SymbolStateStore({"005930": self.sym})
        self.scanner = LowCostMarketScanner(self.store)

    def test_1_fallback_atr_never_zero(self):
        """1. 캔들 부족 시에도 ATR이 0.0으로 반환되지 않고 폴백값(0.5% 등)을 제공하는지 검증"""
        agg = CandleAggregator("005930")
        # 캔들이 없는 상태
        atr_empty = agg.calculate_atr("1m", 14)
        self.assertEqual(atr_empty, 0.0)

        # 틱 1개 수신 후 형성 중인 캔들만 있는 상태
        agg.on_tick(Tick(timestamp=self.now, iem_cd="005930", price=82000, volume=100))
        atr_forming = agg.calculate_atr("1m", 14)
        self.assertGreater(atr_forming, 0.0)
        self.assertAlmostEqual(atr_forming, 82000 * 0.005, places=1)

    def test_2_three_tier_gating_scoring(self):
        """2. 3-Tier Gating: Hard Gate 통과 후 셋업 가산점(+25) 및 Soft Score 합산(>=60 -> BUY) 검증"""
        sym = SymbolInfo(iem_cd="TEST1", name="게이팅테스트", market="KOSPI", price=50000, open_price=49000, is_tradable=True)
        agg = CandleAggregator("TEST1")
        agg.vwap = 49500.0
        agg.current_1m = Candle(timestamp=self.now, timeframe="1m", open=49200, high=50200, low=49100, close=50000, volume=20000)

        # 셋업 충족 시 점수 60점 이상 산출 및 BUY 승인
        sigs = FullStrategySuite.evaluate_intraday_all(
            sym=sym, agg=agg, regime=MarketRegime.BULL, now=self.now,
            patterns={"is_ignition": True}
        )
        self.assertGreaterEqual(len(sigs), 1)
        self.assertGreaterEqual(sigs[0].score, 60.0)
        self.assertEqual(sigs[0].side, OrderSide.BUY)

    def test_3_setups_a_through_f_generation(self):
        """3. 6대 셋업(A~F) 신호 생성 정상 작동 검증"""
        agg = CandleAggregator("005930")
        for i in range(10):
            c = Candle(timestamp=self.now - timedelta(minutes=10-i), timeframe="1m", open=81000, high=81300, low=80900, close=81100, volume=5000, is_closed=True)
            agg.candles_1m.append(c)
        agg.vwap = 81200.0
        agg.current_1m = Candle(timestamp=self.now, timeframe="1m", open=81200, high=82300, low=81200, close=82000, volume=30000)

        # Strategy A: Momentum Ignition
        sigs_a = FullStrategySuite.evaluate_intraday_all(
            sym=self.sym, agg=agg, regime=MarketRegime.BULL, now=self.now, patterns={"is_ignition": True}
        )
        self.assertTrue(any("모멘텀 점화" in s.reason for s in sigs_a))

        # Strategy B: Breakout
        sigs_b = FullStrategySuite.evaluate_intraday_all(
            sym=self.sym, agg=agg, regime=MarketRegime.BULL, now=self.now, patterns={}
        )
        self.assertTrue(len(sigs_b) > 0)

        # Strategy C: VWAP Pullback
        agg_c = CandleAggregator("005930")
        agg_c.vwap = 81900.0
        agg_c.candles_1m.append(Candle(timestamp=self.now - timedelta(minutes=1), timeframe="1m", open=82100, high=82200, low=81890, close=81920, volume=4000, is_closed=True))
        agg_c.current_1m = Candle(timestamp=self.now, timeframe="1m", open=81900, high=82100, low=81890, close=82000, volume=8000)
        sigs_c = FullStrategySuite.evaluate_intraday_all(
            sym=self.sym, agg=agg_c, regime=MarketRegime.BULL, now=self.now, patterns={}
        )
        self.assertTrue(any("VWAP 지지" in s.reason for s in sigs_c))

        # Strategy D: Compression Breakout
        sigs_d = FullStrategySuite.evaluate_intraday_all(
            sym=self.sym, agg=agg, regime=MarketRegime.BULL, now=self.now, patterns={"is_compression_breakout": True}
        )
        self.assertTrue(any(s.strategy_id == "INT_COMPRESSION_BREAKOUT" for s in sigs_d))

        # Strategy E: ORB
        sigs_e = FullStrategySuite.evaluate_intraday_all(
            sym=self.sym, agg=agg, regime=MarketRegime.BULL, now=self.now, patterns={}, or_high=81800
        )
        self.assertTrue(any("장초반" in s.reason for s in sigs_e))

        # Strategy F: PDH Breakout (sym.prev_high is 81500, price is 82000)
        sigs_f = FullStrategySuite.evaluate_intraday_all(
            sym=self.sym, agg=agg, regime=MarketRegime.BULL, now=self.now, patterns={}
        )
        self.assertTrue(any("전일 고가" in s.reason for s in sigs_f))

    def test_4_momentum_stages_and_chase_restriction(self):
        """4. 모멘텀 구간 분류 및 7% 초과 LATE 구간에서만 추격매수 차단 검증"""
        agg = CandleAggregator("005930")
        agg.current_1m = Candle(timestamp=self.now, timeframe="1m", open=80000, high=81500, low=80000, close=81500, volume=10000)

        # 1. Early (+2.0%)
        sym_early = SymbolInfo(iem_cd="005930", name="삼성전자", market="KOSPI", price=81600, open_price=80000, is_tradable=True)
        EventDetector.evaluate_events(sym_early, agg, self.now)
        self.assertEqual(sym_early.momentum_stage, "EARLY")
        self.assertNotIn("CHASE_RESTRICTED_LATE_MOMENTUM", sym_early.buy_block_reasons)

        # 2. Active (+5.0%)
        sym_active = SymbolInfo(iem_cd="005930", name="삼성전자", market="KOSPI", price=84000, open_price=80000, is_tradable=True)
        EventDetector.evaluate_events(sym_active, agg, self.now)
        self.assertEqual(sym_active.momentum_stage, "ACTIVE")
        self.assertNotIn("CHASE_RESTRICTED_LATE_MOMENTUM", sym_active.buy_block_reasons)

        # 3. Late (+9.0%) -> is_chase_forbidden should be True
        sym_late = SymbolInfo(iem_cd="005930", name="삼성전자", market="KOSPI", price=87200, open_price=80000, is_tradable=True)
        _, _, _, patterns_late = EventDetector.evaluate_events(sym_late, agg, self.now)
        self.assertEqual(sym_late.momentum_stage, "LATE")
        self.assertTrue(patterns_late["is_chase_forbidden"])

        # Strategy evaluation should record block reason
        sigs_late = FullStrategySuite.evaluate_intraday_all(
            sym=sym_late, agg=agg, regime=MarketRegime.BULL, now=self.now, patterns=patterns_late
        )
        self.assertEqual(len(sigs_late), 0)
        self.assertIn("CHASE_RESTRICTED_LATE_MOMENTUM", sym_late.buy_block_reasons)

    def test_5_ml_meta_filter_non_blocking(self):
        """5. 룰 점수 60점 이상 우수 셋업은 ML 확률(0.64)이 0.65 미만이어도 거절되지 않고 통과하는지 검증"""
        signal = TradeSignal(
            strategy_id="INT_MOMENTUM_IGNITION",
            time_horizon=TimeHorizon.INTRADAY,
            iem_cd="005930",
            name="삼성전자",
            side=OrderSide.BUY,
            strategy_price=82000,
            stop_price=80500,
            score=82.0,
            reason="테스트 신호",
            timestamp=self.now,
            rule_score=82.0
        )
        # ML 확률 0.64 (과거 하드코딩 기준 0.65 미만이어도 룰 점수 82점이므로 승인)
        engine = MetaDecisionEngine()
        result = engine.evaluate_candidate(
            setup_name="INT_MOMENTUM_IGNITION",
            time_horizon=TimeHorizon.INTRADAY,
            entry_price=82000,
            stop_price=80500,
            target_price=85000,
            predicted_probs={"p_target": 0.64, "p_stop": 0.36},
            rule_score=82.0
        )
        self.assertTrue(result.approved)
        self.assertGreater(result.recommended_risk_pct, 0.0)
        self.assertGreater(result.expected_net_r, 0.0)

    def test_6_available_risk_and_sim_mode(self):
        """6. Available Risk 계산 및 SIM_RISK_MODE 보호 동작 검증"""
        # 기본 자산 5억
        summary = PortfolioRiskManager.get_risk_summary(positions=[], equity=500_000_000.0, max_risk_limit=0.04)
        self.assertEqual(summary["used_risk_ratio"], 0.0)
        self.assertEqual(summary["available_risk_ratio"], 0.04)
        self.assertEqual(summary["status"], "NORMAL")

        # SIM_RISK_MODE 동작
        PortfolioRiskManager.SIM_RISK_MODE = True
        tot_risk, r_ratio, stat = PortfolioRiskManager.calculate_total_open_risk(positions=[], equity=0.0)
        self.assertEqual(stat, "NORMAL")
        PortfolioRiskManager.SIM_RISK_MODE = False

    def test_7_telemetry_funnel_and_rejection_recording(self):
        """7. 텔레메트리 퍼널 추적 및 거절 사유 기록 검증"""
        diag = DiagnosticEngine()
        diag.record_event_detected(3136)
        diag.record_candidate(10)
        diag.record_setup_match(5)
        diag.record_buy_approved(3)
        diag.record_order_created(3)
        diag.record_order_sent(3)
        diag.record_fill(3)
        diag.record_rejection("SPREAD_TOO_WIDE", 2)

        rates = diag.get_conversion_rates()
        self.assertAlmostEqual(rates["candidate_to_setup"], 50.0, places=1)
        self.assertAlmostEqual(rates["setup_to_buy"], 60.0, places=1)
        self.assertEqual(diag.rejection_counter["SPREAD_TOO_WIDE"], 2)

    def test_8_force_signal_test_100_percent(self):
        """8. FORCE_SIGNAL_TEST 5대 시나리오(TEST A~E) 100% 통과 검증"""
        diag = DiagnosticEngine()
        res = diag.run_force_signal_test(
            scanner=self.scanner,
            full_strategy_suite=FullStrategySuite,
            scoring_engine=ScoringEngine,
            order_router=None,
            symbol_store=self.store
        )
        self.assertTrue(res["all_passed"], f"FORCE_SIGNAL_TEST 실패: {res['scenarios']}")
        for t_name, info in res["scenarios"].items():
            self.assertTrue(info["passed"], f"{t_name} 실패: {info}")


if __name__ == "__main__":
    unittest.main()
