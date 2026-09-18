"""[FINAL MASTER v16.0] 포지션 사이징 엔진 (risk/position_sizer.py)
Section 65: Position Sizing & Capital Allocation
- 고정 비율 위험(Fixed Fractional Risk) 모델:
  단타 1회 최대 Risk: Equity * 0.5%
  스윙 1회 최대 Risk: Equity * 1.0%
- 주문 수량 = Risk Amount / |Entry - Stop|
- 소액 계좌(예: 100만원 전후)에서 고가 우량주 0주 탈락 방지 (1주 최소 진입 및 자산 35% 한도 캡)
- 가용 현금, 슬리피지 예산, 포트폴리오 노출도 제약 전수 준수
"""

import math
from typing import Tuple
from core.models import TimeHorizon
from config.settings import INTRADAY_RISK_PER_TRADE, SWING_RISK_PER_TRADE
import config.settings as settings


class PositionSizer:
    @staticmethod
    def calculate_shares(
        time_horizon: TimeHorizon,
        equity: float,
        available_cash: float,
        entry_price: int,
        stop_price: int,
        allocated_capital: float = float("inf"),
        order_type: Optional[Any] = None
    ) -> Tuple[int, float, str]:
        """
        리스크 기반 정밀 주문 수량 산출
        :return: (shares, risk_amount, rationale)
        """
        if entry_price <= 0 or stop_price <= 0:
            return 0, 0.0, "가격 오류"

        stop_distance = abs(entry_price - stop_price)
        if stop_distance <= 0:
            # 안전 기본 손절폭 (2%)
            stop_distance = max(1, int(entry_price * 0.02))

        stop_ratio = stop_distance / float(entry_price)

        # 1. 타임프레임별 기본 리스크 비율 산출
        if time_horizon == TimeHorizon.INTRADAY:
            # 단타: 손절폭 3.5% 초과 시 진입 금지
            if stop_ratio > 0.035:
                return 0, 0.0, f"단타 손절폭 한도(3.5%) 초과: {stop_ratio*100:.2f}%"
            base_risk_ratio = INTRADAY_RISK_PER_TRADE  # 0.5%
            size_multiplier = 1.0
        else:
            # 스윙: > 12% 면 진입 금지, 8~12% 면 50% 축소
            if stop_ratio > 0.12:
                return 0, 0.0, f"스윙 손절폭 한도(12.0%) 초과: {stop_ratio*100:.2f}%"
            base_risk_ratio = SWING_RISK_PER_TRADE  # 1.0%
            size_multiplier = 0.5 if stop_ratio >= 0.08 else 1.0

        risk_amount = equity * base_risk_ratio * size_multiplier

        # 2. 이론적 매수 수량 = Risk Amount / |Entry - Stop|
        raw_shares = risk_amount / float(stop_distance)

        # 3. 가용 자금 및 배분 한도 제약 검증 (Section 4 & 5)
        risk_based_shares = int(math.floor(raw_shares))
        max_capital = min(available_cash, allocated_capital)

        # 시장가 주문(MARKET)의 경우 브로커 시장가 증거금(약 120~125%) 선점 버퍼 고려
        is_market = False
        if order_type is not None:
            is_market = (str(order_type).upper() in ("05", "MARKET", "ORDERTYPE.MARKET"))
        else:
            is_market = True  # 기본 시장가 주문

        market_margin_buffer = 1.25 if is_market else 1.0

        from risk.portfolio_cash import PortfolioCashManager
        effective_cost_per_share = float(entry_price * (market_margin_buffer + PortfolioCashManager.FEE_RATE + PortfolioCashManager.SLIPPAGE_BUFFER_RATE))
        cash_based_shares = max(0, int(math.floor(max_capital / effective_cost_per_share))) if effective_cost_per_share > 0 else 0

        cash_req = PortfolioCashManager.calculate_total_required_cash(max(1, risk_based_shares), entry_price * market_margin_buffer)

        if cash_req.total_required_cash > max_capital:
            # Section 5: ALLOW_PARTIAL_CASH_BUY = False인 경우 강제 축소 매수 금지 -> INSUFFICIENT_CASH
            if not getattr(settings, "ALLOW_PARTIAL_CASH_BUY", False):
                shortfall = cash_req.total_required_cash - max_capital
                return 0, 0.0, f"INSUFFICIENT_CASH: 필요금액({cash_req.total_required_cash:,.0f}원) > 가용현금({max_capital:,.0f}원) (부족: {shortfall:,.0f}원)"

            # ALLOW_PARTIAL_CASH_BUY가 True인 경우에만 가용 현금 범위로 축소
            final_shares = cash_based_shares
        else:
            final_shares = risk_based_shares

        # 소액 계좌(자산 200만원 이하)에서 자금 충분 시 최소 1주 진입 보장 (단일 종목 자산 35% 이내)
        if final_shares <= 0 and available_cash >= cash_req.total_required_cash and entry_price <= equity * 0.35:
            final_shares = 1

        if final_shares <= 0:
            return 0, 0.0, f"INSUFFICIENT_CASH: 가용 현금 부족 또는 수량 0주 (필요: {entry_price:,}원, 가용: {available_cash:,.0f}원)"

        actual_risk = final_shares * stop_distance
        rationale = (
            f"수량: {final_shares}주 (1R={stop_distance:,}원, 위험금액={actual_risk:,.0f}원, "
            f"리스크비율={actual_risk/equity*100:.2f}%, 배수={size_multiplier}x)"
        )
        return final_shares, actual_risk, rationale
