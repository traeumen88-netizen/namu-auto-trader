"""Tick 데이터의 다중 타임프레임(1분/3분/5분/15분) 캔들 집계 및 기술적 지표 엔진
- Look-Ahead Bias 방지 (미완성 봉 지표 산출 분리)
- VWAP, EMA(9, 20, 60, 120, 240), RSI(14), ATR(14), RVOL 실시간 계산
"""

from datetime import datetime, timedelta
from typing import Dict, List, Optional
from core.models import Tick, Candle


class CandleAggregator:
    def __init__(self, iem_cd: str):
        self.iem_cd = iem_cd
        # 타임프레임별 완성된 봉 리스트
        self.candles_1m: List[Candle] = []
        self.candles_3m: List[Candle] = []
        self.candles_5m: List[Candle] = []
        self.candles_15m: List[Candle] = []

        # 현재 형성 중인 미완성 봉
        self.current_1m: Optional[Candle] = None
        self.current_3m: Optional[Candle] = None
        self.current_5m: Optional[Candle] = None
        self.current_15m: Optional[Candle] = None

        # 당일 누적 거래량 및 거래대금 (당일 VWAP 계산용)
        self.cum_volume: int = 0
        self.cum_turnover: float = 0.0
        self.vwap: float = 0.0

    def _get_bar_start_time(self, dt: datetime, minutes: int) -> datetime:
        """분 단위에 맞춘 봉 시작 시각 산출 (예: 5분봉 -> 09:00, 09:05 ...)"""
        discard_mins = dt.minute % minutes
        return dt.replace(minute=dt.minute - discard_mins, second=0, microsecond=0)

    def on_tick(self, tick: Tick):
        """실시간 Tick 수신 시 봉 업데이트 및 마감 감지"""
        price = tick.price
        vol = tick.volume
        turnover = price * vol
        dt = tick.timestamp

        # 당일 VWAP 갱신
        self.cum_volume += vol
        self.cum_turnover += turnover
        if self.cum_volume > 0:
            self.vwap = self.cum_turnover / self.cum_volume

        # 1분, 3분, 5분, 15분 봉 각각 갱신
        self._update_timeframe(tick, 1, "1m", self.candles_1m, "current_1m")
        self._update_timeframe(tick, 3, "3m", self.candles_3m, "current_3m")
        self._update_timeframe(tick, 5, "5m", self.candles_5m, "current_5m")
        self._update_timeframe(tick, 15, "15m", self.candles_15m, "current_15m")

    def _update_timeframe(
        self, tick: Tick, minutes: int, tf_str: str,
        history: List[Candle], current_attr: str
    ):
        bar_start = self._get_bar_start_time(tick.timestamp, minutes)
        cur: Optional[Candle] = getattr(self, current_attr)

        if cur is None:
            # 첫 번째 봉 시작
            new_candle = Candle(
                timestamp=bar_start,
                timeframe=tf_str,
                open=tick.price,
                high=tick.price,
                low=tick.price,
                close=tick.price,
                volume=tick.volume,
                turnover=tick.price * tick.volume,
                vwap=self.vwap,
                is_closed=False
            )
            setattr(self, current_attr, new_candle)
        elif bar_start > cur.timestamp:
            # 이전 봉 완성 -> history에 마감 처리하여 추가
            cur.is_closed = True
            history.append(cur)
            # 최대 1000개 봉 유지
            if len(history) > 1000:
                history.pop(0)

            # 새 봉 시작
            new_candle = Candle(
                timestamp=bar_start,
                timeframe=tf_str,
                open=tick.price,
                high=tick.price,
                low=tick.price,
                close=tick.price,
                volume=tick.volume,
                turnover=tick.price * tick.volume,
                vwap=self.vwap,
                is_closed=False
            )
            setattr(self, current_attr, new_candle)
        else:
            # 현재 봉 진행 중 갱신
            cur.high = max(cur.high, tick.price)
            cur.low = min(cur.low, tick.price)
            cur.close = tick.price
            cur.volume += tick.volume
            cur.turnover += tick.price * tick.volume
            cur.vwap = self.vwap

    # =========================================================================
    # 기술적 보조지표 계산 함수군 (Look-Ahead Bias 방지: 완성된 봉만 사용)
    # =========================================================================

    def calculate_vwap(self, timeframe: str = "1m") -> float:
        """당일 거래대금 가중평균가(VWAP) 반환"""
        return self.vwap

    def calculate_ema(self, timeframe: str, period: int) -> float:

        """완성된 봉 기준 지수이동평균(EMA) 계산"""
        history = self._get_history(timeframe)
        if len(history) < period:
            return float(history[-1].close) if history else 0.0

        multiplier = 2.0 / (period + 1.0)
        # 초기 단순이동평균(SMA)
        ema = sum(c.close for c in history[:period]) / period
        for c in history[period:]:
            ema = (c.close - ema) * multiplier + ema
        return ema

    def calculate_rsi(self, timeframe: str, period: int = 14) -> float:
        """완성된 봉 기준 상대강도지수(RSI) 산출"""
        history = self._get_history(timeframe)
        if len(history) <= period:
            return 50.0  # 데이터 부족 시 중립

        gains = []
        losses = []
        for i in range(1, len(history)):
            diff = history[i].close - history[i - 1].close
            if diff >= 0:
                gains.append(diff)
                losses.append(0.0)
            else:
                gains.append(0.0)
                losses.append(abs(diff))

        if len(gains) < period:
            return 50.0

        avg_gain = sum(gains[-period:]) / period
        avg_loss = sum(losses[-period:]) / period

        if avg_loss == 0:
            return 100.0
        rs = avg_gain / avg_loss
        return 100.0 - (100.0 / (1.0 + rs))

    def calculate_atr(self, timeframe: str, period: int = 14) -> float:
        """완성된 봉 기준 평균진폭(ATR) 산출"""
        history = self._get_history(timeframe)
        if len(history) < 2:
            return 0.0

        tr_list = []
        for i in range(1, len(history)):
            c = history[i]
            prev_c = history[i - 1]
            tr = max(
                c.high - c.low,
                abs(c.high - prev_c.close),
                abs(c.low - prev_c.close)
            )
            tr_list.append(tr)

        if len(tr_list) < period:
            return sum(tr_list) / len(tr_list) if tr_list else 0.0
        return sum(tr_list[-period:]) / period

    def calculate_rvol(self, timeframe: str = "1m", lookback: int = 20) -> float:
        """상대 거래량(Relative Volume: RVOL) = 현재 봉 거래량 / 최근 N개 봉 평균 거래량"""
        history = self._get_history(timeframe)
        cur = getattr(self, f"current_{timeframe}", None)
        if not history or not cur:
            return 1.0

        sample = history[-lookback:]
        avg_vol = sum(c.volume for c in sample) / len(sample)
        if avg_vol <= 0:
            return 1.0
        return cur.volume / avg_vol

    def _get_history(self, timeframe: str) -> List[Candle]:
        if timeframe == "1m":
            return self.candles_1m
        elif timeframe == "3m":
            return self.candles_3m
        elif timeframe == "5m":
            return self.candles_5m
        elif timeframe == "15m":
            return self.candles_15m
        return self.candles_1m
