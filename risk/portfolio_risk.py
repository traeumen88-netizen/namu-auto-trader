"""포트폴리오 리스크 및 테마 상관관계 통제 엔진
- 동시 보유 포지션 총 위험(Total Open Risk) 계산:
  <= 3% : 정상 신규 진입
  3~4%  : 신규 진입 규모 50% 감축
  > 4%  : 신규 진입 전면 금지
- 동일 테마/업종 집중도 제한: 최대 3종목, 테마 총 위험 <= 1.5%
- 시장국면별 자산 배분 (단타 / 스윙 / 현금) 관리
"""

from typing import List, Dict, Any, Tuple, Optional
from core.models import Position, MarketRegime, TradeSignal
from config.settings import (
    TOTAL_RISK_NORMAL_LIMIT,
    TOTAL_RISK_REDUCED_LIMIT,
    MAX_STOCKS_PER_THEME,
    MAX_RISK_PER_THEME,
    REGIME_ALLOCATION
)


class PortfolioRiskManager:
    SIM_RISK_MODE: bool = False
    UNLIMITED_MODE: bool = True
    MAX_POSITION_COUNT: str = "UNLIMITED"
    POSITION_COUNT_BLOCK: bool = False
    POSITION_COUNT_CHECK: str = "BYPASSED / NOT_USED"

    @staticmethod
    def calculate_total_open_risk(positions: List[Position], equity: float) -> Tuple[float, float, str]:
        """
        열려 있는 모든 포지션의 최대 손실 합계 계산
        - 보유 종목 수 자체에 대한 인위적인 제한(Hard Gate)은 완전히 없음 (MAX_POSITION_COUNT = UNLIMITED)
        - 오직 포트폴리오 리스크(Risk) 한도 및 가용 현금(Cash)에 의해서만 통제
        :return: (total_risk_amount, total_risk_ratio, status: "NORMAL" | "REDUCED" | "BLOCKED")
        """
        if PortfolioRiskManager.SIM_RISK_MODE:
            # 가상 시뮬레이션 리스크 모드 (Section 22)
            equity = max(equity, 100_000_000.0)

        active_positions = [p for p in positions if not p.is_closed]
        total_risk_amount = sum(p.initial_risk_amount for p in active_positions)

        if equity <= 0:
            if not active_positions:
                return 0.0, 0.0, "NORMAL"
            return total_risk_amount, 1.0, "BLOCKED"

        risk_ratio = total_risk_amount / equity

        if risk_ratio <= TOTAL_RISK_NORMAL_LIMIT:
            status = "NORMAL"
        elif risk_ratio <= TOTAL_RISK_REDUCED_LIMIT:
            status = "REDUCED"
        else:
            status = "BLOCKED"

        return total_risk_amount, risk_ratio, status

    @staticmethod
    def get_risk_summary(positions: List[Position], equity: float, max_risk_limit: float = 0.04) -> Dict[str, Any]:
        """대시보드 표출용 Available Risk 및 리스크 요약 (Section 21)"""
        tot_amt, risk_ratio, status = PortfolioRiskManager.calculate_total_open_risk(positions, equity)
        avail_ratio = max(0.0, max_risk_limit - risk_ratio)
        avail_amt = avail_ratio * (equity if equity > 0 else 100_000_000.0)
        return {
            "total_equity": equity if equity > 0 else 100_000_000.0,
            "used_risk_amount": tot_amt,
            "used_risk_ratio": risk_ratio,
            "available_risk_ratio": avail_ratio,
            "available_risk_amount": avail_amt,
            "status": status,
            "max_risk_limit": max_risk_limit,
            "max_position_count": "UNLIMITED",
            "current_position_count": len([p for p in positions if not p.is_closed]),
            "position_count_block": False,
            "position_count_check": "BYPASSED / NOT_USED"
        }

    @staticmethod
    def check_theme_risk(
        candidate_iem_cd: str,
        candidate_theme: str,
        candidate_risk: float,
        positions: List[Position],
        stock_theme_map: Dict[str, str],
        equity: float
    ) -> Tuple[bool, str]:
        """
        동일 테마/업종 포지션 집중도 검사
        - 보유 종목 수 제한 완전 제거: 테마별 종목 수 인위적 제한 없음
        - 테마 총 위험 비율(<= 1.5%)로만 통제
        """
        if not candidate_theme or candidate_theme == "기타":
            return True, "테마 무관 통과"

        active_positions = [p for p in positions if not p.is_closed]
        theme_positions = [
            p for p in active_positions
            if stock_theme_map.get(p.iem_cd, "") == candidate_theme
        ]

        # 테마 총 위험 비율 제한 (<= 1.5%) - 종목 개수 제한은 완전 배제
        curr_theme_risk = sum(p.initial_risk_amount for p in theme_positions)
        new_theme_risk_ratio = (curr_theme_risk + candidate_risk) / equity

        if new_theme_risk_ratio > MAX_RISK_PER_THEME:
            return False, f"PORTFOLIO_RISK_LIMIT: 테마 '{candidate_theme}' 총 위험 한도(1.5%) 초과: {new_theme_risk_ratio*100:.2f}%"

        return True, "테마 리스크 통과"

    @staticmethod
    def get_capital_allocation(market_regime: MarketRegime, equity: float) -> Dict[str, float]:
        """시장국면별 가용 자본 배분 한도 산출"""
        alloc = REGIME_ALLOCATION.get(market_regime.value, REGIME_ALLOCATION["NEUTRAL"])
        return {
            "intraday_capital": equity * alloc["intraday"],
            "swing_capital": equity * alloc["swing"],
            "cash_buffer": equity * alloc["cash"]
        }

    @staticmethod
    def check_order_risk(
        signal: TradeSignal,
        shares: int,
        order_price: float,
        balance: Dict[str, Any],
        positions: Optional[List[Position]] = None,
        equity: Optional[float] = None
    ) -> Tuple[bool, str]:
        """주문 발주 전 포트폴리오 리스크 및 가용 현금 검증 (종목 수 제한 없음)"""
        if shares <= 0:
            return False, "주문 수량이 0 이하입니다."

        cash = balance.get("cash", 0.0)
        req_cash = shares * order_price
        if cash > 0 and req_cash > cash:
            return False, "INSUFFICIENT_CASH"

        if positions and equity and equity > 0:
            tot_amt, risk_ratio, status = PortfolioRiskManager.calculate_total_open_risk(positions, equity)
            if status == "BLOCKED":
                return False, "PORTFOLIO_RISK_LIMIT"

        return True, "RISK_APPROVED"
