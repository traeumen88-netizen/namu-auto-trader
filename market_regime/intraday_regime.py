"""단타용 장중 실시간 시장국면 판정 엔진 (INTRADAY MARKET REGIME)
상승/하락 종목수 비율(AD Ratio), 5분 지수 수익률을 이용한 실시간 국면 분석
- STRONG_BULL / BULL / NEUTRAL / BEAR / PANIC
"""

from typing import Dict, Any
from core.models import MarketRegime


class IntradayRegimeEngine:
    @staticmethod
    def evaluate(
        advancing_count: int,
        declining_count: int,
        kospi_5m_return: float,
        kosdaq_5m_return: float,
        index_above_vwap: bool = True
    ) -> Dict[str, Any]:
        """
        실시간 시장 폭(Market Breadth) 및 단기 모멘텀 기반 단타 국면 판정
        :param advancing_count: 상승 종목 수
        :param declining_count: 하락 종목 수
        :param kospi_5m_return: 코스피 최근 5분 수익률 (소수점, 예: 0.001 = +0.1%)
        :param kosdaq_5m_return: 코스닥 최근 5분 수익률
        :param index_above_vwap: 지수 선물/ETF의 VWAP 상회 여부
        :return: {"regime": MarketRegime, "ad_ratio": float, "is_panic": bool, "reason": str}
        """
        total = advancing_count + declining_count
        ad_ratio = (advancing_count / total) if total > 0 else 0.50

        # 1. PANIC 조건: AD < 0.35 OR KOSPI 5분 <= -1.0% OR KOSDAQ 5분 <= -1.2%
        if ad_ratio < 0.35 or kospi_5m_return <= -0.010 or kosdaq_5m_return <= -0.012:
            reasons = []
            if ad_ratio < 0.35:
                reasons.append(f"AD 비율 극단적 침체({ad_ratio*100:.1f}%)")
            if kospi_5m_return <= -0.010:
                reasons.append(f"코스피 5분 급락({kospi_5m_return*100:.2f}%)")
            if kosdaq_5m_return <= -0.012:
                reasons.append(f"코스닥 5분 급락({kosdaq_5m_return*100:.2f}%)")

            return {
                "regime": MarketRegime.PANIC,
                "ad_ratio": ad_ratio,
                "is_panic": True,
                "can_trade_intraday": False,
                "reason": " / ".join(reasons)
            }

        # 2. STRONG_BULL 조건: AD >= 0.65 AND 코스피 5분 > 0 AND 코스닥 5분 > 0
        if ad_ratio >= 0.65 and kospi_5m_return > 0 and kosdaq_5m_return > 0 and index_above_vwap:
            return {
                "regime": MarketRegime.STRONG_BULL,
                "ad_ratio": ad_ratio,
                "is_panic": False,
                "can_trade_intraday": True,
                "reason": f"강한 상승장 (AD {ad_ratio*100:.1f}%, 양대지수 5분 반등)"
            }

        # 3. BULL 조건: AD >= 0.55
        if ad_ratio >= 0.55:
            return {
                "regime": MarketRegime.BULL,
                "ad_ratio": ad_ratio,
                "is_panic": False,
                "can_trade_intraday": True,
                "reason": f"상승 우위 (AD {ad_ratio*100:.1f}%)"
            }

        # 4. NEUTRAL 조건: 0.45 <= AD < 0.55
        if 0.45 <= ad_ratio < 0.55:
            return {
                "regime": MarketRegime.NEUTRAL,
                "ad_ratio": ad_ratio,
                "is_panic": False,
                "can_trade_intraday": True,
                "reason": f"중립 횡보장 (AD {ad_ratio*100:.1f}%)"
            }

        # 5. BEAR 조건: AD < 0.45
        return {
            "regime": MarketRegime.BEAR,
            "ad_ratio": ad_ratio,
            "is_panic": False,
            "can_trade_intraday": False,  # BEAR 국면에서는 신규 단타 제한
            "reason": f"하락 우위 (AD {ad_ratio*100:.1f}%)"
        }
