"""v7.0 Self-Improving Quant AI Comprehensive Test Harness
(tests/test_v7_self_improving_ai_harness.py)

Fully verifies all 12 scenarios specified in Section 97 of SELF-IMPROVING QUANT AI v7.0:
- TEST 1: Unregistered stock 5x volume spike -> Auto detection
- TEST 2: PDH breakout -> Auto detection
- TEST 3: Momentum ignition -> Momentum model activation & prediction
- TEST 4: Chart structure improvement -> Higher-High/Higher-Low score boost
- TEST 5: Model performance drop -> Drift detection & Retraining request trigger
- TEST 6: Candidate model generated -> Automated validation & OOS testing
- TEST 7: Candidate superior -> Paper Trading (Shadow Mode) evaluation
- TEST 8: Paper trading superior -> Champion/Challenger comparison & staged rollout
- TEST 9: Deployed challenger performance degradation -> Automatic Emergency Rollback to Champion
- TEST 10: Market crash / panic -> Trading Firewall rejects ML BUY signal
- TEST 11: API failure / heartbeat delay -> Emergency circuit breaker blocks new orders
- TEST 12: Invalid tick pricing -> KRX Tick normalization auto-adjusts
"""

import sys
import os
import unittest
from datetime import datetime, timedelta
import numpy as np

# Add project root to sys.path
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core.models import (
    Tick, Candle, OrderSide, OrderType, OrderStatus, TimeHorizon, MarketRegime, TradeSignal
)
from core.tick_normalizer import normalize_price, get_tick_size
from core.aggregator import CandleAggregator
from universe.full_universe_master import FullUniverseMaster
from core.symbol_store import SymbolStateStore
from core.low_cost_scanner import LowCostMarketScanner
from core.event_detector import EventDetector
from ml.features import QuantitativeFeatureEngine
from ml.opportunity_model import OpportunityPredictor
from ml.meta_decision import MetaDecisionEngine, MetaDecisionResult
from ml.trade_db import TradeDatabase, TradeRecord
from ml.drift_detector import DriftDetector, DriftReport
from ml.learning_pipeline import ChampionChallengerManager, LearningPipeline
from risk.circuit_breaker import CircuitBreaker
from risk.loss_limits import LossLimitManager
from risk.trading_firewall import TradingFirewall
from execution.order_router import OrderRouter
from execution.position_manager import PositionManager


class TestV7SelfImprovingAIHarness(unittest.TestCase):
    """
    Section 97: 12-Scenario Execution-Grade Test Suite for v7.0 Self-Improving Quant AI.
    """

    def setUp(self):
        self.now = datetime(2026, 9, 4, 10, 0, 0)
        self.master_symbols = FullUniverseMaster.load_full_universe()
        self.store = SymbolStateStore(self.master_symbols)
        self.scanner = LowCostMarketScanner(self.store)

    def test_section97_01_unregistered_volume_spike_detection(self):
        """TEST 1: 미등록 종목 당일 거래량 5배 폭증 발생 -> 자동 탐지"""
        symbol = "090430"  # 아모레퍼시픽 (관심종목 밖 임의의 종목)
        now = self.now

        agg = self.scanner.get_aggregator(symbol)
        # 과거 20분 동안 평소 거래량 1,000주씩 누적
        t = now - timedelta(minutes=25)
        for i in range(20):
            t += timedelta(minutes=1)
            agg.on_tick(Tick(timestamp=t, iem_cd=symbol, price=145000, volume=1000))

        # 현재 1분 동안 거래량 5,000주 (5배) 폭증 틱
        t_now = now
        agg.on_tick(Tick(timestamp=t_now, iem_cd=symbol, price=146000, volume=5000))

        state, score, detected_sym = self.scanner.on_market_tick(
            iem_cd=symbol, price=146000, volume=5000, timestamp=t_now
        )
        self.assertGreaterEqual(score, 15.0)
        self.assertTrue(any("거래량 폭증" in ev for ev in detected_sym.active_events))

    def test_section97_02_unregistered_pdh_breakout_detection(self):
        """TEST 2: 전일 고가 돌파 발생 -> 자동 탐지"""
        symbol = "028300"  # HLB
        sym = self.store.get(symbol)
        if sym:
            sym.prev_high = 85000.0

        # 전일 고가(85,000원) 돌파 86,200원 틱 주입
        agg = self.scanner.get_aggregator(symbol)
        agg.on_tick(Tick(timestamp=self.now, iem_cd=symbol, price=86200, volume=5000))

        state, score, detected_sym = self.scanner.on_market_tick(
            iem_cd=symbol, price=86200, volume=5000, timestamp=self.now
        )
        self.assertGreaterEqual(score, 10.0)
        self.assertTrue(any("전일 고가" in ev for ev in detected_sym.active_events))

    def test_section97_03_momentum_ignition_model_activation(self):
        """TEST 3: 모멘텀 점화 발생 -> 모멘텀 모델 자동 활성화 및 확률 예측"""
        agg = CandleAggregator("000660")
        base_dt = self.now - timedelta(minutes=30)
        for i in range(30):
            p = 150000 + i * 500
            v = 5000 if i >= 25 else 1000
            agg.on_tick(Tick(base_dt + timedelta(minutes=i, seconds=10), "000660", p, v // 2))
            agg.on_tick(Tick(base_dt + timedelta(minutes=i, seconds=50), "000660", p + 200, v // 2))

        engine = QuantitativeFeatureEngine()
        features = engine.calculate_features("000660", agg, 165000, MarketRegime.STRONG_BULL, self.now)

        # 52개 피처 추출 확인
        self.assertGreaterEqual(len(features), 50)
        self.assertIn("ret_3m", features)
        self.assertIn("rvol_5m", features)
        self.assertIn("vwap_dist", features)

        # 모멘텀 기회 예측기 추론
        predictor = OpportunityPredictor(version="test_mom")
        # 가중치 주입 또는 synthetic predict
        pred = predictor.predict_opportunity(features)
        self.assertIn("p_target", pred)
        self.assertIn("p_stop", pred)
        self.assertIn("expected_net_r", pred)
        self.assertGreaterEqual(pred["p_target"], 0.0)
        self.assertLessEqual(pred["p_target"], 1.0)

    def test_section97_04_chart_structure_hh_hl_boost(self):
        """TEST 4: 차트 구조 개선(HH-HL 형성) -> 모델 스코어/확률 상승"""
        # Flat 구조의 캔들
        agg_flat = CandleAggregator("005930")
        base_dt = self.now - timedelta(minutes=30)
        for i in range(30):
            agg_flat.on_tick(Tick(base_dt + timedelta(minutes=i), "005930", 70000 + (i % 2) * 50, 1000))

        # Higher-High / Higher-Low (안정적 계단식 상승 추세) 캔들
        agg_bull = CandleAggregator("005930")
        for i in range(30):
            agg_bull.on_tick(Tick(base_dt + timedelta(minutes=i), "005930", 70000 + i * 30, 2000))

        engine = QuantitativeFeatureEngine()
        feats_flat = engine.calculate_features("005930", agg_flat, 70000, MarketRegime.NEUTRAL, self.now)
        feats_bull = engine.calculate_features("005930", agg_bull, 70900, MarketRegime.STRONG_BULL, self.now)

        # 구조 점수(Higher-High Higher-Low) 비교
        score_flat = feats_flat.get("structure_hh_hl", 0.0)
        score_bull = feats_bull.get("structure_hh_hl", 0.0)
        self.assertGreater(score_bull, score_flat)

        predictor = OpportunityPredictor(version="test_struct")
        pred_flat = predictor.predict_opportunity(feats_flat)
        pred_bull = predictor.predict_opportunity(feats_bull)
        self.assertGreater(pred_bull["p_target"], pred_flat["p_target"])

    def test_section97_05_performance_drop_drift_and_retraining_trigger(self):
        """TEST 5: 모델 성능 저하 발생 -> Drift 감지 및 재학습 요청(RETRAINING_REQUEST) 트리거"""
        detector = DriftDetector(psi_threshold=0.25, min_rolling_win_rate=0.45)

        # 1. Feature Drift (PSI) 테스트
        np.random.seed(42)
        baseline_rvol = np.random.normal(1.2, 0.3, 100).tolist()
        shifted_rvol = np.random.normal(4.5, 1.2, 100).tolist() # 큰 폭의 분포 이동

        base_feats = {"rvol_1m": baseline_rvol}
        recent_feats = {"rvol_1m": shifted_rvol}

        # 2. Performance Decay (최근 20건 거래 중 승률 20%로 급락)
        degraded_trades = []
        for i in range(20):
            is_win = (i < 4) # 4승 16패 (20% 승률)
            degraded_trades.append({
                "trade_id": f"t_{i}",
                "pnl": 50000.0 if is_win else -100000.0,
                "r_multiple": 1.0 if is_win else -1.0,
                "p_target_pred": 0.70 # 모델은 70%로 높게 예측했으나 실제 연패 -> Brier 점수 악화
            })

        report = detector.evaluate(
            baseline_features=base_feats,
            recent_features=recent_feats,
            recent_trades=degraded_trades
        )

        self.assertTrue(report.is_drift_detected)
        self.assertTrue(report.retraining_needed)
        self.assertGreaterEqual(report.max_psi, 0.25)
        self.assertLess(report.rolling_win_rate, 0.45)
        self.assertGreater(len(report.reasons), 0)

    def test_section97_06_candidate_model_generation_and_oos_testing(self):
        """TEST 6: 후보 모델 생성 -> 자동 검증 및 Out-of-Sample(OOS) 평가 통과"""
        pipeline = LearningPipeline(models_dir="models/test_opp")
        np.random.seed(42)

        # 100건의 합성 학습 데이터 생성
        features_data = []
        labels = []
        for i in range(120):
            f = {
                "ret_1m": np.random.normal(0.005, 0.01),
                "rvol_1m": np.random.uniform(1.0, 5.0),
                "rsi_1m": np.random.uniform(40.0, 80.0),
                "dev_vwap": np.random.normal(0.003, 0.005),
                "structure_hh_hl": np.random.uniform(0.3, 1.0),
                "obi_5": np.random.uniform(-0.5, 0.5)
            }
            # RVOL과 구조 점수가 높을 때 높은 성공 확률 부여
            prob = 0.3 + 0.3 * (f["rvol_1m"] / 5.0) + 0.4 * f["structure_hh_hl"]
            label = 1 if np.random.rand() < prob else 0
            features_data.append(f)
            labels.append(label)

        report = pipeline.train_and_validate_candidate(features_data, labels, "v7.1_candidate")
        self.assertIsNotNone(report)
        self.assertTrue(report.is_valid)
        self.assertGreaterEqual(report.sample_size, 30) # OOS 샘플 크기

    def test_section97_07_candidate_superior_paper_trading_shadow_mode(self):
        """TEST 7: 후보 모델 우수 -> Paper Trading(Shadow Mode) 가상 성과 평가"""
        manager = ChampionChallengerManager(state_file="data/test_shadow_registry.json")
        manager.register_challenger("v7.1_challenger")

        self.assertEqual(manager.challenger_state, "SHADOW")
        self.assertEqual(manager.challenger_allocation, 0.0)

        # 20건의 가상 섀도우 트레이딩 결과 주입 (승률 65%, PF 2.1)
        for i in range(20):
            is_win = (i % 3 != 0)
            manager.record_shadow_trade({
                "trade_id": f"shadow_{i}",
                "pnl": 150000.0 if is_win else -80000.0,
                "r_multiple": 1.5 if is_win else -1.0
            })

        shadow_eval = manager.evaluate_shadow_performance()
        self.assertTrue(shadow_eval["superior"])
        self.assertGreaterEqual(shadow_eval["win_rate"], 0.55)
        self.assertGreaterEqual(shadow_eval["profit_factor"], 1.5)

    def test_section97_08_staged_rollout_advancement_to_champion(self):
        """TEST 8: 가상 성과 우수 -> Champion/Challenger 단계적 롤아웃 (5% -> 10% -> 25% -> 50% -> 100%)"""
        manager = ChampionChallengerManager(champion_version="v7.0", state_file="data/test_rollout_registry.json")
        manager.register_challenger("v7.1_alpha")

        # 1. 섀도우에서 롤아웃 진입 -> 5%
        ok = manager.start_staged_rollout()
        self.assertTrue(ok)
        self.assertEqual(manager.challenger_state, "STAGED")
        self.assertEqual(manager.challenger_allocation, 0.05)

        # 2. 단계적 배분 확대: 10% -> 25% -> 50%
        manager.advance_rollout_stage()
        self.assertEqual(manager.challenger_allocation, 0.10)
        manager.advance_rollout_stage()
        self.assertEqual(manager.challenger_allocation, 0.25)
        manager.advance_rollout_stage()
        self.assertEqual(manager.challenger_allocation, 0.50)

        # 3. 최종 100% 승격 -> 새로운 챔피언으로 등극
        manager.advance_rollout_stage()
        self.assertEqual(manager.champion_version, "v7.1_alpha")
        self.assertIsNone(manager.challenger_version)
        self.assertEqual(manager.challenger_state, "PROMOTED")

    def test_section97_09_deployed_challenger_emergency_rollback(self):
        """TEST 9: 롤아웃 중 챌린저 성능 저하 발생 -> 챔피언으로 즉각 긴급 롤백(Emergency Rollback)"""
        manager = ChampionChallengerManager(champion_version="v7.0_champ", state_file="data/test_rollback_registry.json")
        manager.register_challenger("v7.2_buggy")
        manager.start_staged_rollout() # 5% 배분 시작

        # 실전 배치 후 4연패 발생 (급격한 성능 저하 시뮬레이션)
        bad_trades = [
            {"model_version": "v7.2_buggy", "pnl": -50000, "r_multiple": -1.0},
            {"model_version": "v7.2_buggy", "pnl": -60000, "r_multiple": -1.0},
            {"model_version": "v7.2_buggy", "pnl": -70000, "r_multiple": -1.0},
            {"model_version": "v7.2_buggy", "pnl": -80000, "r_multiple": -1.0},
        ]

        healthy = manager.check_challenger_health(bad_trades)
        self.assertFalse(healthy) # 이상 징후 감지

        # 긴급 롤백 결과 확인: 챌린저 배분 즉시 0%, 챔피언 100% 원복
        self.assertEqual(manager.challenger_allocation, 0.0)
        self.assertEqual(manager.challenger_state, "ROLLED_BACK")
        self.assertEqual(manager.champion_version, "v7.0_champ")

    def test_section97_10_market_crash_firewall_veto(self):
        """TEST 10: 시장 패닉/급락장 발생 -> AI 매수 신호를 Trading Firewall이 즉시 거부(Veto)"""
        firewall = TradingFirewall()

        # AI가 승률 85%의 강력한 매수 신호를 생성했더라도
        sig = TradeSignal(
            strategy_id="AI_MOMENTUM", time_horizon=TimeHorizon.INTRADAY,
            iem_cd="005930", name="삼성전자", side=OrderSide.BUY,
            strategy_price=70000, stop_price=69000, score=95.0,
            reason="High ML score", timestamp=self.now, target_1r=71000, target_2r=72000
        )

        # 시장 국면이 PANIC인 경우
        verdict_panic = firewall.check_firewall(
            signal=sig, shares=100, order_price=70000, equity=100_000_000, cash=50_000_000,
            current_regime=MarketRegime.PANIC, active_positions={}
        )
        self.assertFalse(verdict_panic.approved)
        self.assertIn("MARKET_REGIME_VETO", verdict_panic.veto_reason)
        self.assertEqual(verdict_panic.shares, 0)

        # 시장 국면이 BEAR인 경우도 거부
        verdict_bear = firewall.check_firewall(
            signal=sig, shares=100, order_price=70000, equity=100_000_000, cash=50_000_000,
            current_regime=MarketRegime.BEAR, active_positions={}
        )
        self.assertFalse(verdict_bear.approved)

    def test_section97_11_api_failure_heartbeat_circuit_breaker(self):
        """TEST 11: API 지연/하트비트 중단 -> 긴급 서킷 브레이커 발동 및 신규 주문 차단"""
        cb = CircuitBreaker()
        firewall = TradingFirewall(circuit_breaker=cb)

        sig = TradeSignal(
            strategy_id="AI_MOMENTUM", time_horizon=TimeHorizon.INTRADAY,
            iem_cd="005930", name="삼성전자", side=OrderSide.BUY,
            strategy_price=70000, stop_price=69000, score=90.0,
            reason="Normal signal", timestamp=self.now, target_1r=71000, target_2r=72000
        )

        # 데이터 하트비트가 4초 전으로 지연 발생 (임계값 3.0초 초과)
        cb.update_data_heartbeat(self.now - timedelta(seconds=4))

        verdict = firewall.check_firewall(
            signal=sig, shares=100, order_price=70000, equity=100_000_000, cash=50_000_000,
            current_regime=MarketRegime.STRONG_BULL, active_positions={}, now=self.now
        )

        self.assertFalse(verdict.approved)
        self.assertTrue(cb.is_tripped)
        self.assertIn("DATA_HEARTBEAT_STALE", verdict.veto_reason)

    def test_section97_12_invalid_tick_pricing_normalization(self):
        """TEST 12: 비정상 호가 주문 -> KRX 호가단위 자동 정규화 및 과도한 손절폭 차단"""
        firewall = TradingFirewall()

        # 75,040원 (75,000원 이상은 100원 호가단위이므로 75,040원은 불법 호가)
        # 매수 시 75,000원으로 자동 내림 정규화 확인
        self.assertEqual(normalize_price(75040, "BUY", "LIMIT"), 75000)

        # 단타에서 손절폭이 -4.5%인 비정상 주문 신호 주입 (단타 규정상 최대 3.0% 이내여야 함)
        invalid_stop_sig = TradeSignal(
            strategy_id="AI_MOMENTUM", time_horizon=TimeHorizon.INTRADAY,
            iem_cd="005930", name="삼성전자", side=OrderSide.BUY,
            strategy_price=75040, stop_price=71500, # -4.7% 손절폭
            score=90.0, reason="Wide stop", timestamp=self.now, target_1r=78000, target_2r=80000
        )

        verdict = firewall.check_firewall(
            signal=invalid_stop_sig, shares=50, order_price=75040, equity=100_000_000, cash=50_000_000,
            current_regime=MarketRegime.STRONG_BULL, active_positions={}, now=self.now
        )

        self.assertFalse(verdict.approved)
        self.assertIn("INTRADAY_STOP_LOSS_TOO_WIDE", verdict.veto_reason)


if __name__ == "__main__":
    unittest.main(verbosity=2)
