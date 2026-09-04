"""기회 및 순기대값 예측 모델 (Opportunity Predictor v7.0)
- Execution-Grade Specification v7.0 (Section 5, 9, 11, 15, 17, 53 준수)
- LightGBM / Scikit-learn GBDT 앙상블 기반
- P(Target), P(Stop), Expected Net Return (E[Net R]), Expected Holding Time, MAE, MFE 산출
- Probability Calibration (Platt Scaling) 통합
"""

import os
import json
import logging
from dataclasses import dataclass
from typing import Dict, List, Any, Optional, Tuple
import numpy as np

try:
    import lightgbm as lgb
    HAS_LIGHTGBM = True
except ImportError:
    HAS_LIGHTGBM = False

try:
    from sklearn.ensemble import GradientBoostingClassifier, GradientBoostingRegressor
    HAS_SKLEARN = True
except ImportError:
    HAS_SKLEARN = False

from ml.features import QuantitativeFeatureEngine
from ml.calibrator import ProbabilityCalibrator

logger = logging.getLogger("OpportunityModel")


@dataclass
class PredictionOutput:
    p_target: float            # 목표가 도달 확률 [0.0, 1.0]
    p_stop: float              # 손절가 도달 확률 [0.0, 1.0]
    expected_net_r: float      # 거래비용 차감 후 순기대값 (R-Unit)
    expected_holding_bars: int # 예상 보유 봉 수
    expected_mae: float        # 예상 최대 불리 역행 (%)
    expected_mfe: float        # 예상 최대 유리 진행 (%)
    model_confidence: float    # 모델 신뢰도 [0.0, 1.0]
    is_buy_candidate: bool     # BUY 후보 기준 충족 여부
    is_a_plus: bool            # A+ 프리미엄 등급 여부
    rationale: str             # 판단 근거 요약


class OpportunityPredictor:
    def __init__(
        self,
        model_id: str = "CHAMPION_V7_0",
        horizon: str = "INTRADAY",
        version: Optional[str] = None,
        model_dir: Optional[str] = None
    ):
        self.model_id = version or model_id
        self.horizon = horizon  # 'INTRADAY' 또는 'SWING'
        self.model_dir = model_dir or "models/opportunity"
        self.calibrator_target = ProbabilityCalibrator(method="sigmoid")
        self.calibrator_stop = ProbabilityCalibrator(method="sigmoid")
        self.is_trained = False
        
        # 모델 파라미터 (Section 17 기준치)
        self.min_p_target = 0.65
        self.min_edge_spread = 0.25  # P(Target) - P(Stop) >= 0.25
        self.min_expected_r = 0.20   # E[Net R] >= +0.20R
        
        # A+ 기준
        self.a_plus_p_target = 0.72
        self.a_plus_expected_r = 0.35

        # 기본 모델 (LightGBM 또는 Sklearn GBDT)
        self.model_target = None
        self.model_stop = None
        self.feature_importances: Dict[str, float] = {}

    def train(
        self,
        X: Any,
        y_target: Any,
        y_stop: Optional[Any] = None,
        validation_split: float = 0.2,
        feature_names: Optional[List[str]] = None
    ):
        """
        X: (N, 52) Feature Matrix or List of Feature Dicts
        y_target: (N,) Binary (1 = Hit Target, 0 = Otherwise)
        y_stop: (N,) Binary (1 = Hit Stop, 0 = Otherwise), optional
        """
        if isinstance(X, list) and len(X) > 0 and isinstance(X[0], dict):
            X_arr = np.array([QuantitativeFeatureEngine.to_vector(d) for d in X], dtype=np.float32)
        else:
            X_arr = np.asarray(X, dtype=np.float32)

        yt_arr = np.asarray(y_target, dtype=np.int32)
        if y_stop is None or (isinstance(y_stop, list) and len(y_stop) > 0 and isinstance(y_stop[0], str)):
            if isinstance(y_stop, list) and len(y_stop) > 0 and isinstance(y_stop[0], str):
                feature_names = y_stop
            ys_arr = (yt_arr == 0).astype(np.int32)
        else:
            ys_arr = np.asarray(y_stop, dtype=np.int32)

        N = len(X_arr)
        if N < 20:
            logger.warning(f"학습 표본 부족 ({N}건), 기본 파라미터 유지")
            self.is_trained = True
            return

        split_idx = int(N * (1.0 - validation_split))
        X_train, X_val = X_arr[:split_idx], X_arr[split_idx:]
        yt_train, yt_val = yt_arr[:split_idx], yt_arr[split_idx:]
        ys_train, ys_val = ys_arr[:split_idx], ys_arr[split_idx:]

        if HAS_LIGHTGBM:
            params = {
                "objective": "binary",
                "learning_rate": 0.05,
                "num_leaves": 31,
                "max_depth": 5,
                "feature_fraction": 0.8,
                "min_child_samples": 10,
                "verbose": -1,
                "random_state": 42
            }
            dtrain_t = lgb.Dataset(X_train, label=yt_train)
            dval_t = lgb.Dataset(X_val, label=yt_val, reference=dtrain_t)
            self.model_target = lgb.train(
                params, dtrain_t, num_boost_round=100,
                valid_sets=[dval_t]
            )

            dtrain_s = lgb.Dataset(X_train, label=ys_train)
            dval_s = lgb.Dataset(X_val, label=ys_val, reference=dtrain_s)
            self.model_stop = lgb.train(
                params, dtrain_s, num_boost_round=100,
                valid_sets=[dval_s]
            )

            # Feature Importance 산출
            importances = self.model_target.feature_importance(importance_type="gain")
            tot = sum(importances) or 1.0
            self.feature_importances = {
                feat: float(imp / tot)
                for feat, imp in zip(QuantitativeFeatureEngine.FEATURE_NAMES, importances)
            }

            # Probability Calibration Fitting
            raw_val_t = self.model_target.predict(X_val)
            raw_val_s = self.model_stop.predict(X_val)
            self.calibrator_target.fit(raw_val_t, yt_val)
            self.calibrator_stop.fit(raw_val_s, ys_val)

        elif HAS_SKLEARN:
            self.model_target = GradientBoostingClassifier(n_estimators=100, max_depth=4, random_state=42)
            self.model_target.fit(X_train, yt_train)

            self.model_stop = GradientBoostingClassifier(n_estimators=100, max_depth=4, random_state=42)
            self.model_stop.fit(X_train, ys_train)

            raw_val_t = self.model_target.predict_proba(X_val)[:, 1]
            raw_val_s = self.model_stop.predict_proba(X_val)[:, 1]
            self.calibrator_target.fit(raw_val_t, yt_val)
            self.calibrator_stop.fit(raw_val_s, ys_val)

        self.is_trained = True
        logger.info(f"모델 [{self.model_id}] 학습 완료 (학습: {len(X_train)}건, 검증: {len(X_val)}건)")

    def predict(
        self,
        features: Dict[str, float],
        target_r: float = 1.5,
        stop_r: float = 1.0,
        cost_r: float = 0.15,
        slippage_r: float = 0.05
    ) -> PredictionOutput:
        """
        단일 종목 피처에 대해 정밀 확률, 순기대값, MAE/MFE 산출
        """
        x_vec = QuantitativeFeatureEngine.to_vector(features).reshape(1, -1)

        if self.is_trained and (self.model_target is not None):
            if HAS_LIGHTGBM and isinstance(self.model_target, lgb.Booster):
                raw_p_t = float(self.model_target.predict(x_vec)[0])
                raw_p_s = float(self.model_stop.predict(x_vec)[0])
            elif HAS_SKLEARN and hasattr(self.model_target, "predict_proba"):
                raw_p_t = float(self.model_target.predict_proba(x_vec)[0, 1])
                raw_p_s = float(self.model_stop.predict_proba(x_vec)[0, 1])
            else:
                raw_p_t, raw_p_s = self._rule_heuristic_probabilities(features)
        else:
            # Cold-start 또는 초기 기본 모델: 퀀트 수급 휴리스틱 매핑
            raw_p_t, raw_p_s = self._rule_heuristic_probabilities(features)

        # 1. 캘리브레이션 보정
        cal_p_t = float(self.calibrator_target.predict_proba(np.array([raw_p_t]))[0])
        cal_p_s = float(self.calibrator_stop.predict_proba(np.array([raw_p_s]))[0])

        # 확률 합계 정규화 (P(Target) + P(Stop) <= 1.0 보장)
        if cal_p_t + cal_p_s > 0.95:
            scale = 0.95 / (cal_p_t + cal_p_s)
            cal_p_t *= scale
            cal_p_s *= scale

        # 2. 거래비용 차감 순기대값 (Section 11, 15)
        # E[Net R] = P(Target)*target_r - P(Stop)*stop_r - cost_r - slippage_r
        expected_net_r = (cal_p_t * target_r) - (cal_p_s * stop_r) - cost_r - slippage_r

        # 3. 과열 추격 페널티 (Section 18, 55)
        chase_penalty = 0.0
        if features.get("vwap_dist", 0.0) >= 0.030:
            chase_penalty += 0.20
        if features.get("ret_5m", 0.0) >= 0.040:
            chase_penalty += 0.20
        if features.get("ret_1m", 0.0) >= 0.020:
            chase_penalty += 0.15

        cal_p_t = max(0.01, cal_p_t - chase_penalty)
        expected_net_r -= chase_penalty

        # 4. MAE, MFE, 예상 보유시간 산출
        atr_r = features.get("atr_ratio", 0.005)
        expected_mae = float(atr_r * stop_r * 100.0)
        expected_mfe = float(atr_r * target_r * 100.0)
        expected_holding_bars = 7 if self.horizon == "INTRADAY" else 15

        # 5. 모델 신뢰도 산출
        model_confidence = float(min(1.0, max(0.5, 0.70 + (cal_p_t - cal_p_s) * 0.5)))

        # 6. BUY Candidate & A+ 판정 (Section 17)
        is_buy = (
            (cal_p_t >= self.min_p_target)
            and ((cal_p_t - cal_p_s) >= self.min_edge_spread)
            and (expected_net_r >= self.min_expected_r)
        )
        is_a_plus = (
            is_buy
            and (cal_p_t >= self.a_plus_p_target)
            and (expected_net_r >= self.a_plus_expected_r)
        )

        rationale = (
            f"P(Target)={cal_p_t*100:.1f}%, P(Stop)={cal_p_s*100:.1f}%, "
            f"순기대값={expected_net_r:+.2f}R (비용={cost_r+slippage_r:.2f}R 차감후)"
        )

        return PredictionOutput(
            p_target=cal_p_t,
            p_stop=cal_p_s,
            expected_net_r=expected_net_r,
            expected_holding_bars=expected_holding_bars,
            expected_mae=expected_mae,
            expected_mfe=expected_mfe,
            model_confidence=model_confidence,
            is_buy_candidate=is_buy,
            is_a_plus=is_a_plus,
            rationale=rationale
        )

    def _rule_heuristic_probabilities(self, f: Dict[str, float]) -> Tuple[float, float]:
        """초기 콜드스타트용 퀀트 수급 기반 확률 추정 (Section 10, 19, 20 준수)"""
        score = 50.0  # 기본 점수

        # 거래량 / 거래대금 가속 (+20점)
        if f.get("rvol_5m", 1.0) >= 3.0:
            score += 15.0
        elif f.get("rvol_5m", 1.0) >= 2.0:
            score += 8.0

        if f.get("vol_accel", 0.0) >= 0.5:
            score += 5.0

        # 모멘텀 및 돌파 (+20점)
        if f.get("ret_3m", 0.0) >= 0.020:
            score += 10.0
        if f.get("dist_pdh", -1.0) >= 0.0:
            score += 10.0

        # 차트 구조 (+25점)
        if f.get("is_uptrend_structure", 0.0) == 1.0:
            score += 10.0
        if f.get("higher_highs", 0.0) == 1.0:
            score += 5.0
        if f.get("higher_lows", 0.0) == 1.0:
            score += 5.0
        if f.get("is_above_vwap", 0.0) == 1.0:
            score += 5.0

        # 압축 후 돌파 (+10점)
        if f.get("volatility_compression", 0.0) == 1.0:
            score += 10.0

        # 호가 및 체결강도 (+10점)
        if f.get("execution_intensity", 100.0) >= 120.0:
            score += 5.0
        if f.get("order_book_imbalance", 0.0) >= 0.25:
            score += 5.0

        p_t = np.clip(score / 100.0, 0.20, 0.85)
        p_s = np.clip(1.0 - p_t - 0.10, 0.10, 0.50)
        return float(p_t), float(p_s)

    def predict_opportunity(self, features: Dict[str, float]) -> Dict[str, float]:
        """Convenience method returning a dictionary of probabilities and metrics."""
        out = self.predict(features)
        return {
            "p_target": out.p_target,
            "p_stop": out.p_stop,
            "expected_net_r": out.expected_net_r,
            "is_buy": out.is_buy_candidate,
            "is_a_plus": out.is_a_plus,
            "confidence": out.model_confidence,
            "expected_mae": out.expected_mae,
            "expected_mfe": out.expected_mfe,
            "expected_holding_bars": out.expected_holding_bars
        }

    def save(self, filepath: Optional[str] = None):
        target_path = filepath or os.path.join(self.model_dir, f"{self.model_id}.meta")
        os.makedirs(os.path.dirname(target_path), exist_ok=True)
        with open(target_path, "w", encoding="utf-8") as f:
            json.dump({
                "model_id": self.model_id,
                "horizon": self.horizon,
                "is_trained": self.is_trained,
                "feature_importances": self.feature_importances
            }, f, indent=2)

    def load(self, filepath: Optional[str] = None):
        target_path = filepath or os.path.join(self.model_dir, f"{self.model_id}.meta")
        if os.path.exists(target_path):
            with open(target_path, "r", encoding="utf-8") as f:
                data = json.load(f)
                self.model_id = data.get("model_id", self.model_id)
                self.horizon = data.get("horizon", self.horizon)
                self.is_trained = data.get("is_trained", False)
                self.feature_importances = data.get("feature_importances", {})

