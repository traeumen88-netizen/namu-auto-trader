"""단타 전략 ④: EMA Pullback (이동평균선 눌림목)
- 상승 추세 중 단기 10봉 최고가 대비 건전한 조정(-1.5% 이내) 후 직전봉 고점 돌파 재진입
- 조건: EMA9 > EMA20, Price > VWAP, 최근 10봉 최고가 대비 조정폭 <= 1.5%,
        조정구간 평균 거래량 <= 상승구간 평균 거래량 * 0.70, Close > Previous High
"""

from datetime import datetime
from typing import Optional
from core.models import TradeSignal, TimeHorizon, OrderSide, MarketRegime
from core.tick_normalizer import normalize_price


class EMAPullbackStrategy:
    STRATEGY_ID = "INT_EMA_PULLBACK"

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
        if market_regime in (MarketRegime.BEAR, MarketRegime.PANIC):
            return None

        if spread_ratio > 0.0020:
            return None

        candles_1m = aggregator.candles_1m
        if len(candles_1m) < 15:
            return None

        # 1. EMA9 > EMA20, Price > VWAP
        ema9 = aggregator.calculate_ema("1m", 9)
        ema20 = aggregator.calculate_ema("1m", 20)
        if ema9 <= ema20:
            return None
        if current_price <= aggregator.vwap:
            return None

        # 2. 최근 10봉 최고가 대비 조정폭 <= 1.5%
        recent_10 = candles_1m[-10:]
        highest_10 = max(c.high for c in recent_10)
        drawdown = (highest_10 - current_price) / highest_10
        if drawdown > 0.015 or drawdown < 0:
            return None

        # 3. 조정구간 평균 거래량 <= 상승구간 평균 거래량 * 0.70
        pullback_vols = [c.volume for c in recent_10[-3:]]
        impulse_vols = [c.volume for c in recent_10[:7]]
        avg_pb = sum(pullback_vols) / len(pullback_vols)
        avg_imp = sum(impulse_vols) / len(impulse_vols) if impulse_vols else avg_pb
        if avg_pb > avg_imp * 0.70:
            return None

        # 4. Close > Previous High (재돌파 시점)
        last_bar = candles_1m[-1]
        prev_bar = candles_1m[-2]
        if last_bar.close <= prev_bar.high:
            return None

        # 5. 손절가 (EMA20 하단 또는 1.5 * ATR14, 최대 3% 제한)
        atr = aggregator.calculate_atr("1m", 14)
        structure_stop = int(ema20 * 0.997)
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
            score=86.0,
            reason=f"EMA 눌림목 반등 (10봉 고점 대비 조정={drawdown*100:.2f}%, 거래량 수축={avg_pb/avg_imp:.2f}x)",
            timestamp=current_time,
            atr14=atr,
            expected_rr=2.0
        )
