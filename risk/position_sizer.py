"""포지션 사이징 엔진 (Position Sizer)
- 고정 비율 위험(Fixed Fractional Risk) 모델:
  단타 1회 최대 Risk: Equity * 0.5%
  스윙 1회 최대 Risk: Equity * 1.0%
- 주문 수량 = Risk Amount / |Entry - Stop|
- 스윙 손절폭 8~12% 시 포지션 50% 감축, >12% 시 진입 금지
- 주문 가능 현금 및 가용 한도 초과 금지
"""

import math
from typing import Tuple
from core.models import TimeHorizon
from config.settings import INTRADAY_RISK_PER_TRADE, SWING_RISK_PER_TRADE


class PositionSizer:
    @staticmethod
    def calculate_shares(
        time_horizon: TimeHorizon,
        equity: float,
        available_cash: float,
        entry_price: int,
        stop_price: int,
        allocated_capital: float = float("inf")
    ) -> Tuple[int, float, str]:
        """
        리스크 기반 정밀 주문 수량 산출
        :return: (shares, risk_amount, rationale)
        """
        if entry_price <= 0 or stop_price <= 0:
            return 0, 0.0, "가격 오류"

        stop_distance = abs(entry_price - stop_price)
        if stop_distance <= 0:
            return 0, 0.0, "손절폭이 0 이하"

        stop_ratio = stop_distance / entry_price

        # 1. 타임프레임별 기본 리스크 비율 산출
        if time_horizon == TimeHorizon.INTRADAY:
            # 단타: 손절폭 > 3% 이면 신규 진입 금지
            if stop_ratio > 0.03:
                return 0, 0.0, f"단타 손절폭 한도(3.0%) 초과: {stop_ratio*100:.2f}%"
            base_risk_ratio = INTRADAY_RISK_PER_TRADE  # 0.5%
            size_multiplier = 1.0
        else:
            # 스윙: > 12% 면 진입 금지, 8~12% 면 포지션 규모 50% 축소
            if stop_ratio > 0.12:
                return 0, 0.0, f"스윙 손절폭 한도(12.0%) 초과: {stop_ratio*100:.2f}%"
            base_risk_ratio = SWING_RISK_PER_TRADE  # 1.0%
            size_multiplier = 0.5 if stop_ratio >= 0.08 else 1.0

        risk_amount = equity * base_risk_ratio * size_multiplier

        # 2. 이론적 매수 수량 = Risk Amount / |Entry - Stop|
        raw_shares = risk_amount / stop_distance

        # 3. 가용 자금 및 배분 한도 제약 검증
        max_capital = min(available_cash, allocated_capital)
        max_cash_shares = max_capital / entry_price

        final_shares = int(math.floor(min(raw_shares, max_cash_shares)))

        if final_shares <= 0:
            return 0, 0.0, "계산된 매수 수량이 0주 (자금 부족 또는 손절폭 과소)"

        actual_risk = final_shares * stop_distance
        rationale = (
            f"수량: {final_shares}주 (1R={stop_distance:,}원, 위험금액={actual_risk:,.0f}원, "
            f"리스크비율={actual_risk/equity*100:.2f}%, 배수={size_multiplier}x)"
        )
        return final_shares, actual_risk, rationale
