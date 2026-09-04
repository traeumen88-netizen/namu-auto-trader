"""단타 전략 ②: 전일고가 돌파 (PDH Breakout)
- PDH (Previous Day High) 돌파 시점 포착
- 조건: Current Price > PDH, 1분봉 종가 > PDH, RVOL >= 1.5, Price > VWAP,
        EMA9 > EMA20, RSI >= 55, Body Ratio >= 0.55, Market != PANIC,
        돌파 대비 +1.0% 이격 초과 시 추격 금지
"""

from datetime import datetime
from typing import Optional
from core.models import TradeSignal, TimeHorizon, OrderSide, MarketRegime
from core.tick_normalizer import normalize_price


class PDHBreakoutStrategy:
    STRATEGY_ID = "INT_PDH"

    @classmethod
    def evaluate(
        cls,
        iem_cd: str,
        name: str,
        current_price: int,
        pdh: int,
        aggregator,
        market_regime: MarketRegime,
        current_time: datetime,
        spread_ratio: float
    ) -> Optional[TradeSignal]:
        if pdh <= 0:
            return None

        # 1. 시장국면 (PANIC 금지)
        if market_regime == MarketRegime.PANIC:
            return None

        # 2. 스프레드 확인 <= 0.20%
        if spread_ratio > 0.0020:
            return None

        # 3. 돌파 여부 및 추격 매수 제한 (+1.0% 초과 금지)
        if current_price <= pdh:
            return None
        if current_price > pdh * 1.010:
            return None  # +1.0% 이상 이격 추격 금지

        # 4. 1분봉 종가 확인
        candles_1m = aggregator.candles_1m
        if not candles_1m:
            return None
        last_bar = candles_1m[-1]

        if last_bar.close <= pdh:
            return None

        # 5. Body Ratio >= 0.55 (양봉 캔들 몸통 비율)
        bar_range = last_bar.high - last_bar.low
        if bar_range <= 0:
            return None
        body = last_bar.close - last_bar.open
        if body <= 0 or (body / bar_range) < 0.55:
            return None

        # 6. VWAP 확인 (Price > VWAP)
        if current_price <= aggregator.vwap:
            return None

        # 7. RVOL >= 1.5
        rvol = aggregator.calculate_rvol("1m", 20)
        if rvol < 1.5:
            return None

        # 8. EMA9 > EMA20
        ema9 = aggregator.calculate_ema("1m", 9)
        ema20 = aggregator.calculate_ema("1m", 20)
        if ema9 <= ema20:
            return None

        # 9. RSI >= 55
        rsi = aggregator.calculate_rsi("1m", 14)
        if rsi < 55.0:
            return None

        # 10. 손절가 산출 (PDH 또는 1.5 * ATR14, 최대 3% 제한)
        atr = aggregator.calculate_atr("1m", 14)
        structure_stop = pdh
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
            score=90.0,
            reason=f"전일고가 돌파 (PDH={pdh:,}, RVOL={rvol:.1f}, RSI={rsi:.1f}, BodyRatio={body/bar_range:.2f})",
            timestamp=current_time,
            atr14=atr,
            expected_rr=2.0
        )
