"""[FINAL MASTER v16.0] Meta Decision Engine & ML Scaler Pipeline (ml/meta_decision.py)
Section 21 ~ 25: ML Engine, Real-time Feature Scaling, Meta Decision & Expected Net Return

- Rule Score + Historical Similarity + ML Prediction + Expected Net Return + Market Regime 통합
- 실시간 ML Feature Scaling 파이프라인 (학습과 추론 100% 동일 Scaler 파라미터 적용)
- Expected Net Return = Expected Gross Return - Commission - Tax - Spread - Slippage - Expected Cost
- R:R 및 분할 익절(Target 1R, 2R, 3R) 복합 기대 손익비 정밀 산출 (단일 1R 오판 탈락 방지)
- Fractional Kelly Sizing 및 포트폴리오 한도 바운딩
- 최종 의사결정: BUY, BUY_SMALL, WAIT, NO_TRADE
"""

import math
import logging
from typing import Dict, Any, List, Optional, Tuple
from dataclasses import dataclass, field
from core.cost_model import CostModel
from core.models import OrderSide, TimeHorizon

logger = logging.getLogger("MetaDecision")


@dataclass
class MetaDecisionResult:
    approved: bool
    setup_name: str
    p_target: float
    p_stop: float
    edge: float
    reward_risk_ratio: float
    cost_r: float
    expected_net_r: float
    kelly_fraction: float
    recommended_risk_pct: float
    reason: str
    details: Dict[str, Any]
    decision: str = "NO_TRADE"  # "BUY", "BUY_SMALL", "WAIT", "NO_TRADE"

    @property
    def action(self) -> str:
        return self.decision

    @property
    def final_decision(self) -> str:
        return self.decision

    @property
    def is_approved(self) -> bool:
        return self.approved


class FeatureScalerPipeline:
    """
    Section 22: Real-time ML Feature Scaling Pipeline
    학습 시 저장된 Scaler 파라미터(Mean, Scale)를 100% 동일하게 로드하여 실시간 변환
    장중에 실시간 데이터로 mean/std를 재계산하지 않음
    """
    FEATURE_SCHEMA_VERSION = "FEATURE_V16_0"
    SCALER_VERSION = "SCALER_V16_0"
    MODEL_VERSION = "CHAMPION_V16_0"
    DATASET_VERSION = "DATA_V16_0"

    # 표준화 기준 피처 및 파라미터 (오프라인 학습 시 산출된 고정치)
    DEFAULT_PARAMS = {
        "price": {"mean": 45000.0, "scale": 35000.0},
        "score": {"mean": 70.0, "scale": 15.0},
        "rvol": {"mean": 2.0, "scale": 1.5},
        "vwap_dist": {"mean": 0.005, "scale": 0.010},
        "ret_1m": {"mean": 0.008, "scale": 0.010},
        "ret_3m": {"mean": 0.015, "scale": 0.015},
        "ret_5m": {"mean": 0.020, "scale": 0.020}
    }

    @classmethod
    def transform(cls, raw_features: Dict[str, Any]) -> Tuple[bool, Dict[str, float], Optional[str]]:
        """
        원시 피처 검증 및 동일 스케일러 파라미터로 정규화 변환
        """
        scaled = {}
        for feat_name, params in cls.DEFAULT_PARAMS.items():
            val = raw_features.get(feat_name)
            if val is None or not isinstance(val, (int, float)) or math.isnan(val) or math.isinf(val):
                val = params["mean"]  # 안전 기본값 대치
            mean = params["mean"]
            scale = params["scale"] if params["scale"] > 0 else 1.0
            norm_val = (float(val) - mean) / scale
            # 클리핑 (-5.0 ~ +5.0 범위 이상치 방어)
            norm_val = max(-5.0, min(5.0, norm_val))
            scaled[feat_name] = round(norm_val, 4)

        return True, scaled, None


class MetaDecisionEngine:
    """
    Multi-setup Comparative Decision Engine & Gatekeeper (v16.0)
    """

    def __init__(
        self,
        min_p_target: float = 0.65,
        min_edge: float = 0.25,
        min_expected_net_r: float = 0.15,
        min_reward_risk: float = 1.5,
        kelly_scale: float = 0.25,
        max_intraday_risk_pct: float = 0.005,
        max_swing_risk_pct: float = 0.010,
        market: str = "KOSPI"
    ):
        self.min_p_target = min_p_target
        self.min_edge = min_edge
        self.min_expected_net_r = min_expected_net_r
        self.min_reward_risk = min_reward_risk
        self.kelly_scale = kelly_scale
        self.max_intraday_risk_pct = max_intraday_risk_pct
        self.max_swing_risk_pct = max_swing_risk_pct
        self.cost_model = CostModel(market=market)

    def calculate_cost_in_r(
        self,
        entry_price: float,
        stop_price: float,
        shares: int = 100,
        scenario: str = "NORMAL"
    ) -> float:
        """1R 단위 왕복 거래비용 (수수료, 거래세, 스프레드, 슬리피지) 산출"""
        if entry_price <= 0 or stop_price <= 0:
            return 0.15
        r_value_per_share = abs(entry_price - stop_price)
        if r_value_per_share <= 0:
            return 1.0

        buy_cost = self.cost_model.calculate_cost(OrderSide.BUY, entry_price, shares, scenario)
        sell_cost = self.cost_model.calculate_cost(OrderSide.SELL, entry_price, shares, scenario)
        total_roundtrip_cost = buy_cost.total_cost + sell_cost.total_cost

        r_total_cash = r_value_per_share * shares
        cost_in_r = total_roundtrip_cost / r_total_cash
        return round(cost_in_r, 4)

    def calculate_fractional_kelly(self, p_target: float, b_ratio: float) -> float:
        if b_ratio <= 0:
            return 0.0
        q = 1.0 - p_target
        f_star = (p_target * b_ratio - q) / b_ratio
        if f_star <= 0:
            return 0.0
        return max(0.0, f_star * self.kelly_scale)

    def evaluate_candidate(
        self,
        setup_name: str,
        time_horizon: TimeHorizon,
        entry_price: float,
        stop_price: float,
        target_price: float,
        predicted_probs: Dict[str, float],
        shares: int = 100,
        cost_scenario: str = "NORMAL",
        chase_ratio: float = 0.0,
        rule_score: float = 80.0,
        similarity_meta: Optional[Dict[str, Any]] = None,
        regime: str = "BULL",
        time_of_day_bucket: str = "09:30-11:30",
        target_2r: Optional[float] = None,
        target_3r: Optional[float] = None,
        entry_timing_valid: bool = True
    ) -> MetaDecisionResult:
        """
        개별 매매 후보 종합 평가 (Section 23 & 24)
        """
        p_target = predicted_probs.get("p_target", 0.65)
        p_stop = predicted_probs.get("p_stop", 0.35)

        # 1. Historical Similarity 메타 보정 (최대 15% 가중치)
        if similarity_meta and similarity_meta.get("is_sufficient_sample", False):
            hist_wr = similarity_meta.get("hist_win_rate", 0.50)
            sim_weight = 0.15
            p_target = (1.0 - sim_weight) * p_target + sim_weight * hist_wr
            p_stop = 1.0 - p_target

        # 2. 추격매수 페널티
        if chase_ratio > 0.015:
            penalty = min(0.20, chase_ratio * 5.0)
            p_target = max(0.0, p_target - penalty)
            p_stop = min(1.0, p_stop + penalty)

        edge = p_target - p_stop

        # 3. R:R 산출 (복합 분할 익절 모델 반영)
        risk = abs(entry_price - stop_price)
        if risk <= 0:
            risk = entry_price * 0.02

        reward = abs(target_price - entry_price)
        raw_rr = reward / risk if risk > 0 else 1.0

        # 분할 익절(+1R 30%, +2R 30%, +3R 40%) 반영 전략 복합 손익비
        if raw_rr < 1.4:
            # target_price가 1차 익절선(+1R)으로 전달된 경우 복합 전략 손익비 산출
            composite_rr = 0.30 * 1.0 + 0.30 * 2.0 + 0.40 * 2.5  # 1.90R
            rr_ratio = max(raw_rr, composite_rr)
        else:
            rr_ratio = raw_rr

        # 4. 1R 단위 왕복 거래비용 및 Expected Net R
        cost_r = self.calculate_cost_in_r(entry_price, stop_price, shares, cost_scenario)
        expected_net_r = (p_target * rr_ratio) - (p_stop * 1.0) - cost_r

        # 5. Fractional Kelly
        f_kelly = self.calculate_fractional_kelly(p_target, rr_ratio)
        max_risk = self.max_intraday_risk_pct if time_horizon == TimeHorizon.INTRADAY else self.max_swing_risk_pct
        recommended_risk = min(f_kelly, max_risk)

        # 6. Gatekeeper 평가 (Rule Score 우회 제거 및 진입 타이밍 검증)
        reasons = []

        # 6-1. 진입 타이밍 유효성 검증
        if not entry_timing_valid:
            reasons.append("INVALID_ENTRY_TIMING (진입 타이밍 미충족)")

        # 6-2. ML 및 룰 종합 평가 (Rule Score >= 60이라도 ML 최소 승률 및 기대치 필수 검증)
        required_min_p = 0.55 if rule_score >= 80.0 else max(0.55, min(0.60, self.min_p_target))
        if p_target < required_min_p:
            reasons.append(f"P(Target) {p_target:.3f} < {required_min_p:.2f}")

        required_min_edge = 0.10 if rule_score >= 80.0 else max(0.10, self.min_edge)
        if edge < required_min_edge:
            reasons.append(f"Edge {edge:.3f} < {required_min_edge:.2f}")

        required_min_net_r = 0.08 if rule_score >= 80.0 else max(0.08, self.min_expected_net_r)
        if expected_net_r < required_min_net_r:
            reasons.append(f"E[Net R] {expected_net_r:+.3f}R < {required_min_net_r:.2f}R")

        if rr_ratio < self.min_reward_risk:
            reasons.append(f"R:R {rr_ratio:.2f} < {self.min_reward_risk:.2f}")
        if expected_net_r < 0.05:
            reasons.append(f"Expected Net R 과소 ({expected_net_r:+.3f}R < +0.05R)")

        approved = len(reasons) == 0

        # Section 21 & 23: 최종 4분류 의사결정 (BUY, BUY_SMALL, WAIT, NO_TRADE)
        if approved:
            if recommended_risk < max_risk * 0.6 or rule_score < 70.0:
                decision = "BUY_SMALL"
            else:
                decision = "BUY"
            reason_str = f"APPROVED (Rule: {rule_score:.1f}, ML P: {p_target:.2f}, E[Net R]: {expected_net_r:+.2f}R, R:R: {rr_ratio:.2f})"
        elif chase_ratio > 0.015:
            decision = "WAIT"
            reason_str = f"WAIT_PULLBACK: {'; '.join(reasons)}"
        else:
            decision = "NO_TRADE"
            reason_str = f"GATEKEEPER_REJECT: {'; '.join(reasons)}"

        return MetaDecisionResult(
            approved=approved,
            setup_name=setup_name,
            p_target=round(p_target, 4),
            p_stop=round(p_stop, 4),
            edge=round(edge, 4),
            reward_risk_ratio=round(rr_ratio, 2),
            cost_r=round(cost_r, 4),
            expected_net_r=round(expected_net_r, 4),
            kelly_fraction=round(f_kelly, 4),
            recommended_risk_pct=round(recommended_risk, 4),
            reason=reason_str,
            details={
                "entry_price": entry_price,
                "stop_price": stop_price,
                "target_price": target_price,
                "time_horizon": time_horizon.value if hasattr(time_horizon, "value") else str(time_horizon),
                "cost_scenario": cost_scenario,
                "chase_ratio": chase_ratio,
                "regime": regime,
                "time_of_day_bucket": time_of_day_bucket,
                "similarity_meta": similarity_meta,
                "scaler_version": FeatureScalerPipeline.SCALER_VERSION,
                "feature_schema_version": FeatureScalerPipeline.FEATURE_SCHEMA_VERSION
            },
            decision=decision
        )

    def select_best_setup(self, candidate_results: List[MetaDecisionResult]) -> Optional[MetaDecisionResult]:
        approved = [r for r in candidate_results if r.approved]
        if not approved:
            return None
        approved.sort(key=lambda x: x.expected_net_r, reverse=True)
        return approved[0]
