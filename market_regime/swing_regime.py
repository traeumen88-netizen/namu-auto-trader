"""스윙용 시장국면 판정 엔진 (SWING MARKET REGIME)
KOSPI / KOSDAQ 일봉 이동평균선(MA20, MA60, MA120, MA240) 기반 판정
- STRONG_BULL / BULL / NEUTRAL / BEAR / PANIC
"""

from typing import List, Dict, Any
from core.models import MarketRegime


class SwingRegimeEngine:
    @staticmethod
    def evaluate(
        close_prices: List[float],
        one_day_return: float
    ) -> Dict[str, Any]:
        """
        일봉 종가 배열을 바탕으로 스윙 시장국면 판정
        :param close_prices: 최신순 또는 과거순 종가 리스트 (최소 60일, 가급적 240일 이상)
        :param one_day_return: 당일 지수 등락률 (소수점, 예: -0.02 = -2%)
        :return: {"regime": MarketRegime, "ma20": float, "ma60": float, "reason": str}
        """
        if len(close_prices) < 60:
            return {
                "regime": MarketRegime.NEUTRAL,
                "ma20": 0.0,
                "ma60": 0.0,
                "reason": "데이터 부족 (기본값 NEUTRAL)"
            }

        # 최신 종가가 리스트 마지막이라고 가정
        current = close_prices[-1]
        ma20 = sum(close_prices[-20:]) / 20.0
        ma60 = sum(close_prices[-60:]) / 60.0

        ma120 = sum(close_prices[-120:]) / 120.0 if len(close_prices) >= 120 else ma60
        ma240 = sum(close_prices[-240:]) / 240.0 if len(close_prices) >= 240 else ma120

        # PANIC: Index < MA60 AND 1일 수익률 <= -2%
        if current < ma60 and one_day_return <= -0.02:
            return {
                "regime": MarketRegime.PANIC,
                "ma20": ma20, "ma60": ma60, "ma120": ma120, "ma240": ma240,
                "reason": f"지수 < MA60 및 당일 급락 ({one_day_return*100:.2f}%)"
            }

        # BEAR: Index < MA60
        if current < ma60:
            return {
                "regime": MarketRegime.BEAR,
                "ma20": ma20, "ma60": ma60, "ma120": ma120, "ma240": ma240,
                "reason": f"지수({current:,.1f}) < MA60({ma60:,.1f})"
            }

        # STRONG_BULL: Index > MA20 AND MA20 > MA60 AND Close > MA60
        if current > ma20 and ma20 > ma60 and current > ma60:
            # 추가 확인: 120, 240 정배열 여부
            return {
                "regime": MarketRegime.STRONG_BULL,
                "ma20": ma20, "ma60": ma60, "ma120": ma120, "ma240": ma240,
                "reason": f"지수 > MA20 > MA60 완전 정배열"
            }

        # BULL: Index > MA20 AND MA20 > MA60
        if current > ma20 and ma20 > ma60:
            return {
                "regime": MarketRegime.BULL,
                "ma20": ma20, "ma60": ma60, "ma120": ma120, "ma240": ma240,
                "reason": f"지수 > MA20 및 MA20 > MA60"
            }

        # NEUTRAL: Index > MA60 BUT MA20 <= MA60
        return {
            "regime": MarketRegime.NEUTRAL,
            "ma20": ma20, "ma60": ma60, "ma120": ma120, "ma240": ma240,
            "reason": f"지수 > MA60이나 MA20 <= MA60 (중립 횡보/조정)"
        }
