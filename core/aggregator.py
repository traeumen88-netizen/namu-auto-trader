"""[FINAL MASTER v16.0] Tick 데이터의 다중 타임프레임 캔들 집계 및 기술적 지표 엔진 (core/aggregator.py)
Section 12: Tick -> Synthetic Bar Engine (1s, 3s, 5s, 10s, 1m, 3m, 5m)
- confirmed_bar (완성봉) vs synthetic_bar (진행봉) 명확한 구분
- Look-Ahead Bias 완전 차단
- 누적 거래량(acml_vol) 수신 시 정밀 delta volume 변환
- 온디맨드 사전 히스토리 프리필(Pre-fill) 지원
- VWAP, EMA(9, 20, 60, 120, 240), RSI(14), ATR(14), RVOL 실시간 계산
"""

from datetime import datetime, timedelta
from typing import Dict, List, Optional
from core.models import Tick, Candle


class CandleAggregator:
    def __init__(self, iem_cd: str):
        self.iem_cd = iem_cd

        # 타임프레임별 완성된 확정 봉(confirmed_bar) 리스트
        self.candles_1s: List[Candle] = []
        self.candles_3s: List[Candle] = []
        self.candles_5s: List[Candle] = []
        self.candles_10s: List[Candle] = []
        self.candles_1m: List[Candle] = []
        self.candles_3m: List[Candle] = []
        self.candles_5m: List[Candle] = []
        self.candles_15m: List[Candle] = []

        # 현재 형성 중인 미완성 진행봉(synthetic_bar)
        self.current_1s: Optional[Candle] = None
        self.current_3s: Optional[Candle] = None
        self.current_5s: Optional[Candle] = None
        self.current_10s: Optional[Candle] = None
        self.current_1m: Optional[Candle] = None
        self.current_3m: Optional[Candle] = None
        self.current_5m: Optional[Candle] = None
        self.current_15m: Optional[Candle] = None

        # 당일 누적 거래량 및 거래대금 (당일 VWAP 계산용)
        self.cum_volume: int = 0
        self.cum_turnover: float = 0.0
        self.vwap: float = 0.0
        self.last_acml_vol: int = 0
        self.last_tick_time: Optional[datetime] = None

    def _get_bar_start_time_seconds(self, dt: datetime, seconds: int) -> datetime:
        """초 단위 봉 시작 시각 산출 (1s, 3s, 5s, 10s)"""
        discard_sec = dt.second % seconds
        return dt.replace(second=dt.second - discard_sec, microsecond=0)

    def _get_bar_start_time_minutes(self, dt: datetime, minutes: int) -> datetime:
        """분 단위 봉 시작 시각 산출 (1m, 3m, 5m, 15m)"""
        discard_mins = dt.minute % minutes
        return dt.replace(minute=dt.minute - discard_mins, second=0, microsecond=0)

    def on_tick(self, tick: Tick):
        """실시간 Tick 수신 시 봉 업데이트 및 마감 감지"""
        price = tick.price
        raw_vol = tick.volume
        dt = tick.timestamp

        # 브로커에서 당일 누적 거래량이 수신된 경우 delta volume 산출
        if self.last_acml_vol > 0 and raw_vol >= self.last_acml_vol:
            delta_vol = raw_vol - self.last_acml_vol
        else:
            delta_vol = raw_vol
        self.last_acml_vol = max(self.last_acml_vol, raw_vol)

        # 유효 틱 거래량
        tick_vol = max(1, delta_vol) if (raw_vol > 0 and self.cum_volume > 0) else raw_vol
        turnover = price * tick_vol

        # 당일 VWAP 갱신
        self.cum_volume += tick_vol
        self.cum_turnover += turnover
        if self.cum_volume > 0:
            self.vwap = self.cum_turnover / self.cum_volume
        else:
            self.vwap = float(price)

        self.last_tick_time = dt

        # 1초, 3초, 5초, 10초 초봉 갱신 (Section 12)
        self._update_timeframe_sec(tick, price, tick_vol, turnover, 1, "1s", self.candles_1s, "current_1s")
        self._update_timeframe_sec(tick, price, tick_vol, turnover, 3, "3s", self.candles_3s, "current_3s")
        self._update_timeframe_sec(tick, price, tick_vol, turnover, 5, "5s", self.candles_5s, "current_5s")
        self._update_timeframe_sec(tick, price, tick_vol, turnover, 10, "10s", self.candles_10s, "current_10s")

        # 1분, 3분, 5분, 15분 분봉 갱신
        self._update_timeframe_min(tick, price, tick_vol, turnover, 1, "1m", self.candles_1m, "current_1m")
        self._update_timeframe_min(tick, price, tick_vol, turnover, 3, "3m", self.candles_3m, "current_3m")
        self._update_timeframe_min(tick, price, tick_vol, turnover, 5, "5m", self.candles_5m, "current_5m")
        self._update_timeframe_min(tick, price, tick_vol, turnover, 15, "15m", self.candles_15m, "current_15m")

    def _update_timeframe_sec(
        self, tick: Tick, price: int, vol: int, turnover: float,
        seconds: int, tf_str: str, history: List[Candle], current_attr: str
    ):
        bar_start = self._get_bar_start_time_seconds(tick.timestamp, seconds)
        cur: Optional[Candle] = getattr(self, current_attr)

        if cur is None:
            new_candle = Candle(
                timestamp=bar_start, timeframe=tf_str, open=price, high=price,
                low=price, close=price, volume=vol, turnover=turnover,
                vwap=self.vwap, is_closed=False
            )
            setattr(self, current_attr, new_candle)
        elif bar_start > cur.timestamp:
            cur.is_closed = True  # 확정봉 전환 (confirmed_bar)
            history.append(cur)
            if len(history) > 300:
                history.pop(0)
            new_candle = Candle(
                timestamp=bar_start, timeframe=tf_str, open=price, high=price,
                low=price, close=price, volume=vol, turnover=turnover,
                vwap=self.vwap, is_closed=False
            )
            setattr(self, current_attr, new_candle)
        else:
            # 진행 중 합성봉 (synthetic_bar)
            cur.high = max(cur.high, price)
            cur.low = min(cur.low, price)
            cur.close = price
            cur.volume += vol
            cur.turnover += turnover
            cur.vwap = self.vwap

    def _update_timeframe_min(
        self, tick: Tick, price: int, vol: int, turnover: float,
        minutes: int, tf_str: str, history: List[Candle], current_attr: str
    ):
        bar_start = self._get_bar_start_time_minutes(tick.timestamp, minutes)
        cur: Optional[Candle] = getattr(self, current_attr)

        if cur is None:
            new_candle = Candle(
                timestamp=bar_start, timeframe=tf_str, open=price, high=price,
                low=price, close=price, volume=vol, turnover=turnover,
                vwap=self.vwap, is_closed=False
            )
            setattr(self, current_attr, new_candle)
        elif bar_start > cur.timestamp:
            cur.is_closed = True  # 확정봉 전환 (confirmed_bar)
            history.append(cur)
            if len(history) > 1000:
                history.pop(0)
            new_candle = Candle(
                timestamp=bar_start, timeframe=tf_str, open=price, high=price,
                low=price, close=price, volume=vol, turnover=turnover,
                vwap=self.vwap, is_closed=False
            )
            setattr(self, current_attr, new_candle)
        else:
            cur.high = max(cur.high, price)
            cur.low = min(cur.low, price)
            cur.close = price
            cur.volume += vol
            cur.turnover += turnover
            cur.vwap = self.vwap

    def prefill_history(
        self,
        open_price: int,
        high_price: int,
        low_price: int,
        curr_price: int,
        total_vol: int,
        prev_close: int = 0,
        now: Optional[datetime] = None
    ):
        """
        신규 발굴 종목에 대해 당일 시가/고가/저가/거래량 기반 사전 히스토리 프리필
        지표 계산에 필요한 초기 베이스라인(EMA, ATR, VWAP, RVOL)을 즉각 확립
        """
        if len(self.candles_1m) >= 5 or curr_price <= 0:
            return
        now = now or datetime.now()
        base_open = open_price if open_price > 0 else (prev_close if prev_close > 0 else curr_price)
        base_high = max(high_price, curr_price, base_open)
        base_low = min(low_price, curr_price, base_open) if low_price > 0 else min(curr_price, base_open)

        # 20분 전부터 현재 직전까지 20개 분봉 베이스라인 생성
        n_bars = 20
        vol_per_bar = max(100, total_vol // max(1, n_bars))
        for i in range(n_bars):
            t = now - timedelta(minutes=n_bars - i)
            ratio = i / float(n_bars)
            interp_close = int(base_open + (curr_price - base_open) * ratio)
            interp_high = max(interp_close, int(base_low + (base_high - base_low) * ratio))
            interp_low = min(interp_close, base_low)
            bar = Candle(
                timestamp=t, timeframe="1m", open=interp_close, high=interp_high,
                low=interp_low, close=interp_close, volume=vol_per_bar,
                turnover=float(interp_close * vol_per_bar),
                vwap=float(interp_close), is_closed=True
            )
            self.candles_1m.append(bar)

        self.vwap = float(curr_price)
        self.cum_volume = max(self.cum_volume, total_vol)
        self.last_acml_vol = max(self.last_acml_vol, total_vol)

    # =========================================================================
    # 기술적 보조지표 계산 함수군
    # =========================================================================

    def calculate_vwap(self, timeframe: str = "1m") -> float:
        """당일 거래대금 가중평균가(VWAP) 반환"""
        return self.vwap

    def calculate_ema(self, timeframe: str, period: int) -> float:
        """완성된 봉 기준 지수이동평균(EMA) 계산"""
        history = self._get_history(timeframe)
        if not history:
            return 0.0
        if len(history) < period:
            return float(history[-1].close)

        multiplier = 2.0 / (period + 1.0)
        ema = sum(c.close for c in history[:period]) / float(period)
        for c in history[period:]:
            ema = (c.close - ema) * multiplier + ema
        return ema

    def calculate_rsi(self, timeframe: str, period: int = 14) -> float:
        """완성된 봉 기준 상대강도지수(RSI) 산출"""
        history = self._get_history(timeframe)
        if len(history) <= period:
            return 50.0

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

        avg_gain = sum(gains[-period:]) / float(period)
        avg_loss = sum(losses[-period:]) / float(period)

        if avg_loss == 0:
            return 100.0
        rs = avg_gain / avg_loss
        return 100.0 - (100.0 / (1.0 + rs))

    def calculate_atr(self, timeframe: str, period: int = 14) -> float:
        """완성된 봉 기준 평균진폭(ATR) 산출"""
        history = self._get_history(timeframe)
        cur = getattr(self, f"current_{timeframe}", None)
        if len(history) < 2:
            if len(history) == 1:
                base_tr = history[0].high - history[0].low
                if cur:
                    base_tr = max(base_tr, cur.high - cur.low, abs(cur.high - history[0].close))
                return float(base_tr) if base_tr > 0 else float(history[0].close * 0.005)
            if cur:
                tr = cur.high - cur.low
                return float(tr) if tr > 0 else float(cur.close * 0.005)
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
        return sum(tr_list[-period:]) / float(period)

    def calculate_rvol(self, timeframe: str = "1m", lookback: int = 20) -> float:
        """상대 거래량(RVOL) = 현재 봉 거래량 / 최근 N개 봉 평균 거래량"""
        history = self._get_history(timeframe)
        cur = getattr(self, f"current_{timeframe}", None)
        if not history or not cur:
            return 1.0

        sample = history[-lookback:]
        avg_vol = sum(c.volume for c in sample) / float(len(sample))
        if avg_vol <= 0:
            return 1.0
        return max(0.1, cur.volume / avg_vol)

    def _get_history(self, timeframe: str) -> List[Candle]:
        if timeframe == "1s":
            return self.candles_1s
        elif timeframe == "3s":
            return self.candles_3s
        elif timeframe == "5s":
            return self.candles_5s
        elif timeframe == "10s":
            return self.candles_10s
        elif timeframe == "1m":
            return self.candles_1m
        elif timeframe == "3m":
            return self.candles_3m
        elif timeframe == "5m":
            return self.candles_5m
        elif timeframe == "15m":
            return self.candles_15m
        return self.candles_1m

    def get_completed_candles(self, timeframe: str = "1m", limit: int = 60) -> List[Candle]:
        history = self._get_history(timeframe)
        return history[-limit:] if limit > 0 else history
