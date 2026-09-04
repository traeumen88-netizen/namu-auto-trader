"""단타 전략 ①: Opening Range Breakout (ORB)
- 09:00~09:05 5분봉의 고가(OR_HIGH)를 돌파 시 진입
- 조건: 시간 09:05~10:30, Close > OR_HIGH, RVOL >= 1.5, Price > VWAP,
        1분 EMA9 > EMA20, 3분 EMA9 > EMA20, RSI >= 55, Market != BEAR/PANIC,
        스프레드 <= 0.20%, R:R >= 1.5, 추격 제한 +1.2% 이내
"""

from datetime import datetime, time
from typing import Optional, Dict, Any
from core.models import TradeSignal, TimeHorizon, OrderSide, MarketRegime
from core.tick_normalizer import normalize_price


class ORBStrategy:
    STRATEGY_ID = "INT_ORB"

    @classmethod
    def evaluate(
        cls,
        iem_cd: str,
        name: str,
        current_price: int,
        aggregator,
        market_regime: MarketRegime,
        current_time: datetime,
        spread_ratio: float,
        or_high: Optional[int],
        or_low: Optional[int]
    ) -> Optional[TradeSignal]:
        # 1. 시간 필터: 09:05:00 ~ 10:30:00
        t = current_time.time()
        if not (time(9, 5) <= t <= time(10, 30)):
            return None

        if or_high is None or or_low is None or or_high <= 0:
            return None

        # 2. 시장국면 필터 (BEAR, PANIC 금지)
        if market_regime in (MarketRegime.BEAR, MarketRegime.PANIC):
            return None

        # 3. 유동성 스프레드 필터 <= 0.20%
        if spread_ratio > 0.0020:
            return None

        # 4. 돌파 및 추격 매수 제한 (+1.2% 이격 초과 시 금지)
        if current_price <= or_high:
            return None
        if current_price > or_high * 1.012:
            return None  # 추격 금지

        # 5. 완성된 1분봉 데이터 및 지표 추출
        candles_1m = aggregator.candles_1m
        if not candles_1m:
            return None
        last_bar = candles_1m[-1]

        # 1분봉 종가 > OR_HIGH 확인
        if last_bar.close <= or_high:
            return None

        # 6. VWAP 확인 (Price > VWAP)
        if current_price <= aggregator.vwap:
            return None

        # 7. RVOL >= 1.5
        rvol = aggregator.calculate_rvol("1m", lookback=20)
        if rvol < 1.5:
            return None

        # 8. 이동평균선 정배열 (1분봉 EMA9 > EMA20, 3분봉 EMA9 > EMA20)
        ema9_1m = aggregator.calculate_ema("1m", 9)
        ema20_1m = aggregator.calculate_ema("1m", 20)
        if ema9_1m <= ema20_1m:
            return None

        ema9_3m = aggregator.calculate_ema("3m", 9)
        ema20_3m = aggregator.calculate_ema("3m", 20)
        if ema9_3m <= ema20_3m:
            return None

        # 9. RSI >= 55
        rsi = aggregator.calculate_rsi("1m", 14)
        if rsi < 55.0:
            return None

        # 10. 손절가 산출 (Structure Stop or 1.5 * ATR14, 최대 3% 제한)
        atr = aggregator.calculate_atr("1m", 14)
        structure_stop = or_low  # ORB 구조적 손절: OR_LOW
        atr_stop = int(current_price - 1.5 * atr) if atr > 0 else int(current_price * 0.98)
        stop_price = max(structure_stop, atr_stop)

        # 손절폭 검사 (> 3% 면 신규 진입 금지)
        stop_distance = current_price - stop_price
        if stop_distance > current_price * 0.03 or stop_distance <= 0:
            return None

        # 11. 예상 R:R >= 1.5 검증
        target_1r = current_price + stop_distance
        target_2r = current_price + int(stop_distance * 2.0)
        target_3r = current_price + int(stop_distance * 3.0)
        expected_rr = (target_2r - current_price) / stop_distance
        if expected_rr < 1.5:
            return None

        # 정규화
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
            score=92.0,
            reason=f"ORB 돌파 (OR_HIGH={or_high:,}, RVOL={rvol:.1f}, RSI={rsi:.1f})",
            timestamp=current_time,
            atr14=atr,
            expected_rr=expected_rr
        )
