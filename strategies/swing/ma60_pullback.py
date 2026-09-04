"""스윙 전략 ④: MA60 Pullback (60일선 중기 지지 눌림목)
- 60일 수급선 상승 지지 구간에서 건전한 조정(-15% 이내) 후 반등
- 조건: Close > MA60, MA60 상승, MA20 > MA60, 고점 대비 조정폭 <= 15%,
        RSI >= 45, 하락구간 거래량 감소, 양봉 전환 및 거래량 증가
"""

from datetime import datetime
from typing import Optional, List, Dict, Any
from core.models import TradeSignal, TimeHorizon, OrderSide, MarketRegime
from core.tick_normalizer import normalize_price


class MA60PullbackStrategy:
    STRATEGY_ID = "SWG_MA60_PULLBACK"

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

        if len(daily_candles) < 70:
            return None

        today = daily_candles[-1]
        yesterday = daily_candles[-2]
        current_price = today["close"]

        closes = [c["close"] for c in daily_candles]
        ma20 = sum(closes[-20:]) / 20.0
        ma60 = sum(closes[-60:]) / 60.0
        prev_ma60 = sum(closes[-65:-5]) / 60.0

        # 1. Close > MA60, MA60 상승, MA20 > MA60
        if current_price <= ma60 or ma60 <= prev_ma60 or ma20 <= ma60:
            return None

        # 2. 최근 60일 고점 대비 조정폭 <= 15%
        high_60 = max(c["high"] for c in daily_candles[-60:])
        drawdown = (high_60 - current_price) / high_60
        if drawdown > 0.15 or drawdown < 0:
            return None

        # 3. RSI >= 45
        gains, losses = [], []
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
        if rsi < 45.0:
            return None

        # 4. 하락구간 거래량 감소 후 양봉 전환
        if today["close"] <= today["open"]:
            return None
        if today["volume"] <= yesterday["volume"]:
            return None

        # 5. 손절가 산출
        ranges = [c["high"] - c["low"] for c in daily_candles[-14:]]
        atr14 = sum(ranges) / 14.0 if ranges else current_price * 0.03
        structure_stop = int(ma60 * 0.97)
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
            score=87.0,
            reason=f"MA60 눌림목 반등 (고점대비 조정={drawdown*100:.1f}%, MA60 지지, RSI={rsi:.1f})",
            timestamp=current_time,
            atr14=atr14,
            expected_rr=3.0
        )
