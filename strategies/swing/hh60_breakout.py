"""스윙 전략 ②: 60일 신고가 돌파 (HH60 Breakout)
- 60거래일 신고가 돌파 및 거래량 1.5배 급증 포착
- 조건: Close > HH60, Volume >= MA60 Volume * 1.5, RSI >= 55, Close > MA20,
        Market != BEAR/PANIC, 당일 상승률 < +10% (추격 금지)
"""

from datetime import datetime
from typing import Optional, List, Dict, Any
from core.models import TradeSignal, TimeHorizon, OrderSide, MarketRegime
from core.tick_normalizer import normalize_price


class HH60BreakoutStrategy:
    STRATEGY_ID = "SWG_HH60_BREAKOUT"

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

        # 1. 당일 상승률 필터: >= +10% 이면 추격 금지
        today_return = (current_price - yesterday["close"]) / yesterday["close"]
        if today_return >= 0.10:
            return None

        # 2. 60거래일 최고가 (당일 제외 이전 60일 고가)
        past_60_highs = [c["high"] for c in daily_candles[-61:-1]]
        hh60 = max(past_60_highs)

        if current_price <= hh60:
            return None

        # 3. 거래량 >= 60일 평균 거래량 * 1.5
        past_60_vols = [c["volume"] for c in daily_candles[-61:-1]]
        ma60_vol = sum(past_60_vols) / len(past_60_vols)
        if today["volume"] < ma60_vol * 1.5:
            return None

        # 4. Close > MA20
        ma20 = sum(c["close"] for c in daily_candles[-20:]) / 20.0
        if current_price <= ma20:
            return None

        # 5. 간이 일봉 RSI >= 55
        gains = []
        losses = []
        for i in range(len(daily_candles) - 14, len(daily_candles)):
            diff = daily_candles[i]["close"] - daily_candles[i-1]["close"]
            if diff >= 0:
                gains.append(diff)
                losses.append(0.0)
            else:
                gains.append(0.0)
                losses.append(abs(diff))
        avg_g = sum(gains) / 14.0
        avg_l = sum(losses) / 14.0
        rsi = 100.0 if avg_l == 0 else 100.0 - (100.0 / (1.0 + (avg_g / avg_l)))

        if rsi < 55.0:
            return None

        # 6. 손절폭 산출
        ranges = [c["high"] - c["low"] for c in daily_candles[-14:]]
        atr14 = sum(ranges) / 14.0 if ranges else current_price * 0.03
        atr_stop = int(current_price - 2.0 * atr14)
        stop_price = max(hh60, atr_stop)

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
            score=93.0,
            reason=f"60일 신고가 돌파 (HH60={hh60:,}, 거래량배수={today['volume']/ma60_vol:.1f}x, RSI={rsi:.1f})",
            timestamp=current_time,
            atr14=atr14,
            expected_rr=3.0
        )
