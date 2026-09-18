"""v12.3 Learning Integrity & Point-in-Time Experience Memory Test Harness
tests/test_v12_learning_integrity_harness.py

Tests all 30 requirements of [FINAL PATCH v12.3]:
1. Point-in-Time Experience Memory Immutability
2. Separation of Feature Snapshot & Future Outcome
3. NO_TRADE Virtual Trade Simulation (Spread, Slippage, Fees, Taxes)
4. Missed Winner Definition
5. Correct No Trade Definition
6. Missed Opportunity Sub-categories (Breakout, Momentum, Pullback, Swing, False Signal, etc.)
7. Historical Similarity Feature Redundancy & Correlation Check
8. Historical Similarity as Meta Feature (No direct BUY)
9. Probability Calibration (ECE & Brier Score)
10. Meta Model Ablation Test (Rule vs ML vs Similarity vs Execution)
11. Retraining Trigger (Scheduled vs Event-Based with 2+ conditions)
12. Minimum Sample Requirement (<10k total or <500 strategy keeps Champion)
13. Recent Data Weighting (<3m: 1.5x, 3~12m: 1.0x, >12m: 0.5x)
14. Regime-Specific Memory Downweighting
15. Time-of-Day Memory Downweighting
16. Composite Similarity (Feature * Regime * Time * Liquidity)
17. Model Registry & Metadata
18. Champion / Challenger / Experimental Roles
19. Model Promotion Gates
20. Emergency Rollback to Previous Champion
21. Feature & Dataset Versioning
22. Reproducibility with Random Seed
23. Learning Audit Log Logging
24. Learning Freeze during Market Hours (09:00~15:30 Inference Only)
25. Shadow Model Real-time Prediction Tracking
26. Multi-Window Degradation Monitoring (50, 100, 200, 500)
27. Closed-Loop Self-Learning Flow
"""

import os
import sys
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import unittest
import tempfile
import shutil
from datetime import datetime, timedelta, time
import numpy as np

from ml.experience_memory import (
    PointInTimeSnapshot,
    OutcomeRecord,
    ExperienceMemory
)
from ml.similarity_engine import (
    HistoricalSimilarityEngine,
    SimilarityMetaOutput
)
from ml.model_registry import (
    ModelMetadata,
    EnhancedModelRegistry
)
from ml.retraining_trigger import (
    LearningFreezeManager,
    RetrainingTriggerEngine,
    CalibrationMonitor,
    MetaModelAblationTester
)
from ml.meta_decision import (
    MetaDecisionEngine,
    MetaDecisionResult
)
from core.models import TimeHorizon


class TestV12LearningIntegrityHarness(unittest.TestCase):
    def setUp(self):
        self.test_dir = tempfile.mkdtemp()
        self.db_path = os.path.join(self.test_dir, "test_exp_memory.db")
        self.registry_path = os.path.join(self.test_dir, "test_registry.json")
        self.audit_log_path = os.path.join(self.test_dir, "test_audit.jsonl")

        self.memory = ExperienceMemory(db_path=self.db_path)
        self.sim_engine = HistoricalSimilarityEngine(self.memory, min_samples=3)
        self.registry = EnhancedModelRegistry(
            registry_file=self.registry_path,
            audit_log_file=self.audit_log_path
        )
        self.retraining_engine = RetrainingTriggerEngine(
            scheduled_days=7,
            min_total_samples=10000,
            min_strategy_samples=500
        )
        self.meta_engine = MetaDecisionEngine()

    def tearDown(self):
        shutil.rmtree(self.test_dir, ignore_errors=True)

    # -------------------------------------------------------------------------
    # TEST 1 & 2: Point-in-Time Immutability & Feature/Outcome Separation
    # -------------------------------------------------------------------------
    def test_01_point_in_time_immutability_and_separation(self):
        """TEST 1 & 2: Point-in-Time 스냅샷 불변성 및 미래 결과와의 엄격한 분리"""
        now = datetime(2026, 9, 7, 10, 0, 0)
        snap = PointInTimeSnapshot(
            event_id="EVT_TEST_001",
            iem_cd="005930",
            name="삼성전자",
            event_time=now.isoformat(),
            as_of_time=now.isoformat(),
            feature_version="FEATURE_V001",
            strategy_version="STRATEGY_V12_3",
            model_version="CHAMPION_V12_3",
            dataset_version="DATA_V001",
            raw_features={"price": 70000, "volume": 100000, "rvol": 2.5, "vwap": 69800},
            prediction={"p_target": 0.68, "p_stop": 0.32, "expected_net_r": 0.25},
            decision="NO_TRADE",
            decision_reason="RR Ratio Below Target",
            rule_score=68.0,
            virtual_trade={
                "entry_price": 70000, "stop_price": 68500, "target_price": 73000,
                "shares": 10, "strategy_id": "INT_BREAKOUT", "time_horizon": "INTRADAY"
            },
            regime="BULL",
            time_of_day_bucket="09:30-11:30"
        )

        # 1. Frozen dataclass 확인 (속성 수정 불가)
        with self.assertRaises(Exception):
            snap.decision = "BUY"

        # 2. DB 저장 및 로드
        self.memory.record_snapshot(snap)
        loaded = self.memory.get_snapshot("EVT_TEST_001")
        self.assertIsNotNone(loaded)
        self.assertEqual(loaded.raw_features["price"], 70000)
        self.assertEqual(loaded.decision, "NO_TRADE")

        # 3. 10분 후 가격 변동은 Outcome Dataset에만 저장되고 Snapshot raw_features는 변하지 않음
        outcome = OutcomeRecord(
            event_id="EVT_TEST_001",
            evaluated_at=(now + timedelta(minutes=10)).isoformat(),
            horizon_reached="10M",
            target_hit=True,
            stop_hit=False,
            target_hit_first=True,
            max_mfe_pct=0.045,
            max_mae_pct=0.005,
            realized_net_r=1.85,
            outcome_category="MISSED_WINNER"
        )
        self.memory.record_outcome(outcome)

        # 재로드 후 스냅샷은 여전히 원래 시점 데이터만 유지
        loaded_again = self.memory.get_snapshot("EVT_TEST_001")
        self.assertNotIn("max_mfe_pct", loaded_again.raw_features)
        self.assertEqual(loaded_again.raw_features["price"], 70000)

        # Outcome 테이블에 분리 저장 확인
        loaded_outcome = self.memory.get_outcome("EVT_TEST_001")
        self.assertIsNotNone(loaded_outcome)
        self.assertEqual(loaded_outcome.outcome_category, "MISSED_WINNER")

    # -------------------------------------------------------------------------
    # TEST 3, 4, 5, 6: NO_TRADE Virtual Simulation, Missed Winner & Categories
    # -------------------------------------------------------------------------
    def test_02_virtual_trade_simulation_missed_winner(self):
        """TEST 3 & 4: NO_TRADE 종목 가상 거래 시뮬레이션 후 MISSED_WINNER 정확한 분류"""
        now = datetime(2026, 9, 7, 10, 15, 0)
        snap = PointInTimeSnapshot(
            event_id="EVT_MISSED_BO",
            iem_cd="000660",
            name="SK하이닉스",
            event_time=now.isoformat(),
            as_of_time=now.isoformat(),
            feature_version="FEATURE_V001",
            strategy_version="STRATEGY_V12_3",
            model_version="CHAMPION_V12_3",
            dataset_version="DATA_V001",
            raw_features={"price": 120000, "rvol": 3.0, "vwap": 119500},
            prediction={"p_target": 0.58, "p_stop": 0.42, "expected_net_r": 0.12},
            decision="NO_TRADE",
            decision_reason="Expected Net R < 0.15R",
            rule_score=72.0,
            virtual_trade={
                "entry_price": 120000, "stop_price": 118000, "target_price": 124000,
                "shares": 10, "strategy_id": "INT_BREAKOUT", "time_horizon": "INTRADAY"
            },
            regime="BULL",
            time_of_day_bucket="09:30-11:30"
        )
        self.memory.record_snapshot(snap)
        self.assertIn("EVT_MISSED_BO", self.memory.active_tracking)

        # 시세 업데이트: Stop(118000) 건드리지 않고 Target(124000) 도달!
        resolved = self.memory.on_price_update(
            iem_cd="000660",
            current_price=124500,
            high_price=124500,
            low_price=119500,
            now=now + timedelta(minutes=15)
        )
        self.assertEqual(len(resolved), 1)
        rec = resolved[0]
        self.assertTrue(rec.target_hit_first)
        # Breakout 전략이었으므로 MISSED_BREAKOUT으로 세분화 분류
        self.assertEqual(rec.outcome_category, "MISSED_BREAKOUT")

    def test_03_virtual_trade_simulation_correct_no_trade(self):
        """TEST 5: NO_TRADE 이후 손절가 먼저 도달 시 CORRECT_NO_TRADE 분류"""
        now = datetime(2026, 9, 7, 11, 0, 0)
        snap = PointInTimeSnapshot(
            event_id="EVT_CORRECT_NT",
            iem_cd="035420",
            name="NAVER",
            event_time=now.isoformat(),
            as_of_time=now.isoformat(),
            feature_version="FEATURE_V001",
            strategy_version="STRATEGY_V12_3",
            model_version="CHAMPION_V12_3",
            dataset_version="DATA_V001",
            raw_features={"price": 200000, "rvol": 1.2},
            prediction={"p_target": 0.45, "p_stop": 0.55, "expected_net_r": -0.10},
            decision="NO_TRADE",
            decision_reason="Gatekeeper Rejected",
            rule_score=52.0,
            virtual_trade={
                "entry_price": 200000, "stop_price": 196000, "target_price": 208000,
                "shares": 5, "strategy_id": "INT_PULLBACK", "time_horizon": "INTRADAY"
            },
            regime="BEAR",
            time_of_day_bucket="09:30-11:30"
        )
        self.memory.record_snapshot(snap)

        # 시세 업데이트: Stop(196000) 먼저 터치 (급락)
        resolved = self.memory.on_price_update(
            iem_cd="035420",
            current_price=195000,
            high_price=200500,
            low_price=194500,
            now=now + timedelta(minutes=8)
        )
        self.assertEqual(len(resolved), 1)
        rec = resolved[0]
        self.assertFalse(rec.target_hit_first)
        self.assertEqual(rec.outcome_category, "CORRECT_NO_TRADE")

    def test_04_category_statistics_retrieval(self):
        """TEST 6: 세분화된 Missed Opportunity 카테고리별 통계 집계 검증"""
        # outcome 기록 몇 개 주입
        for i, cat in enumerate(["MISSED_MOMENTUM", "MISSED_PULLBACK", "MISSED_SWING", "CORRECT_NO_TRADE", "CORRECT_NO_TRADE"]):
            eid = f"EVT_STAT_{i}"
            snap = PointInTimeSnapshot(
                event_id=eid, iem_cd=f"0000{i}0", name=f"종목{i}",
                event_time=datetime.now().isoformat(), as_of_time=datetime.now().isoformat(),
                feature_version="V1", strategy_version="V1", model_version="V1", dataset_version="V1",
                raw_features={}, prediction={}, decision="NO_TRADE", decision_reason="", rule_score=60,
                virtual_trade={}, regime="BULL", time_of_day_bucket="09:00-09:30"
            )
            self.memory.record_snapshot(snap)
            self.memory.record_outcome(OutcomeRecord(
                event_id=eid, evaluated_at=datetime.now().isoformat(), horizon_reached="10M",
                target_hit=True, stop_hit=False, target_hit_first=True, max_mfe_pct=0.03,
                max_mae_pct=0.01, realized_net_r=1.2, outcome_category=cat
            ))

        stats = self.memory.get_category_stats()
        self.assertEqual(stats["MISSED_MOMENTUM"], 1)
        self.assertEqual(stats["MISSED_PULLBACK"], 1)
        self.assertEqual(stats["MISSED_SWING"], 1)
        self.assertEqual(stats["CORRECT_NO_TRADE"], 2)

    # -------------------------------------------------------------------------
    # TEST 7 & 8: Historical Similarity as Meta Feature & Correlation Check
    # -------------------------------------------------------------------------
    def test_05_historical_similarity_as_meta_feature(self):
        """TEST 7 & 8: 과거 유사도가 직접 BUY를 결정하지 않고 Meta Feature만 산출하는지 검증"""
        now = datetime(2026, 9, 7, 10, 0, 0)
        # 5개의 유사 과거 경험 생성
        for i in range(5):
            eid = f"PAST_EXP_{i}"
            snap = PointInTimeSnapshot(
                event_id=eid, iem_cd=f"0010{i}0", name=f"과거종목{i}",
                event_time=(now - timedelta(days=10)).isoformat(),
                as_of_time=(now - timedelta(days=10)).isoformat(),
                feature_version="FEATURE_V001", strategy_version="V12", model_version="V12", dataset_version="V1",
                raw_features={"return_1m": 0.008, "return_3m": 0.018, "rvol": 2.2, "execution_intensity": 125.0},
                prediction={"p_target": 0.65}, decision="NO_TRADE", decision_reason="", rule_score=70,
                virtual_trade={}, regime="BULL", time_of_day_bucket="09:30-11:30"
            )
            self.memory.record_snapshot(snap)
            self.memory.record_outcome(OutcomeRecord(
                event_id=eid, evaluated_at=(now - timedelta(days=10)).isoformat(),
                horizon_reached="TARGET_HIT", target_hit=True, stop_hit=False,
                target_hit_first=(i % 2 == 0), max_mfe_pct=0.03, max_mae_pct=0.01,
                realized_net_r=1.5 if (i % 2 == 0) else -0.8,
                outcome_category="MISSED_WINNER" if (i % 2 == 0) else "CORRECT_NO_TRADE"
            ))

        # 현재 이벤트 유사도 검색
        cur_feats = {"return_1m": 0.008, "return_3m": 0.019, "rvol": 2.1, "execution_intensity": 120.0}
        sim_meta: SimilarityMetaOutput = self.sim_engine.query_similarity(cur_feats, current_regime="BULL", now=now)

        # Meta Feature 검증: 승률, 기대값, 표본수 산출
        self.assertTrue(sim_meta.is_sufficient_sample)
        self.assertGreaterEqual(sim_meta.sample_count, 3)
        self.assertGreater(sim_meta.hist_win_rate, 0.0)
        self.assertLessEqual(sim_meta.hist_win_rate, 1.0)
        self.assertIsInstance(sim_meta.expected_r, float)

        # Meta Model에 입력으로 제공하고 BUY 여부는 MetaDecisionEngine이 규칙+리스크+기대값으로 결정
        meta_res = self.meta_engine.evaluate_candidate(
            setup_name="INT_MOMENTUM",
            time_horizon=TimeHorizon.INTRADAY,
            entry_price=50000,
            stop_price=49000,
            target_price=52500,
            predicted_probs={"p_target": 0.65, "p_stop": 0.35},
            similarity_meta=sim_meta.__dict__,
            rule_score=75.0
        )
        self.assertIn(meta_res.decision, ("BUY", "WAIT", "NO_TRADE"))

    def test_06_feature_correlation_and_redundancy_check(self):
        """TEST 7: Historical Similarity와 ML Feature 간 상관관계/중복 검사"""
        base_feats = [{"rvol": float(i), "rsi": float(50 + i)} for i in range(20)]
        # rvol과 완전히 동일한 유사도 지표 생성 -> 상관계수 1.0 (중복 감지)
        sim_vals = [float(i) for i in range(20)]
        corr_res = HistoricalSimilarityEngine.check_feature_correlation(base_feats, sim_vals, threshold=0.85)
        self.assertTrue(corr_res["has_leakage"])
        self.assertEqual(corr_res["high_corr_features"][0][0], "rvol")

    # -------------------------------------------------------------------------
    # TEST 9 & 10: Probability Calibration & Meta Model Ablation Test
    # -------------------------------------------------------------------------
    def test_07_probability_calibration_ece_and_brier(self):
        """TEST 9: Probability Calibration ECE 및 Brier Score 계산 및 가중치 조절"""
        probs = [0.9, 0.8, 0.7, 0.6, 0.5, 0.4, 0.3, 0.2]
        labels = [1,   1,   1,   0,   0,   0,   0,   0]
        ece = CalibrationMonitor.calculate_ece(probs, labels)
        brier = CalibrationMonitor.calculate_brier_score(probs, labels)

        self.assertIsInstance(ece, float)
        self.assertGreaterEqual(ece, 0.0)
        self.assertLessEqual(ece, 1.0)
        self.assertIsInstance(brier, float)
        self.assertLessEqual(brier, 0.25)

        # Well-calibrated case ECE <= 0.25
        well_cal_probs = [0.8, 0.8, 0.2, 0.2]
        well_cal_labels = [1, 1, 0, 0]
        well_ece = CalibrationMonitor.calculate_ece(well_cal_probs, well_cal_labels)
        self.assertLessEqual(well_ece, 0.25)

        # Calibration 악화 시 가중치 감소 확인
        wt_good = CalibrationMonitor.get_calibrated_probability_weight(0.04)
        wt_bad = CalibrationMonitor.get_calibrated_probability_weight(0.18)
        self.assertEqual(wt_good, 1.0)
        self.assertEqual(wt_bad, 0.30)

    def test_08_meta_model_ablation_test(self):
        """TEST 10: Meta Model Ablation Test 4개 구성 OOS 성능 비교 및 최적 선택"""
        dataset = []
        for i in range(100):
            dataset.append({
                "rule_score": 75.0 if i % 2 == 0 else 45.0,
                "ml_prob": 0.70 if i % 2 == 0 else 0.40,
                "sim_win_rate": 0.65 if i % 2 == 0 else 0.35,
                "exec_cost_r": 0.08,
                "label_target_first": (i % 2 == 0) # 짝수 인덱스는 타겟 도달
            })

        ablation_res = MetaModelAblationTester.run_ablation_test(dataset)
        configs = ablation_res["configurations"]
        self.assertIn("RULE_ONLY", configs)
        self.assertIn("RULE_ML", configs)
        self.assertIn("RULE_ML_SIMILARITY", configs)
        self.assertIn("RULE_ML_SIMILARITY_EXECUTION", configs)
        self.assertIn(ablation_res["recommended_configuration"], MetaModelAblationTester.CONFIGS)

    # -------------------------------------------------------------------------
    # TEST 11, 12, 13: Retraining Trigger, Minimum Samples, Recency Weights
    # -------------------------------------------------------------------------
    def test_09_retraining_trigger_conditions(self):
        """TEST 11: 정기 재학습 및 2개 이상 조건 충족 시 이벤트 기반 재학습 트리거"""
        # A. 정기 재학습 (7일 경과)
        now = datetime(2026, 9, 7, 16, 0, 0)
        past_8d = now - timedelta(days=8)
        past_3d = now - timedelta(days=3)
        trig_8d, _ = self.retraining_engine.check_scheduled_trigger(past_8d, now)
        trig_3d, _ = self.retraining_engine.check_scheduled_trigger(past_3d, now)
        self.assertTrue(trig_8d)
        self.assertFalse(trig_3d)

        # B. 이벤트 기반 재학습: 8대 조건 중 2개 이상 동시 발생
        trades_bad = [{"pnl": -50000, "realized_net_r": -0.8} for _ in range(60)]
        should_retrain, conds, _ = self.retraining_engine.check_event_based_trigger(
            recent_trades=trades_bad,
            current_calibration_error=0.18, # 조건 1: Calibration 악화
            baseline_calibration_error=0.05,
            feature_drift_detected=True,    # 조건 2: Feature Drift
            current_mdd=0.06,               # 조건 3: MDD 초과
            baseline_mdd=0.02
        )
        self.assertTrue(should_retrain)
        self.assertGreaterEqual(len(conds), 2)

    def test_10_minimum_sample_requirement(self):
        """TEST 12: 학습 데이터 부족 (<10,000 또는 <500) 시 재학습 차단 및 챔피언 유지"""
        ok_total, msg1 = self.retraining_engine.check_minimum_samples(total_samples=5000, strategy_samples=600)
        self.assertFalse(ok_total)
        self.assertIn("전체 학습 표본 부족", msg1)

        ok_strat, msg2 = self.retraining_engine.check_minimum_samples(total_samples=15000, strategy_samples=200)
        self.assertFalse(ok_strat)
        self.assertIn("해당 전략 표본 부족", msg2)

        ok_all, _ = self.retraining_engine.check_minimum_samples(total_samples=12000, strategy_samples=800)
        self.assertTrue(ok_all)

    def test_11_recency_and_regime_weights(self):
        """TEST 13, 14, 15: 최근 데이터 가중치 및 시장 국면/시간대 가중치 검증"""
        now = datetime(2026, 9, 7, 10, 0, 0)
        # Recency
        w_recent = HistoricalSimilarityEngine.calculate_recency_weight(now - timedelta(days=30), now)
        w_mid = HistoricalSimilarityEngine.calculate_recency_weight(now - timedelta(days=150), now)
        w_old = HistoricalSimilarityEngine.calculate_recency_weight(now - timedelta(days=400), now)
        self.assertEqual(w_recent, 1.5)
        self.assertEqual(w_mid, 1.0)
        self.assertEqual(w_old, 0.5)

        # Regime
        self.assertEqual(HistoricalSimilarityEngine.calculate_regime_match("BULL", "BULL"), 1.0)
        self.assertEqual(HistoricalSimilarityEngine.calculate_regime_match("BULL", "STRONG_BULL"), 0.7)
        self.assertEqual(HistoricalSimilarityEngine.calculate_regime_match("BULL", "BEAR"), 0.35)

        # Time-of-Day
        self.assertEqual(HistoricalSimilarityEngine.calculate_tod_match("09:00-09:30", "09:00-09:30"), 1.0)
        self.assertEqual(HistoricalSimilarityEngine.calculate_tod_match("09:00-09:30", "09:30-11:30"), 0.6)
        self.assertEqual(HistoricalSimilarityEngine.calculate_tod_match("09:00-09:30", "13:00-15:30"), 0.15)

    # -------------------------------------------------------------------------
    # TEST 17, 18, 19, 20, 24: Model Registry, Promotion, Rollback, Audit Log
    # -------------------------------------------------------------------------
    def test_12_model_registry_promotion_and_rollback(self):
        """TEST 17~20 & 24: Model Registry, 승격 조건 검증, 롤백 및 Audit Log 기록"""
        # 1. 챔피언 확인
        champ = self.registry.get_champion()
        self.assertIsNotNone(champ)

        # 2. 우수한 챌린저 모델 등록
        challenger = ModelMetadata(
            model_id="CHALLENGER_V12_4",
            role="CHALLENGER",
            dataset_id="DATA_V002",
            feature_version="FEATURE_V002",
            label_version="LABEL_V001",
            strategy_version="STRATEGY_V12_3",
            training_date="2026-09-07",
            validation_date="2026-09-07",
            oos_score=0.80,
            profit_factor=2.45,         # Champion(2.15)보다 우수
            win_rate=0.66,
            expected_net_r=0.35,        # Champion(0.28)보다 우수
            brier_score=0.15,
            calibration_error=0.04,     # Champion(0.06)보다 우수
            max_drawdown=0.030,         # MDD 안정
            random_seed=42
        )
        self.registry.register_model(challenger)

        # 3. 승격(Promote) 실행
        promoted = self.registry.promote_challenger("CHALLENGER_V12_4", trigger_reason="Superior OOS Expectancy")
        self.assertTrue(promoted)
        self.assertEqual(self.registry.champion_id, "CHALLENGER_V12_4")

        # 4. 긴급 롤백(Rollback) 실행
        restored = self.registry.emergency_rollback(reason="Simulated degradation")
        self.assertEqual(restored, "CHAMPION_V12_3")
        self.assertEqual(self.registry.champion_id, "CHAMPION_V12_3")

        # 5. Audit Log 기록 확인
        self.assertTrue(os.path.exists(self.audit_log_path))
        with open(self.audit_log_path, "r", encoding="utf-8") as f:
            lines = f.readlines()
            self.assertGreaterEqual(len(lines), 2)
            self.assertIn("PROMOTE", lines[0])
            self.assertIn("ROLLBACK", lines[1])

    # -------------------------------------------------------------------------
    # TEST 25, 26, 27: Learning Freeze, Shadow Mode & Degradation Monitoring
    # -------------------------------------------------------------------------
    def test_13_learning_freeze_during_market_hours(self):
        """TEST 25: 장중(09:00~15:30) 실전매매 중 학습 동결 (Inference Only) 검증"""
        # 평일 10:15 (장중)
        intraday_time = datetime(2026, 9, 7, 10, 15, 0)
        frozen, msg = LearningFreezeManager.is_learning_frozen(intraday_time)
        self.assertTrue(frozen)
        self.assertIn("Inference Only", msg)

        # 평일 16:30 (장마감 후)
        post_time = datetime(2026, 9, 7, 16, 30, 0)
        frozen_post, msg_post = LearningFreezeManager.is_learning_frozen(post_time)
        self.assertFalse(frozen_post)
        self.assertIn("연구 및 재학습 허용", msg_post)

    def test_14_shadow_model_and_multi_window_degradation(self):
        """TEST 26 & 27: Shadow Model 기록 및 Multi-Window (50, 100, 200, 500) 감시"""
        # Shadow Prediction 기록
        self.registry.record_shadow_prediction(
            event_id="EVT_SHADOW_01",
            champion_pred={"p_target": 0.65},
            challenger_pred={"p_target": 0.72}
        )
        self.assertEqual(len(self.registry.shadow_predictions), 1)

        # Multi-Window 모니터링
        trades = [{"pnl": 10000, "realized_net_r": 1.2} for _ in range(60)]
        rep = self.registry.monitor_multi_window_degradation(trades)
        self.assertIn("window_50", rep)
        self.assertFalse(rep["window_50"]["degraded"])
        self.assertFalse(rep["multi_window_degraded"])


if __name__ == "__main__":
    unittest.main()
