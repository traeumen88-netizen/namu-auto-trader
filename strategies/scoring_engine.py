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
        breakout_type: str,  # "ORB", "PDH", "PULLBACK", "MOMENTUM", "COMPRESSION", "NONE"
        execution_intensity: float = 105.0,  # 체결강도
        obi: float = 0.0,                    # 호가 불균형
        body_ratio: float = 0.50,            # 양봉 몸통 비율
        is_day_high: bool = False,           # 당일 고가 돌파
        is_pdh: bool = False,                # 전일 고가 돌파
        relative_strength: float = 0.0,      # 상대강도 RS vs 시장
        turnover_ratio: float = 1.0,         # 거래대금 급증 배수
        is_breakout_20: bool = False         # 20봉 신고가 돌파
    ) -> Tuple[float, str]:
        """
        단타 100점 만점 채점 (Section 10 규격: 60점 이상 B+ BUY 후보, 80점 이상 A, 90점 이상 A+)
        :return: (score, grade)
        """
        # 핵심 셋업 기본 통과 점수 (Hard Gate + Setup 통과 종목)
        score = 25.0

        # 1. 시장 국면 점수 (최대 10점)
        if market_regime == MarketRegime.STRONG_BULL:
            score += 10.0
        elif market_regime == MarketRegime.BULL:
            score += 8.0
        elif market_regime == MarketRegime.NEUTRAL:
            score += 5.0

        # 2. RVOL 상대 거래량 (최대 15점)
        if rvol >= 3.0:
            score += 15.0
        elif rvol >= 2.0:
            score += 10.0
        elif rvol >= 1.5:
            score += 5.0

        # 3. 거래대금 급증 (Turnover Acceleration, 최대 15점)
        if turnover_ratio >= 3.0:
            score += 15.0
        elif turnover_ratio >= 2.0:
            score += 10.0
        elif turnover_ratio > 1.2:
            score += 5.0

        # 4. 상대강도 RS vs 시장 (최대 10점)
        if relative_strength >= 0.02:
            score += 10.0
        elif relative_strength >= 0.01:
            score += 6.0
        elif relative_strength > 0.0:
            score += 3.0

        # 5. VWAP 지지/돌파 (최대 10점)
        if price_above_vwap:
            score += 10.0
        elif vwap_rising:
            score += 5.0

        # 6. EMA 정배열 (최대 10점)
        if ema_aligned:
            score += 10.0

        # 7. RSI (최대 5점)
        if rsi >= 50.0:
            score += 5.0

        # 8. 체결강도 및 호가 불균형 (최대 10점)
        if execution_intensity >= 110.0:
            score += 5.0
        if obi >= 0.20:
            score += 5.0

        # 9. 20봉 고가 / 전일 고가 / 당일 고가 / 몸통 비율 가산점 (각 +10점)
        if is_pdh:
            score += 10.0
        if is_day_high:
            score += 10.0
        if is_breakout_20 and not is_pdh and not is_day_high:
            score += 10.0
        if body_ratio >= 0.55:
            score += 10.0

        # 10. 셋업별 추가 가산점
        if isinstance(breakout_type, (list, tuple, set)):
            b_list = list(breakout_type)
        else:
            b_list = [b.strip() for b in str(breakout_type).split(",") if b.strip()]

        if "PDH" in b_list and not is_pdh:
            score += 10.0
        if "ORB" in b_list:
            score += 10.0
        if any(b in b_list for b in ("PULLBACK", "MOMENTUM", "COMPRESSION")):
            score += 10.0

        score = min(score, 100.0)

        # 11. 정체/저유동성 종목 페널티 및 상한 제약 (Section 3: 정체주 고득점 원천 차단)
        # RVOL 및 turnover_ratio가 저조하여 수급이 없는 종목은 점수를 45점 이하(NO TRADE)로 강제 제한
        is_severe_stagnant = (rvol < 0.50 and turnover_ratio <= 0.80)
        is_stagnant = (rvol < 0.80 and turnover_ratio <= 1.0)
        if is_severe_stagnant:
            score = min(score * 0.40, 40.0)
        elif is_stagnant:
            score = min(score * 0.60, 45.0)

        # 등급 판정: 90점 이상 A+, 80점 이상 A, 60점 이상 B+ (BUY Candidate), 50점 이상 WATCH, 50점 미만 NO TRADE
        if score >= 90.0:
            grade = "A+"
        elif score >= 80.0:
            grade = "A"
        elif score >= 60.0:
            grade = "B+"
        elif score >= 50.0:
            grade = "WATCH"
        else:
            grade = "NO TRADE"

        return round(score, 1), grade

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

        # 7. 정체 종목 페널티: 거래량 증가도 없고 돌파도 없는 경우 점수 상한 제한
        if not volume_surged and not breakout_confirmed:
            score = min(score, 55.0)

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
