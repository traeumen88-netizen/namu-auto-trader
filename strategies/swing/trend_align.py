"""스윙 전략 ①: 정배열 추세 (Trend Alignment)
- 장기 이평선 완전 정배열 및 기울기 우상향
- 조건: Close > MA20 > MA60 > MA120 > MA240, MA20 기울기 > 0, MA60 기울기 > 0
"""

from datetime import datetime
from typing import Optional, List, Dict, Any
from core.models import TradeSignal, TimeHorizon, OrderSide, MarketRegime
from core.tick_normalizer import normalize_price


class TrendAlignmentStrategy:
    STRATEGY_ID = "SWG_TREND_ALIGN"

    @classmethod
    def evaluate(
        cls,
        iem_cd: str,
        name: str,
        daily_candles: List[Dict[str, Any]],  # 일봉 데이터 (최소 240개)
        market_regime: MarketRegime,
        current_time: datetime
    ) -> Optional[TradeSignal]:
        if market_regime in (MarketRegime.BEAR, MarketRegime.PANIC):
            return None

        if len(daily_candles) < 240:
            return None

        closes = [c["close"] for c in daily_candles]
        current_price = closes[-1]

        # 1. 이동평균선 산출
        ma20 = sum(closes[-20:]) / 20.0
        ma60 = sum(closes[-60:]) / 60.0
        ma120 = sum(closes[-120:]) / 120.0
        ma240 = sum(closes[-240:]) / 240.0

        # 완전 정배열 조건: Close > MA20 > MA60 > MA120 > MA240
        if not (current_price > ma20 > ma60 > ma120 > ma240):
            return None

        # 2. 기울기(Slope) 우상향 검증: 5일 전 MA와 비교
        prev_ma20 = sum(closes[-25:-5]) / 20.0
        prev_ma60 = sum(closes[-65:-5]) / 60.0

        if ma20 <= prev_ma20 or ma60 <= prev_ma60:
            return None

        # 3. 손절폭 산출 (Entry - 2 * ATR14 or Structure Low)
        # 간이 ATR14 계산
        ranges = [c["high"] - c["low"] for c in daily_candles[-14:]]
        atr14 = sum(ranges) / 14.0 if ranges else current_price * 0.03

        structure_low = min(c["low"] for c in daily_candles[-10:])
        atr_stop = int(current_price - 2.0 * atr14)
        stop_price = max(structure_low, atr_stop)

        stop_distance = current_price - stop_price
        stop_ratio = stop_distance / current_price

        # 스윙 손절폭 필터: > 12% 면 신규 진입 금지
        if stop_ratio > 0.12 or stop_distance <= 0:
            return None

        target_1r = current_price + stop_distance
        target_2r = current_price + int(stop_distance * 2.0)
        target_3r = current_price + int(stop_distance * 3.0)

        normalized_stop = normalize_price(stop_price, "SELL", "STOP")
        normalized_t1 = normalize_price(target_1r, "SELL", "PROFIT")
        normalized_t2 = normalize_price(target_2r, "SELL", "PROFIT")
        normalized_t3 = normalize_price(target_3r, "SELL", "PROFIT")

        return TradeSignal(
            strategy_id=cls.STRATEGY_ID,
            time_horizon=TimeHorizon.SWING,
            iem_cd=iem_cd,
            name=name,
            side=OrderSide.BUY,
            strategy_price=current_price,
            stop_price=normalized_stop,
            target_1r=normalized_t1,
            target_2r=normalized_t2,
            target_3r=normalized_t3,
            score=91.0,
            reason="완전 정배열 추세 (MA20>MA60>MA120>MA240 및 이평선 우상향)",
            timestamp=current_time,
            atr14=atr14,
            expected_rr=3.0
        )
