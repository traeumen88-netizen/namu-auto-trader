"""스윙 전략 ③: MA20 Pullback (20일선 눌림목 반등)
- 20일선 우상향 지지 구간에서 거래량 감소 후 양봉 반등
- 조건: Close > MA20, MA20 > MA60, 20일 내 신고가 존재, ABS(Close-MA20)/MA20 <= 0.03,
        최근 5일 평균 거래량 < 이전 5일 평균 거래량, 양봉 및 거래량 전일대비 1.2배 증가
"""

from datetime import datetime
from typing import Optional, List, Dict, Any
from core.models import TradeSignal, TimeHorizon, OrderSide, MarketRegime
from core.tick_normalizer import normalize_price


class MA20PullbackStrategy:
    STRATEGY_ID = "SWG_MA20_PULLBACK"

    @classmethod
    def evaluate(
        cls,
        iem_cd: str,
        name: str,
        daily_candles: List[Dict[str, Any]],
        market_regime: MarketRegime,
        current_time: datetime
    ) -> Optional[TradeSignal]:
        if market_regime in (MarketRegime.BEAR, MarketRegime.PANIC):
            return None

        if len(daily_candles) < 65:
            return None

        today = daily_candles[-1]
        yesterday = daily_candles[-2]
        current_price = today["close"]

        closes = [c["close"] for c in daily_candles]
        ma20 = sum(closes[-20:]) / 20.0
        ma60 = sum(closes[-60:]) / 60.0

        # 1. Close > MA20, MA20 > MA60
        if current_price <= ma20 or ma20 <= ma60:
            return None

        # 2. 20일 이내 신고가(최고가) 형성 이력 확인
        highs_20 = [c["high"] for c in daily_candles[-20:]]
        highs_60 = [c["high"] for c in daily_candles[-60:]]
        if max(highs_20) < max(highs_60):
            return None  # 20일 내 신고가가 아님

        # 3. 20일선 이격도 3% 이내: ABS(Close - MA20) / MA20 <= 0.03
        ma_dist = abs(current_price - ma20) / ma20
        if ma_dist > 0.03:
            return None

        # 4. 조정구간 거래량 수축: 최근 5일 평균 거래량 < 이전 5일 평균 거래량
        recent_5_vol = sum(c["volume"] for c in daily_candles[-5:]) / 5.0
        prev_5_vol = sum(c["volume"] for c in daily_candles[-10:-5]) / 5.0
        if recent_5_vol >= prev_5_vol:
            return None

        # 5. 양봉 전환 및 거래량 증가: Close > Open AND Volume > Previous Volume * 1.2
        if today["close"] <= today["open"]:
            return None
        if today["volume"] <= yesterday["volume"] * 1.2:
            return None

        # 6. 손절가 산출 (20일선 하단 지지선 또는 2 * ATR14)
        ranges = [c["high"] - c["low"] for c in daily_candles[-14:]]
        atr14 = sum(ranges) / 14.0 if ranges else current_price * 0.03
        structure_stop = int(ma20 * 0.98)
        atr_stop = int(current_price - 2.0 * atr14)
        stop_price = max(structure_stop, atr_stop)

        stop_distance = current_price - stop_price
        stop_ratio = stop_distance / current_price
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
            score=89.0,
            reason=f"MA20 눌림목 반등 (20일선 이격={ma_dist*100:.2f}%, 양봉전환, 거래량배수={today['volume']/yesterday['volume']:.2f}x)",
            timestamp=current_time,
            atr14=atr14,
            expected_rr=3.0
        )
