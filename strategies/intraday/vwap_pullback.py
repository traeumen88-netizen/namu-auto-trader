"""단타 전략 ③: VWAP Pullback (VWAP 눌림목 반등)
- 상승 추세 진행 중 거래량 급감하며 VWAP에 안착 후 재반등하는 셋업
- 조건: EMA9 > EMA20, Price > VWAP, RSI >= 50, ABS(Price - VWAP)/VWAP <= 0.30%,
        눌림 거래량 <= 상승구간 평균 거래량 * 0.70, 현재 봉 High > 직전 봉 High
"""

from datetime import datetime
from typing import Optional
from core.models import TradeSignal, TimeHorizon, OrderSide, MarketRegime
from core.tick_normalizer import normalize_price


class VWAPPullbackStrategy:
    STRATEGY_ID = "INT_VWAP_PULLBACK"

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
        # 1. 시장국면 (BEAR, PANIC 금지)
        if market_regime in (MarketRegime.BEAR, MarketRegime.PANIC):
            return None

        # 2. 스프레드 확인
        if spread_ratio > 0.0020:
            return None

        candles_1m = aggregator.candles_1m
        if len(candles_1m) < 10:
            return None

        vwap = aggregator.vwap
        if vwap <= 0:
            return None

        # 3. 추세 전제조건: EMA9 > EMA20, Price > VWAP, RSI >= 50
        ema9 = aggregator.calculate_ema("1m", 9)
        ema20 = aggregator.calculate_ema("1m", 20)
        if ema9 <= ema20:
            return None
        if current_price < vwap:
            return None

        rsi = aggregator.calculate_rsi("1m", 14)
        if rsi < 50.0:
            return None

        # 4. VWAP 접근 거리: ABS(Price - VWAP) / VWAP <= 0.30%
        vwap_dist = abs(current_price - vwap) / vwap
        if vwap_dist > 0.0030:
            return None

        # 5. 거래량 수축 확인: 최근 3봉 평균(눌림 거래량) <= 이전 7봉 평균 * 0.70
        pullback_bars = candles_1m[-3:]
        impulse_bars = candles_1m[-10:-3]
        avg_pullback_vol = sum(b.volume for b in pullback_bars) / len(pullback_bars)
        avg_impulse_vol = sum(b.volume for b in impulse_bars) / len(impulse_bars) if impulse_bars else avg_pullback_vol

        if avg_pullback_vol > avg_impulse_vol * 0.70:
            return None

        # 6. 반등 시그널: 현재 봉 High > 직전 봉 High
        cur_bar = aggregator.current_1m
        last_bar = candles_1m[-1]
        if not cur_bar or cur_bar.high <= last_bar.high:
            return None

        # 7. 손절가 (VWAP 하단 또는 1.5 * ATR14, 최대 3% 제한)
        atr = aggregator.calculate_atr("1m", 14)
        structure_stop = int(vwap * 0.997)  # VWAP 바로 아래 0.3%
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
            score=88.0,
            reason=f"VWAP 눌림목 반등 (VWAP거리={vwap_dist*100:.2f}%, 거래량수축={avg_pullback_vol/avg_impulse_vol:.2f}x)",
            timestamp=current_time,
            atr14=atr,
            expected_rr=2.0,
            entry_timing_valid=True,
            timing_reason="VWAP눌림목_반등확인",
            rule_score=88.0,
            approved_status="BUY_APPROVED"
        )
