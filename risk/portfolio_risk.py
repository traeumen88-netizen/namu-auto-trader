"""포트폴리오 리스크 및 테마 상관관계 통제 엔진
- 동시 보유 포지션 총 위험(Total Open Risk) 계산:
  <= 3% : 정상 신규 진입
  3~4%  : 신규 진입 규모 50% 감축
  > 4%  : 신규 진입 전면 금지
- 동일 테마/업종 집중도 제한: 최대 3종목, 테마 총 위험 <= 1.5%
- 시장국면별 자산 배분 (단타 / 스윙 / 현금) 관리
"""

from typing import List, Dict, Any, Tuple
from core.models import Position, MarketRegime
from config.settings import (
    TOTAL_RISK_NORMAL_LIMIT,
    TOTAL_RISK_REDUCED_LIMIT,
    MAX_STOCKS_PER_THEME,
    MAX_RISK_PER_THEME,
    REGIME_ALLOCATION
)


class PortfolioRiskManager:
    @staticmethod
    def calculate_total_open_risk(positions: List[Position], equity: float) -> Tuple[float, float, str]:
        """
        열려 있는 모든 포지션의 최대 손실 합계 계산
        :return: (total_risk_amount, total_risk_ratio, status: "NORMAL" | "REDUCED" | "BLOCKED")
        """
        if equity <= 0:
            return 0.0, 0.0, "BLOCKED"

        active_positions = [p for p in positions if not p.is_closed]
        total_risk_amount = sum(p.initial_risk_amount for p in active_positions)
        risk_ratio = total_risk_amount / equity

        if risk_ratio <= TOTAL_RISK_NORMAL_LIMIT:
            status = "NORMAL"
        elif risk_ratio <= TOTAL_RISK_REDUCED_LIMIT:
            status = "REDUCED"
        else:
            status = "BLOCKED"

        return total_risk_amount, risk_ratio, status

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
        """
        if not candidate_theme or candidate_theme == "기타":
            return True, "테마 무관 통과"

        active_positions = [p for p in positions if not p.is_closed]
        theme_positions = [
            p for p in active_positions
            if stock_theme_map.get(p.iem_cd, "") == candidate_theme
        ]

        # 1. 종목 수 제한 (최대 3종목)
        if len(theme_positions) >= MAX_STOCKS_PER_THEME:
            return False, f"테마 '{candidate_theme}' 보유 종목수 한도({MAX_STOCKS_PER_THEME}개) 초과"

        # 2. 테마 총 위험 비율 제한 (<= 1.5%)
        curr_theme_risk = sum(p.initial_risk_amount for p in theme_positions)
        new_theme_risk_ratio = (curr_theme_risk + candidate_risk) / equity

        if new_theme_risk_ratio > MAX_RISK_PER_THEME:
            return False, f"테마 '{candidate_theme}' 총 위험 한도(1.5%) 초과: {new_theme_risk_ratio*100:.2f}%"

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
