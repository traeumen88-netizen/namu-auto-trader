"""단타 전략 ⑤: Momentum Burst (모멘텀 폭발)
- 거래량 급증(20봉 평균 대비 3배 이상) 및 20봉 최고가 동시 돌파
- 조건: 1분 거래량 >= 20봉 평균 * 3.0, 20봉 최고가 갱신, Price > VWAP,
        EMA9 > EMA20, RSI >= 55, 과열 필터(Price / VWAP < 1.03)
"""

from datetime import datetime
from typing import Optional
from core.models import TradeSignal, TimeHorizon, OrderSide, MarketRegime
from core.tick_normalizer import normalize_price


class MomentumBurstStrategy:
    STRATEGY_ID = "INT_MOMENTUM_BURST"

    @classmethod
    def evaluate(
        cls,
        iem_cd: str,
        name: str,
        current_price: int,
        aggregator,
        market_regime: MarketRegime,
        current_time: datetime,
        spread_ratio: float
    ) -> Optional[TradeSignal]:
        if market_regime == MarketRegime.PANIC:
            return None

        if spread_ratio > 0.0020:
            return None

        candles_1m = aggregator.candles_1m
        if len(candles_1m) < 20:
            return None

        vwap = aggregator.vwap
        if vwap <= 0:
            return None

        # 1. 과열 필터: Price / VWAP >= 1.03 이면 신규 추격 매수 금지
        if (current_price / vwap) >= 1.03:
            return None

        # 2. 거래량 폭발: 최근 20봉 평균 거래량 대비 현재 1분 거래량 >= 3.0배
        recent_20 = candles_1m[-20:]
        avg_vol = sum(c.volume for c in recent_20) / 20.0
        cur_vol = aggregator.current_1m.volume if aggregator.current_1m else candles_1m[-1].volume

        if avg_vol <= 0 or (cur_vol / avg_vol) < 3.0:
            return None

        # 3. 20봉 최고가 갱신
        highest_20 = max(c.high for c in recent_20)
        if current_price <= highest_20:
            return None

        # 4. Price > VWAP, EMA9 > EMA20, RSI >= 55
        if current_price <= vwap:
            return None

        ema9 = aggregator.calculate_ema("1m", 9)
        ema20 = aggregator.calculate_ema("1m", 20)
        if ema9 <= ema20:
            return None

        rsi = aggregator.calculate_rsi("1m", 14)
        if rsi < 55.0:
            return None

        # 5. 손절가 (20봉 전고점 또는 1.5 * ATR14, 최대 3% 제한)
        atr = aggregator.calculate_atr("1m", 14)
        structure_stop = highest_20
        atr_stop = int(current_price - 1.5 * atr) if atr > 0 else int(current_price * 0.98)
        stop_price = max(structure_stop, atr_stop)

        stop_distance = current_price - stop_price
        if stop_distance > current_price * 0.03 or stop_distance <= 0:
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
            time_horizon=TimeHorizon.INTRADAY,
            iem_cd=iem_cd,
            name=name,
            side=OrderSide.BUY,
            strategy_price=current_price,
            stop_price=normalized_stop,
            target_1r=normalized_t1,
            target_2r=normalized_t2,
            target_3r=normalized_t3,
            score=94.0,
            reason=f"모멘텀 폭발 (거래량 배수={cur_vol/avg_vol:.1f}x, 20봉 전고점 돌파, RSI={rsi:.1f})",
            timestamp=current_time,
            atr14=atr,
            expected_rr=2.0
        )
