"""신호 점수 평가 엔진 (Signal Scoring Engine)
- 단타 점수 모델 (100점 만점) 및 스윙 점수 모델 (100점 만점)
- 최종 판정: A+ (>=90), A (80~89), WATCH (70~79), NO TRADE (<70)
"""

from typing import Dict, Any, Tuple
from core.models import MarketRegime


class ScoringEngine:
    @staticmethod
    def score_intraday(
        market_regime: MarketRegime,
        rvol: float,
        price_above_vwap: bool,
        vwap_rising: bool,
        ema_aligned: bool,
        rsi: float,
        breakout_type: str,  # "ORB", "PDH", "PULLBACK", "MOMENTUM", "NONE"
        execution_intensity: float = 105.0,  # 체결강도
        obi: float = 0.0                     # 호가 불균형
    ) -> Tuple[float, str]:
        """
        단타 100점 만점 채점
        :return: (score, grade)
        """
        score = 0.0

        # 1. 시장 국면 점수 (최대 15점)
        if market_regime == MarketRegime.STRONG_BULL:
            score += 15.0
        elif market_regime == MarketRegime.BULL:
            score += 12.0
        elif market_regime == MarketRegime.NEUTRAL:
            score += 5.0

        # 2. RVOL 상대 거래량 (최대 15점)
        if rvol >= 3.0:
            score += 15.0
        elif rvol >= 2.0:
            score += 10.0
        elif rvol >= 1.5:
            score += 5.0

        # 3. VWAP (최대 15점)
        if price_above_vwap:
            score += 10.0
        if vwap_rising:
            score += 5.0

        # 4. EMA 정배열 (최대 10점)
        if ema_aligned:
            score += 10.0

        # 5. RSI (최대 8점)
        if 65.0 <= rsi <= 75.0:
            score += 8.0
        elif 55.0 <= rsi < 65.0:
            score += 5.0
        elif rsi > 75.0:
            score += 3.0

        # 6. 주요 돌파 / 셋업 (각 +10점, 복합 발생 시 가산)
        if isinstance(breakout_type, (list, tuple, set)):
            b_list = list(breakout_type)
        else:
            b_list = [b.strip() for b in str(breakout_type).split(",") if b.strip()]

        if "PDH" in b_list:
            score += 10.0
        if "ORB" in b_list:
            score += 10.0
        if any(b in b_list for b in ("PULLBACK", "MOMENTUM")):
            score += 10.0

        # 7. 체결강도 (최대 5점)
        if execution_intensity >= 110.0:
            score += 5.0

        # 8. 호가 불균형 OBI (최대 5점)
        if obi >= 0.20:
            score += 5.0

        # 등급 판정
        if score >= 90.0:
            grade = "A+"
        elif score >= 80.0:
            grade = "A"
        elif score >= 70.0:
            grade = "WATCH"
        else:
            grade = "NO TRADE"

        return score, grade

    @staticmethod
    def score_swing(
        market_regime: MarketRegime,
        ma20_gt_ma60: bool,
        ma60_gt_ma120: bool,
        ma120_gt_ma240: bool,
        ma20_slope_pos: bool,
        ma60_slope_pos: bool,
        volume_surged: bool,
        breakout_confirmed: bool,
        rsi: float,
        weekly_trend_up: bool
    ) -> Tuple[float, str]:
        """
        스윙 100점 만점 채점
        :return: (score, grade)
        """
        score = 0.0

        # 1. 시장 국면 (최대 15점)
        if market_regime in (MarketRegime.STRONG_BULL, MarketRegime.BULL):
            score += 15.0
        elif market_regime == MarketRegime.NEUTRAL:
            score += 7.0

        # 2. 이평선 배열 (최대 25점)
        if ma20_gt_ma60:
            score += 10.0
        if ma60_gt_ma120:
            score += 10.0
        if ma120_gt_ma240:
            score += 5.0

        # 3. 이평선 기울기 (최대 10점)
        if ma20_slope_pos:
            score += 5.0
        if ma60_slope_pos:
            score += 5.0

        # 4. 거래량 및 돌파 (최대 30점)
        if volume_surged:
            score += 15.0
        if breakout_confirmed:
            score += 15.0

        # 5. RSI 50~70 (최대 10점)
        if 50.0 <= rsi <= 70.0:
            score += 10.0

        # 6. 주봉 상승 추세 (최대 10점)
        if weekly_trend_up:
            score += 10.0

        # 등급 판정
        if score >= 90.0:
            grade = "A+"
        elif score >= 80.0:
            grade = "A"
        elif score >= 70.0:
            grade = "WATCH"
        else:
            grade = "NO TRADE"

        return score, grade
