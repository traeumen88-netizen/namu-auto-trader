"""주문부 기반 엣지 계산 및 기대값 검증 엔진 (Edge Engine v9.3)
- Execution-Grade Specification v9.3
- 최우선 매도호가(Ask 1) 기준 현실적 진입비용 산정
- Expected Net R = P(Target)*Reward - P(Stop)*Risk - Cost >= +0.15R 검증
- ML 장애 시 Rule 기반 Fallback 확률 무중단 자동 전환 (ML Fail-Safe)
"""

from dataclasses import dataclass, field
from typing import Optional, Tuple, Dict, Any
from core.models import SymbolInfo, TradeSignal
from core.tick_normalizer import get_tick_size, normalize_price


@dataclass
class EdgeResult:
    """엣지 산출 및 검증 결과"""
    is_approved: bool
    expected_net_r: float
    entry_price: int
    stop_price: int
    target_price: int
    reward_r: float
    risk_r: float
    cost_r: float
    p_target: float
    p_stop: float
    rejection_reason: Optional[str] = None
    metadata: Dict[str, Any] = field(default_factory=dict)

    @property
    def risk_reward_ratio(self) -> float:
        return self.reward_r


class EdgeEngine:
    """호가창 기반 엣지 계산 및 수학적 기대값 필터링 엔진"""

    def __init__(
        self,
        min_expected_net_r: float = 0.15,
        roundtrip_cost_ratio: float = 0.0023  # 수수료(0.015%*2) + 거래세(0.20%) + 슬리피지(0.05%) ~= 0.23%
    ):
        self.min_expected_net_r = min_expected_net_r
        self.roundtrip_cost_ratio = roundtrip_cost_ratio

    def calculate_fallback_probability(self, score: float) -> Tuple[float, float]:
        """
        ML Fail-Safe: ML 모델 또는 피처 장애 시 Rule 점수 기반 확률 보정
        P(Target) = min(0.70, max(0.50, 0.50 + (score - 60) * 0.005))
        P(Stop) = 1.0 - P(Target)
        """
        if score < 60.0:
            return 0.50, 0.50
        p_target = min(0.70, max(0.50, 0.50 + (score - 60.0) * 0.005))
        p_stop = 1.0 - p_target
        return round(p_target, 4), round(p_stop, 4)

    def calculate_edge(
        self,
        sym: SymbolInfo,
        signal: TradeSignal,
        ask1_price: Optional[int] = None,
        p_target: Optional[float] = None,
        p_stop: Optional[float] = None,
        ml_model: Any = None
    ) -> EdgeResult:
        """
        호가창 최우선 매도호가(Ask 1)를 기반으로 Expected Net R 계산
        """
        # 1. 진입 가격 결정 (Ask 1 우선 -> 없으면 직전가 + 1틱)
        entry = 0
        if ask1_price and ask1_price > 0:
            entry = ask1_price
        elif sym.ask1_price > 0:
            entry = sym.ask1_price
        elif signal.ask1_price > 0:
            entry = signal.ask1_price
        else:
            base_p = sym.price if sym.price > 0 else signal.strategy_price
            tick = get_tick_size(base_p)
            entry = base_p + tick

        entry_price = normalize_price(entry, "BUY", "LIMIT")

        # 2. 손절가 및 익절가 확인
        stop_price = signal.stop_price
        if stop_price <= 0 or stop_price >= entry_price:
            reason = f"INVALID_STOP_PRICE (Stop: {stop_price} >= Entry: {entry_price})"
            sym.buy_block_reasons.append(reason)
            signal.rejection_reasons.append(reason)
            return EdgeResult(
                is_approved=False,
                expected_net_r=-999.0,
                entry_price=entry_price,
                stop_price=stop_price,
                target_price=0,
                reward_r=0.0,
                risk_r=1.0,
                cost_r=0.0,
                p_target=0.0,
                p_stop=1.0,
                rejection_reason=reason
            )

        # 3. 목표가 결정 (트레일링 및 2차 분할익절 고려 복합 기대치: target_2r 우선, 없을 시 최소 1.5R 산출)
        risk_krw = entry_price - stop_price
        target_price = signal.target_2r if (signal.target_2r and signal.target_2r > entry_price) else (
            signal.target_1r if (signal.target_1r and signal.target_1r > entry_price) else 0
        )
        if target_price <= entry_price:
            # 타겟이 미설정되거나 진입가 이하일 경우 기본 1.5R 산출
            target_price = normalize_price(entry_price + int(1.5 * risk_krw), "BUY", "PROFIT")
        elif (target_price - entry_price) < int(1.4 * risk_krw):
            # 1R만 설정되어 있어 손익비가 1.4R 미만인 경우 퀀트 기본 기대치(1.5R)로 보정
            target_price = normalize_price(entry_price + int(1.5 * risk_krw), "BUY", "PROFIT")

        # 4. R 단위 리스크, 리워드, 왕복 비용 계산
        risk_krw = entry_price - stop_price
        reward_krw = target_price - entry_price

        reward_r = reward_krw / risk_krw
        risk_r = 1.0

        cost_krw = entry_price * self.roundtrip_cost_ratio
        cost_r = cost_krw / risk_krw

        # 5. 확률 결정 (ML Fail-Safe 적용)
        pt = p_target
        ps = p_stop
        if pt is None or ps is None:
            if ml_model is not None:
                try:
                    pt, ps = ml_model.predict_prob(sym, signal)
                except Exception:
                    pt, ps = None, None

            if pt is None or ps is None:
                score = signal.score if signal.score > 0 else (signal.rule_score if signal.rule_score > 0 else 60.0)
                pt, ps = self.calculate_fallback_probability(score)

        # 6. Expected Net R 산출
        expected_net_r = (pt * reward_r) - (ps * risk_r) - cost_r
        is_approved = (expected_net_r >= self.min_expected_net_r)

        rejection_reason = None
        if not is_approved:
            rejection_reason = f"LOW_EXPECTED_EDGE ({expected_net_r:.3f}R < {self.min_expected_net_r:.2f}R)"
            sym.buy_block_reasons.append(rejection_reason)
            signal.rejection_reasons.append(rejection_reason)

        return EdgeResult(
            is_approved=is_approved,
            expected_net_r=round(expected_net_r, 4),
            entry_price=entry_price,
            stop_price=stop_price,
            target_price=target_price,
            reward_r=round(reward_r, 3),
            risk_r=risk_r,
            cost_r=round(cost_r, 4),
            p_target=pt,
            p_stop=ps,
            rejection_reason=rejection_reason,
            metadata={
                "risk_krw": risk_krw,
                "reward_krw": reward_krw,
                "cost_krw": round(cost_krw, 1),
                "is_fallback_prob": (ml_model is None or p_target is None)
            }
        )
