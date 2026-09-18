"""v12.3 Retraining Trigger, Calibration Monitor & Ablation Tester (ml/retraining_trigger.py)
[FINAL PATCH v12.3] Section 9, 10, 11, 12, 13, 25

Key Responsibilities:
1. Learning Freeze (Section 25):
   - Intraday (09:00 ~ 15:30): Inference Only. Production model is NEVER trained during trading.
   - Post-market / Weekend: Research & Retraining permitted.
2. Scheduled Retraining (Section 11.A):
   - 7 days, 14 days, 30 days configurable timer.
3. Event-Based Retraining Trigger (Section 11.B):
   - Triggers RETRAINING_REQUEST if 2 or more of the 8 conditions are met:
     (1) Recent 200 Expectancy drop
     (2) Profit Factor drop
     (3) Calibration deterioration
     (4) Feature Drift
     (5) Prediction Drift
     (6) MDD increase
     (7) Specific strategy performance collapse
     (8) Execution cost increase
4. Minimum Sample Requirement (Section 12):
   - Total samples < 10,000 OR Strategy samples < 500 -> Keep Champion.
5. Probability Calibration (Section 9):
   - Continuously computes ECE & Brier Score; degrades probability weight if calibration worsens.
6. Meta Model Ablation Test (Section 10):
   - Compares:
     1) RULE ONLY
     2) RULE + ML
     3) RULE + ML + SIMILARITY
     4) RULE + ML + SIMILARITY + EXECUTION
   - Enforces simplicity if added complexity does not improve OOS performance.
"""

import math
from datetime import datetime, time
from typing import Dict, Any, List, Optional, Tuple
import numpy as np


class LearningFreezeManager:
    """
    Section 25: 장중 실전매매 중 학습 동결 (Learning Freeze)
    장중: Inference Only / 장 종료 후: Research & Training
    """
    MARKET_OPEN = time(9, 0)
    MARKET_CLOSE = time(15, 30)

    @classmethod
    def is_learning_frozen(cls, now: Optional[datetime] = None) -> Tuple[bool, str]:
        now = now or datetime.now()
        # 주말(토=5, 일=6)은 장마감 상태 -> 학습 가능
        if now.weekday() >= 5:
            return False, "주말: 연구 및 재학습 허용"

        cur_time = now.time()
        if cls.MARKET_OPEN <= cur_time <= cls.MARKET_CLOSE:
            return True, "장중 실전매매 시간(09:00~15:30): Inference Only (학습 동결)"

        return False, "장 마감 후(15:30 이후): 연구 및 재학습 허용"


class RetrainingTriggerEngine:
    """
    Section 11 & 12: 정기 및 이벤트 기반 복합 재학습 트리거 엔진
    """

    def __init__(
        self,
        scheduled_days: int = 7,
        min_total_samples: int = 10000,
        min_strategy_samples: int = 500
    ):
        self.scheduled_days = scheduled_days
        self.min_total_samples = min_total_samples
        self.min_strategy_samples = min_strategy_samples

    def check_minimum_samples(self, total_samples: int, strategy_samples: int = 1000) -> Tuple[bool, str]:
        """
        Section 12: Minimum Sample Requirement
        전체 데이터 < 10,000 또는 전략 데이터 < 500이면 기존 Champion 유지
        """
        if total_samples < self.min_total_samples:
            return False, f"전체 학습 표본 부족 ({total_samples:,} < {self.min_total_samples:,})"
        if strategy_samples < self.min_strategy_samples:
            return False, f"해당 전략 표본 부족 ({strategy_samples:,} < {self.min_strategy_samples:,})"
        return True, "표본 수 충족"

    def check_scheduled_trigger(self, last_retrained_at: datetime, now: Optional[datetime] = None) -> Tuple[bool, str]:
        """
        Section 11.A: Scheduled Retraining (주 1회 또는 설정 주기)
        """
        now = now or datetime.now()
        days_passed = (now - last_retrained_at).total_seconds() / 86400.0
        if days_passed >= self.scheduled_days:
            return True, f"정기 재학습 주기 도래 ({days_passed:.1f}일 경과 >= {self.scheduled_days}일)"
        return False, f"정기 재학습 주기 미달 ({days_passed:.1f}일 < {self.scheduled_days}일)"

    def check_event_based_trigger(
        self,
        recent_trades: List[Dict[str, Any]],
        current_calibration_error: float = 0.05,
        baseline_calibration_error: float = 0.05,
        feature_drift_detected: bool = False,
        prediction_drift_detected: bool = False,
        current_mdd: float = 0.02,
        baseline_mdd: float = 0.03,
        strategy_win_rates: Optional[Dict[str, float]] = None,
        avg_execution_cost_r: float = 0.08
    ) -> Tuple[bool, List[str], Dict[str, Any]]:
        """
        Section 11.B: Event-Based Retraining Trigger
        8가지 조건 중 2개 이상 발생 시 RETRAINING REQUEST 트리거:
        1. 최근 200 거래 Expectancy 하락 (>= 20% 하락)
        2. Profit Factor 하락 (PF < 1.3)
        3. Calibration 악화 (ECE > 0.12 or +0.05 증가)
        4. Feature Drift 감지
        5. Prediction Drift 감지
        6. MDD 증가 (MDD > baseline + 0.02)
        7. 특정 전략의 성능 붕괴 (승률 < 35%)
        8. Execution Cost 증가 (비용 > 0.20R)
        """
        active_conditions = []
        condition_details = {}

        # 1. 최근 200 거래 기대값 하락 검사
        n_trades = len(recent_trades)
        if n_trades >= 50:
            sample_200 = recent_trades[-200:]
            avg_r = sum(t.get("realized_net_r", t.get("r_multiple", 0)) for t in sample_200) / len(sample_200)
            if avg_r < 0.05:
                active_conditions.append(f"최근 200거래 순기대값 하락 ({avg_r:+.2f}R < +0.05R)")
            condition_details["recent_expectancy_r"] = round(avg_r, 4)

            # 2. Profit Factor 하락 검사
            wins = [t.get("pnl", 0) for t in sample_200 if t.get("pnl", 0) > 0]
            losses = [abs(t.get("pnl", 0)) for t in sample_200 if t.get("pnl", 0) < 0]
            pf = (sum(wins) / sum(losses)) if sum(losses) > 0 else 99.0
            if pf < 1.30:
                active_conditions.append(f"Profit Factor 하락 ({pf:.2f} < 1.30)")
            condition_details["profit_factor"] = round(pf, 2)

        # 3. Calibration 악화 검사
        if current_calibration_error > max(0.12, baseline_calibration_error + 0.05):
            active_conditions.append(f"Calibration 악화 (ECE {current_calibration_error:.3f} > {baseline_calibration_error:.3f})")
        condition_details["calibration_error"] = round(current_calibration_error, 4)

        # 4. Feature Drift
        if feature_drift_detected:
            active_conditions.append("Feature Drift 감지 (입력 분포 이동)")
        condition_details["feature_drift"] = feature_drift_detected

        # 5. Prediction Drift
        if prediction_drift_detected:
            active_conditions.append("Prediction Drift 감지 (예측 확률 분포 이동)")
        condition_details["prediction_drift"] = prediction_drift_detected

        # 6. MDD 증가
        if current_mdd > baseline_mdd + 0.02:
            active_conditions.append(f"MDD 증가 ({current_mdd*100:.1f}% > {(baseline_mdd+0.02)*100:.1f}%)")
        condition_details["current_mdd"] = round(current_mdd, 4)

        # 7. 특정 전략 붕괴 검사
        if strategy_win_rates:
            collapsed = [s for s, wr in strategy_win_rates.items() if wr < 0.35]
            if collapsed:
                active_conditions.append(f"전략 성능 붕괴 감지: {', '.join(collapsed)}")
            condition_details["strategy_win_rates"] = strategy_win_rates

        # 8. Execution Cost 증가
        if avg_execution_cost_r > 0.20:
            active_conditions.append(f"Execution Cost 급증 ({avg_execution_cost_r:.3f}R > 0.200R)")
        condition_details["avg_cost_r"] = round(avg_execution_cost_r, 4)

        # 조건 2개 이상 충족 시 재학습 요청
        should_retrain = len(active_conditions) >= 2
        return should_retrain, active_conditions, condition_details


class CalibrationMonitor:
    """
    Section 9: Probability Calibration 지속 평가 (ECE 및 Brier Score 계산)
    """

    @classmethod
    def calculate_ece(
        cls,
        probabilities: List[float],
        outcomes: List[int],
        n_bins: int = 10
    ) -> float:
        """
        Expected Calibration Error (ECE)
        """
        if len(probabilities) == 0 or len(outcomes) == 0:
            return 0.0

        p = np.asarray(probabilities, dtype=np.float64)
        y = np.asarray(outcomes, dtype=np.float64)
        n = len(p)

        bins = np.linspace(0.0, 1.0, n_bins + 1)
        ece = 0.0

        for i in range(n_bins):
            bin_lower = bins[i]
            bin_upper = bins[i + 1]
            mask = (p >= bin_lower) & (p < bin_upper) if i < n_bins - 1 else (p >= bin_lower) & (p <= bin_upper)
            bin_count = np.sum(mask)

            if bin_count > 0:
                bin_acc = np.mean(y[mask])
                bin_conf = np.mean(p[mask])
                ece += (bin_count / n) * abs(bin_acc - bin_conf)

        return float(ece)

    @classmethod
    def calculate_brier_score(cls, probabilities: List[float], outcomes: List[int]) -> float:
        p = np.asarray(probabilities, dtype=np.float64)
        y = np.asarray(outcomes, dtype=np.float64)
        if len(p) == 0:
            return 0.25
        return float(np.mean((p - y) ** 2))

    @classmethod
    def get_calibrated_probability_weight(cls, ece: float) -> float:
        """
        Calibration 악화 시 Probability Weight 감소 [0.2 ~ 1.0]
        """
        if ece <= 0.05:
            return 1.0
        elif ece <= 0.10:
            return 0.85
        elif ece <= 0.15:
            return 0.60
        else:
            return 0.30  # 심각한 불일치 시 신뢰도 대폭 축소


class MetaModelAblationTester:
    """
    Section 10: Meta Model Ablation Test
    4가지 구성 비교 검증:
    1) RULE ONLY
    2) RULE + ML
    3) RULE + ML + SIMILARITY
    4) RULE + ML + SIMILARITY + EXECUTION
    복잡도 추가 대비 성과 미개선 시 더 단순한 모델 채택
    """

    CONFIGS = [
        "RULE_ONLY",
        "RULE_ML",
        "RULE_ML_SIMILARITY",
        "RULE_ML_SIMILARITY_EXECUTION"
    ]

    @classmethod
    def run_ablation_test(
        cls,
        test_dataset: List[Dict[str, Any]]
    ) -> Dict[str, Any]:
        """
        각 구성의 OOS Expected Net R 및 Win Rate 비교 산출
        """
        results = {}

        for cfg in cls.CONFIGS:
            net_r_list = []
            win_count = 0
            trade_count = 0

            for sample in test_dataset:
                rule_score = sample.get("rule_score", 70.0)
                ml_prob = sample.get("ml_prob", 0.60)
                sim_win_rate = sample.get("sim_win_rate", 0.55)
                exec_cost_r = sample.get("exec_cost_r", 0.08)
                label_target_first = sample.get("label_target_first", False)

                # 진입 판단
                approved = False
                if cfg == "RULE_ONLY":
                    approved = rule_score >= 60.0
                elif cfg == "RULE_ML":
                    approved = (rule_score >= 60.0 and ml_prob >= 0.50) or (ml_prob >= 0.65)
                elif cfg == "RULE_ML_SIMILARITY":
                    approved = (rule_score >= 60.0 and ml_prob >= 0.48 and sim_win_rate >= 0.45)
                elif cfg == "RULE_ML_SIMILARITY_EXECUTION":
                    raw_expected = (ml_prob * 2.0) - ((1.0 - ml_prob) * 1.0)
                    net_expected = raw_expected - exec_cost_r
                    approved = (rule_score >= 55.0 and net_expected >= 0.15 and sim_win_rate >= 0.45)

                if approved:
                    trade_count += 1
                    if label_target_first:
                        win_count += 1
                        net_r_list.append(2.0 - exec_cost_r)
                    else:
                        net_r_list.append(-1.0 - exec_cost_r)

            win_rate = (win_count / trade_count) if trade_count > 0 else 0.0
            avg_net_r = float(np.mean(net_r_list)) if len(net_r_list) > 0 else 0.0

            results[cfg] = {
                "trades": trade_count,
                "win_rate": round(win_rate, 4),
                "expected_net_r": round(avg_net_r, 4)
            }

        # 최적 구성 선택: expected_net_r 최고치 중 복잡도 패널티 고려
        best_cfg = "RULE_ONLY"
        best_r = -999.0
        for cfg in cls.CONFIGS:
            r = results[cfg]["expected_net_r"]
            if r > best_r + 0.03: # 최소 0.03R 이상 유의미한 개선이 있을 때만 더 복잡한 모델 채택
                best_r = r
                best_cfg = cfg

        return {
            "configurations": results,
            "recommended_configuration": best_cfg,
            "best_expected_net_r": round(best_r, 4)
        }
