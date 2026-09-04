"""통합 전략 스위트 (Full Strategy Suite v6.0)
- Execution-Grade Specification v6.0 (Section 11 ~ 25)
- 단타 11대 전략 (Momentum Ignition, Acceleration, ORB, PDH, Day High, 20-Bar High,
                 VWAP Pullback, EMA Pullback, Compression Breakout, HH/HL Structure, News Momentum)
- 스윙 9대 전략 (Trend Align, HH20, HH60, MA20 Pullback, MA60 Pullback, Box Breakout,
                Volume Expansion, Weekly Trend, Catalyst Breakout)
- 추격매수 금지 (Chase Prevention: Section 55) & Fake Breakout 방어 (Section 56)
"""

import logging
from datetime import datetime
from typing import Dict, List, Any, Optional
from core.models import (
    TradeSignal, TimeHorizon, OrderSide, MarketRegime, Candle, SymbolInfo
)
from core.aggregator import CandleAggregator
from core.tick_normalizer import normalize_price

logger = logging.getLogger("FullStrategySuite")


class FullStrategySuite:
    """단타 11대전략 + 스윙 9대전략 통합 평가 엔진"""

    # =========================================================================
    # [1] 단타 전략 평가 (Section 15: 11 Strategies)
    # =========================================================================
    @classmethod
    def evaluate_intraday_all(
        cls,
        sym: SymbolInfo,
        agg: CandleAggregator,
        regime: MarketRegime,
        now: datetime,
        patterns: Dict[str, Any],
        spread_ratio: float = 0.001,
        or_high: Optional[int] = None,
        has_news: bool = False
    ) -> List[TradeSignal]:
        """
        ACTIVE 승격 종목에 대해 11대 단타 전략 평가 및 유효 신호 생성
        """
        signals: List[TradeSignal] = []

        # 기본 진입 거부 조건 (Section 16 & 55)
        if regime == MarketRegime.PANIC:
            return []
        if spread_ratio > 0.0020:  # 호가 스프레드 > 0.20% 배제
            return []
        if patterns.get("is_chase_forbidden", False):  # 추격매수 금지 필터
            return []

        c_1m = agg.candles_1m
        if len(c_1m) < 5:
            return []

        curr_price = sym.price if sym.price > 0 else c_1m[-1].close
        vwap = agg.calculate_vwap("1m")
        ema9 = agg.calculate_ema("1m", 9)
        ema20 = agg.calculate_ema("1m", 20)
        rsi = agg.calculate_rsi("1m", 14)
        atr14 = agg.calculate_atr("1m", 14)

        if curr_price <= 0 or atr14 <= 0:
            return []

        # 손절가 = Entry - 1.5 * ATR (Section 34)
        stop_price = normalize_price(int(curr_price - 1.5 * atr14), OrderSide.BUY)
        risk_per_share = curr_price - stop_price
        if risk_per_share <= 0:
            return []

        # 손절폭 > 3% 인 경우 진입 금지 (Section 34)
        if (risk_per_share / curr_price) > 0.030:
            return []

        target_1r = normalize_price(int(curr_price + 1.0 * risk_per_share), OrderSide.BUY)
        target_2r = normalize_price(int(curr_price + 2.0 * risk_per_share), OrderSide.BUY)
        target_3r = normalize_price(int(curr_price + 3.0 * risk_per_share), OrderSide.BUY)

        def make_sig(strat_id: str, reason: str, score: float = 85.0) -> TradeSignal:
            return TradeSignal(
                strategy_id=strat_id,
                time_horizon=TimeHorizon.INTRADAY,
                iem_cd=sym.iem_cd,
                name=sym.name,
                side=OrderSide.BUY,
                strategy_price=curr_price,
                stop_price=stop_price,
                score=score,
                reason=reason,
                timestamp=now,
                target_1r=target_1r,
                target_2r=target_2r,
                target_3r=target_3r,
                atr14=atr14,
                expected_rr=1.5
            )

        # 1. MOMENTUM IGNITION (Section 11)
        if patterns.get("is_ignition", False) and rsi >= 55.0:
            signals.append(make_sig("INT_MOMENTUM_IGNITION", "모멘텀 점화: 1분/3분 급등 + RVOL>=2.0 + VWAP/EMA 돌파", 92.0))

        # 2. MOMENTUM ACCELERATION (Section 12)
        if patterns.get("is_acceleration", False) and curr_price > vwap:
            signals.append(make_sig("INT_MOMENTUM_ACCEL", "급등 가속: 1분 연속 양봉 + 거래량 50% 급증", 88.0))

        # 3. OPENING RANGE BREAKOUT (ORB) (Section 18)
        if or_high and or_high > 0:
            if curr_price > or_high and curr_price <= or_high * 1.012:  # +1.2% 이내 추격
                if curr_price > vwap and ema9 > ema20 and rsi >= 55.0:
                    signals.append(make_sig("INT_ORB", f"장초반 고가({or_high:,}원) 돌파", 90.0))

        # 4. PREVIOUS DAY HIGH BREAKOUT (PDH) (Section 19)
        if sym.prev_high > 0 and curr_price > sym.prev_high:
            if curr_price > vwap and ema9 > ema20:
                signals.append(make_sig("INT_PDH", f"전일 고가({sym.prev_high:,}원) 돌파", 87.0))

        # 5. INTRADAY HIGH BREAKOUT (당일 신고가 돌파)
        if sym.high_price > 0 and curr_price >= sym.high_price and curr_price > vwap:
            signals.append(make_sig("INT_DAY_HIGH", f"당일 최고가({sym.high_price:,}원) 갱신", 85.0))

        # 6. 20-BAR HIGH BREAKOUT (20봉 신고가 돌파)
        if len(c_1m) >= 20:
            past_20_high = max(c.high for c in c_1m[-21:-1])
            if curr_price > past_20_high and curr_price > vwap:
                signals.append(make_sig("INT_20BAR_HIGH", f"20봉 고가({past_20_high:,}원) 돌파", 84.0))

        # 7. VWAP PULLBACK (Section 20)
        if ema9 > ema20 and curr_price > vwap and rsi >= 50.0:
            dist_vwap = abs(curr_price - vwap) / vwap
            if dist_vwap <= 0.0030:  # VWAP 0.3% 이내 지지 눌림목
                signals.append(make_sig("INT_VWAP_PULLBACK", "VWAP 지지 눌림목 반등", 86.0))

        # 8. EMA PULLBACK
        if ema9 > ema20 and curr_price > ema20:
            dist_ema9 = abs(curr_price - ema9) / ema9
            if dist_ema9 <= 0.0030 and rsi >= 48.0:
                signals.append(make_sig("INT_EMA_PULLBACK", "EMA9 지지 눌림목 반등", 83.0))

        # 9. COMPRESSION BREAKOUT (Section 14 & 21)
        if patterns.get("is_compression_breakout", False):
            signals.append(make_sig("INT_COMPRESSION_BREAKOUT", "가격 압축 구간 상향 돌파", 93.0))

        # 10. HIGHER-HIGH / HIGHER-LOW (Section 13)
        if patterns.get("is_strong_chart", False):
            signals.append(make_sig("INT_HH_HL_UPTREND", "상승 파동 구조 (Higher High + Higher Low)", 88.0))

        # 11. NEWS MOMENTUM
        if has_news and curr_price > vwap and ema9 > ema20:
            signals.append(make_sig("INT_NEWS_MOMENTUM", "실시간 뉴스/공시 수급 모멘텀", 89.0))

        return signals

    # =========================================================================
    # [2] 스윙 전략 평가 (Section 22: 9 Strategies)
    # =========================================================================
    @classmethod
    def evaluate_swing_all(
        cls,
        sym: SymbolInfo,
        daily_candles: List[Dict[str, Any]],
        regime: MarketRegime,
        now: datetime,
        has_catalyst: bool = False
    ) -> List[TradeSignal]:
        """
        스윙 9대 전략 평가 (Section 22 ~ 25)
        """
        if len(daily_candles) < 60:
            return []
        if regime in (MarketRegime.BEAR, MarketRegime.PANIC):
            return []

        signals: List[TradeSignal] = []
        closes = [c["close"] for c in daily_candles]
        curr_price = closes[0]

        # 이동평균선 계산
        ma20 = sum(closes[:20]) / 20.0
        ma60 = sum(closes[:60]) / 60.0

        # ATR 계산
        trs = []
        for i in range(14):
            c_curr = daily_candles[i]
            c_prev = daily_candles[i + 1] if i + 1 < len(daily_candles) else c_curr
            tr = max(
                c_curr["high"] - c_curr["low"],
                abs(c_curr["high"] - c_prev["close"]),
                abs(c_curr["low"] - c_prev["close"])
            )
            trs.append(tr)
        atr_daily = sum(trs) / 14.0 if trs else curr_price * 0.02

        # 손절가 = Entry - 2.0 * ATR (Section 36)
        stop_price = normalize_price(int(curr_price - 2.0 * atr_daily), OrderSide.BUY)
        risk_per_share = curr_price - stop_price
        stop_ratio = risk_per_share / curr_price if curr_price > 0 else 0

        # 손절폭 > 12% 진입 금지 (Section 36)
        if stop_ratio > 0.120 or risk_per_share <= 0:
            return []

        target_1r = normalize_price(int(curr_price + 1.0 * risk_per_share), OrderSide.BUY)
        target_2r = normalize_price(int(curr_price + 2.0 * risk_per_share), OrderSide.BUY)
        target_3r = normalize_price(int(curr_price + 3.0 * risk_per_share), OrderSide.BUY)

        def make_swing_sig(strat_id: str, reason: str, score: float = 85.0) -> TradeSignal:
            return TradeSignal(
                strategy_id=strat_id,
                time_horizon=TimeHorizon.SWING,
                iem_cd=sym.iem_cd,
                name=sym.name,
                side=OrderSide.BUY,
                strategy_price=curr_price,
                stop_price=stop_price,
                score=score,
                reason=reason,
                timestamp=now,
                target_1r=target_1r,
                target_2r=target_2r,
                target_3r=target_3r,
                atr14=atr_daily,
                expected_rr=2.0
            )

        # 1. 정배열 (Section 23: STRONG SWING TREND)
        if len(closes) >= 120:
            ma120 = sum(closes[:120]) / 120.0
            if curr_price > ma20 > ma60 > ma120:
                signals.append(make_swing_sig("SWG_TREND_ALIGN", "중기 이평선 정배열 추세", 90.0))

        # 2. 20일 신고가 돌파
        hh20 = max(c["high"] for c in daily_candles[1:21])
        if curr_price > hh20:
            signals.append(make_swing_sig("SWG_HH20_BREAKOUT", f"20일 신고가({hh20:,}원) 돌파", 85.0))

        # 3. 60일 신고가 돌파 (Section 24)
        hh60 = max(c["high"] for c in daily_candles[1:61])
        if curr_price > hh60:
            # 당일 상승률 10% 미만만 진입
            today_ret = (curr_price - daily_candles[0]["open"]) / daily_candles[0]["open"] if daily_candles[0]["open"] > 0 else 0
            if today_ret < 0.10:
                signals.append(make_swing_sig("SWG_HH60_BREAKOUT", f"60일 신고가({hh60:,}원) 돌파", 92.0))

        # 4. MA20 눌림목 (Section 25)
        dist_ma20 = abs(curr_price - ma20) / ma20
        if curr_price > ma20 and dist_ma20 <= 0.030 and ma20 > ma60:
            signals.append(make_swing_sig("SWG_MA20_PULLBACK", "20일선 눌림목 반등", 88.0))

        # 5. MA60 눌림목
        dist_ma60 = abs(curr_price - ma60) / ma60
        if curr_price > ma60 and dist_ma60 <= 0.030:
            signals.append(make_swing_sig("SWG_MA60_PULLBACK", "60일선 중기 지지선 반등", 84.0))

        # 6. 박스권 돌파
        if len(daily_candles) >= 20:
            box_high = max(c["high"] for c in daily_candles[1:20])
            box_low = min(c["low"] for c in daily_candles[1:20])
            box_rng = (box_high - box_low) / box_low if box_low > 0 else 1.0
            if box_rng <= 0.12 and curr_price > box_high:
                signals.append(make_swing_sig("SWG_BOX_BREAKOUT", "20일 박스권(변동폭<=12%) 상단 돌파", 89.0))

        # 7. 거래량 수축 후 확장
        vols = [c["volume"] for c in daily_candles[:5]]
        if len(daily_candles) >= 10:
            past_vol_avg = sum(c["volume"] for c in daily_candles[5:15]) / 10.0
            if past_vol_avg > 0 and vols[0] >= past_vol_avg * 2.0 and min(vols[1:5]) <= past_vol_avg * 0.6:
                signals.append(make_swing_sig("SWG_VOL_EXPANSION", "거래량 수축 후 폭발적 거래량 확장 돌파", 91.0))

        # 8. 주봉 추세
        if len(daily_candles) >= 50 and curr_price > ma20:
            signals.append(make_swing_sig("SWG_WEEKLY_TREND", "주봉 상승 추세 지속", 82.0))

        # 9. 실적/뉴스 + 기술적 돌파 (Catalyst Breakout)
        if has_catalyst and curr_price > ma20:
            signals.append(make_swing_sig("SWG_CATALYST_BREAKOUT", "실적/모멘텀 모멘텀 + 기술적 돌파", 94.0))

        return signals
