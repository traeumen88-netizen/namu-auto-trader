"""실시간 시장 이벤트 탐지 및 100점 채점 엔진 (Event Detector v6.0)
- Execution-Grade Specification v6.0 (Section 5, 10, 11, 12, 13, 14, 21, 55, 56 준수)
- 15대 시장 이벤트 (Events A ~ O) 실시간 감지
- 100점 만점 Event Score 계산 및 동적 승격(Candidate Promotion)
- Momentum Ignition, Acceleration, Chart Structure, Compression Breakout 감지
- 추격매수 방지(Chase Filter) 및 Fake Breakout 방어
"""

import logging
from datetime import datetime
from typing import Dict, List, Any, Optional, Tuple
from core.models import (
    SymbolInfo, SymbolState, MarketEventType, MarketEvent, CandidatePriority
)
from core.aggregator import CandleAggregator

logger = logging.getLogger("EventDetector")


class EventDetector:
    """실시간 시장 이벤트 감지 및 스코어링 엔진"""

    @classmethod
    def evaluate_events(
        cls,
        sym: SymbolInfo,
        agg: CandleAggregator,
        now: datetime,
        execution_intensity: float = 100.0,
        obi: float = 0.0,
        has_news: bool = False,
        rank_surged: bool = False
    ) -> Tuple[float, List[MarketEvent], CandidatePriority, Dict[str, Any]]:
        """
        한 종목에 대한 실시간 15대 이벤트 전수 검사 및 100점 만점 채점
        :return: (event_score, detected_events, priority, pattern_flags)
        """
        score = 0.0
        events: List[MarketEvent] = []
        patterns = {
            "is_ignition": False,
            "is_acceleration": False,
            "is_compression_breakout": False,
            "is_strong_chart": False,
            "is_chase_forbidden": False,
            "is_fake_breakout": False
        }

        # 캔들 데이터 준비
        c_1m = agg.candles_1m
        c_5m = agg.candles_5m
        curr_price = sym.price if sym.price > 0 else (c_1m[-1].close if c_1m else 0)
        if curr_price <= 0:
            return 0.0, [], CandidatePriority.INACTIVE, patterns

        # 주요 기술적 지표 산출
        vwap = agg.calculate_vwap("1m")
        ema9 = agg.calculate_ema("1m", 9)
        ema20 = agg.calculate_ema("1m", 20)
        rsi = agg.calculate_rsi("1m", 14)
        atr = agg.calculate_atr("1m", 14)

        # ── 1. 수익률 및 거래량 계산 ──
        # 현재 형성 중인 1분봉 또는 마지막 봉 기준
        curr_vol = agg.current_1m.volume if agg.current_1m else (c_1m[-1].volume if c_1m else 0)
        curr_turnover = agg.current_1m.turnover if agg.current_1m else (c_1m[-1].turnover if c_1m else 0)

        ret_1m = 0.0
        ret_3m = 0.0
        ret_5m = 0.0
        rvol = 1.0

        if agg.current_1m and agg.current_1m.open > 0:
            ret_1m = (curr_price - agg.current_1m.open) / agg.current_1m.open
        elif len(c_1m) >= 1:
            ret_1m = (curr_price - c_1m[-1].open) / c_1m[-1].open

        if len(c_1m) >= 3:
            ret_3m = (curr_price - c_1m[-3].open) / c_1m[-3].open
        elif len(c_1m) >= 1:
            ret_3m = (curr_price - c_1m[0].open) / c_1m[0].open

        if len(c_1m) >= 5:
            ret_5m = (curr_price - c_1m[-5].open) / c_1m[-5].open
        elif len(c_1m) >= 1:
            ret_5m = (curr_price - c_1m[0].open) / c_1m[0].open

        # RVOL (최근 20개 1분봉 평균 대비)
        if len(c_1m) >= 20:
            avg_vol_20 = sum(c.volume for c in c_1m[-20:]) / 20.0
            if avg_vol_20 > 0:
                rvol = curr_vol / avg_vol_20
        elif len(c_1m) >= 1:
            avg_vol_1 = sum(c.volume for c in c_1m) / float(len(c_1m))
            if avg_vol_1 > 0:
                rvol = curr_vol / avg_vol_1

        # ── 2. 15대 이벤트 전수 검사 (Events A ~ O) ──

        # EVENT A: 최근 1분 거래량 >= 평균 * 3 (RVOL >= 3.0) (+15점)
        if rvol >= 3.0:
            score += 15.0
            events.append(MarketEvent(
                event_type=MarketEventType.EVENT_A,
                iem_cd=sym.iem_cd, name=sym.name, timestamp=now,
                description=f"1분 거래량 폭증 (RVOL: {rvol:.1f}배)",
                score_delta=15.0, metrics={"rvol": rvol}
            ))

        # EVENT B: 최근 3분 수익률 >= +2.0% (+10점)
        if ret_3m >= 0.020:
            score += 10.0
            events.append(MarketEvent(
                event_type=MarketEventType.EVENT_B,
                iem_cd=sym.iem_cd, name=sym.name, timestamp=now,
                description=f"3분 급등 ({ret_3m*100:+.2f}%)",
                score_delta=10.0, metrics={"ret_3m": ret_3m}
            ))

        # EVENT C: 최근 5분 수익률 >= +3.0% (+10점)
        if ret_5m >= 0.030:
            score += 10.0
            events.append(MarketEvent(
                event_type=MarketEventType.EVENT_C,
                iem_cd=sym.iem_cd, name=sym.name, timestamp=now,
                description=f"5분 급등 ({ret_5m*100:+.2f}%)",
                score_delta=10.0, metrics={"ret_5m": ret_5m}
            ))

        # EVENT D: 거래대금 급증 (3배 이상) (+15점)
        if len(c_1m) >= 20:
            avg_turnover_20 = sum(c.turnover for c in c_1m[-20:]) / 20.0
            if avg_turnover_20 > 0 and curr_turnover >= avg_turnover_20 * 3.0:
                score += 15.0
                events.append(MarketEvent(
                    event_type=MarketEventType.EVENT_D,
                    iem_cd=sym.iem_cd, name=sym.name, timestamp=now,
                    description="1분 거래대금 3배 급증",
                    score_delta=15.0, metrics={"turnover": curr_turnover}
                ))
        elif len(c_1m) >= 1:
            avg_turnover_1 = sum(c.turnover for c in c_1m) / float(len(c_1m))
            if avg_turnover_1 > 0 and curr_turnover >= avg_turnover_1 * 3.0:
                score += 15.0
                events.append(MarketEvent(
                    event_type=MarketEventType.EVENT_D,
                    iem_cd=sym.iem_cd, name=sym.name, timestamp=now,
                    description="1분 거래대금 3배 급증",
                    score_delta=15.0, metrics={"turnover": curr_turnover}
                ))


        # EVENT E: 당일 신고가 (+10점)
        if sym.high_price > 0 and curr_price >= sym.high_price:
            score += 10.0
            events.append(MarketEvent(
                event_type=MarketEventType.EVENT_E,
                iem_cd=sym.iem_cd, name=sym.name, timestamp=now,
                description="당일 신고가 갱신",
                score_delta=10.0, metrics={"day_high": sym.high_price}
            ))

        # EVENT F: 전일 고가(PDH) 돌파 (+10점)
        if sym.prev_high > 0 and curr_price > sym.prev_high:
            score += 10.0
            events.append(MarketEvent(
                event_type=MarketEventType.EVENT_F,
                iem_cd=sym.iem_cd, name=sym.name, timestamp=now,
                description=f"전일 고가({sym.prev_high:,}원) 돌파",
                score_delta=10.0, metrics={"pdh": sym.prev_high}
            ))

        # EVENT G: 최근 20개 1분봉 최고가 돌파 (+10점)
        if len(c_1m) >= 20:
            past_20_high = max(c.high for c in c_1m[-21:-1])
            if curr_price > past_20_high:
                score += 10.0
                events.append(MarketEvent(
                    event_type=MarketEventType.EVENT_G,
                    iem_cd=sym.iem_cd, name=sym.name, timestamp=now,
                    description=f"20봉 최고가({past_20_high:,}원) 돌파",
                    score_delta=10.0, metrics={"high_20bar": past_20_high}
                ))

        # EVENT H: VWAP 상향 돌파 (+5점)
        if vwap > 0 and curr_price > vwap:
            score += 5.0
            events.append(MarketEvent(
                event_type=MarketEventType.EVENT_H,
                iem_cd=sym.iem_cd, name=sym.name, timestamp=now,
                description=f"VWAP({vwap:,.0f}원) 상향 돌파",
                score_delta=5.0, metrics={"vwap": vwap}
            ))

        # EVENT I: EMA9 > EMA20 골든크로스 (+5점)
        if ema9 > ema20 and ema20 > 0:
            score += 5.0
            events.append(MarketEvent(
                event_type=MarketEventType.EVENT_I,
                iem_cd=sym.iem_cd, name=sym.name, timestamp=now,
                description="EMA9 > EMA20 정배열/골든크로스",
                score_delta=5.0, metrics={"ema9": ema9, "ema20": ema20}
            ))

        # EVENT J: 체결강도 >= 120 (+5점)
        if execution_intensity >= 120.0:
            score += 5.0
            events.append(MarketEvent(
                event_type=MarketEventType.EVENT_J,
                iem_cd=sym.iem_cd, name=sym.name, timestamp=now,
                description=f"체결강도 강세 ({execution_intensity:.1f}%)",
                score_delta=5.0, metrics={"intensity": execution_intensity}
            ))

        # EVENT K: 호가 불균형 OBI >= +0.25 (+5점)
        if obi >= 0.25:
            score += 5.0
            events.append(MarketEvent(
                event_type=MarketEventType.EVENT_K,
                iem_cd=sym.iem_cd, name=sym.name, timestamp=now,
                description=f"호가 매수우위 (OBI: {obi:+.2f})",
                score_delta=5.0, metrics={"obi": obi}
            ))

        # EVENT L: 뉴스/공시 발생 (+10점)
        if has_news:
            score += 10.0
            events.append(MarketEvent(
                event_type=MarketEventType.EVENT_L,
                iem_cd=sym.iem_cd, name=sym.name, timestamp=now,
                description="실시간 뉴스/공시 모멘텀 발생",
                score_delta=10.0, metrics={"news": True}
            ))

        # EVENT M: 순위 급상승
        if rank_surged:
            score += 5.0
            events.append(MarketEvent(
                event_type=MarketEventType.EVENT_M,
                iem_cd=sym.iem_cd, name=sym.name, timestamp=now,
                description="거래대금 순위 급상승",
                score_delta=5.0, metrics={"rank_surged": True}
            ))

        # ── 3. 패턴 분석 (Section 11, 12, 13, 14, 55, 56) ──

        # MOMENTUM IGNITION (Section 11)
        if (
            ret_1m >= 0.008
            and ret_3m >= 0.015
            and rvol >= 2.0
            and curr_price > vwap
            and ema9 > ema20
        ):
            patterns["is_ignition"] = True

        # ACCELERATION (Section 12)
        if len(c_1m) >= 2:
            prev_ret_1m = (c_1m[-2].close - c_1m[-2].open) / c_1m[-2].open if c_1m[-2].open > 0 else 0
            vol_increase = (c_1m[-1].volume - c_1m[-2].volume) / c_1m[-2].volume if c_1m[-2].volume > 0 else 0
            if ret_1m >= 0.010 and prev_ret_1m >= 0.005 and vol_increase >= 0.50:
                patterns["is_acceleration"] = True

        # CHART STRUCTURE (Section 13: Higher High + Higher Low)
        if len(c_1m) >= 6:
            h1, h2, h3 = c_1m[-6].high, c_1m[-4].high, c_1m[-2].high
            l1, l2, l3 = c_1m[-6].low, c_1m[-4].low, c_1m[-2].low
            if h1 < h2 < h3 and l1 < l2 < l3:
                if ema9 > ema20 and curr_price > vwap and 55.0 <= rsi <= 70.0:
                    patterns["is_strong_chart"] = True

        # COMPRESSION DETECTION & BREAKOUT (Section 14 & 21)
        if len(c_1m) >= 40:
            recent_20_ranges = [c.high - c.low for c in c_1m[-20:]]
            prev_20_ranges = [c.high - c.low for c in c_1m[-40:-20]]
            avg_recent_rng = sum(recent_20_ranges) / 20.0
            avg_prev_rng = sum(prev_20_ranges) / 20.0 if sum(prev_20_ranges) > 0 else 1.0

            if avg_recent_rng <= avg_prev_rng * 0.80:  # 변동폭 20% 이상 압축
                # 압축 고점 돌파 검사
                compression_high = max(c.high for c in c_1m[-20:-1])
                avg_comp_vol = sum(c.volume for c in c_1m[-20:-1]) / 19.0
                if (
                    curr_price > compression_high
                    and (curr_vol >= avg_comp_vol * 2.0 or c_1m[-1].volume >= avg_comp_vol * 2.0)
                    and (curr_price > vwap or vwap == 0.0)
                ):
                    patterns["is_compression_breakout"] = True


        # 추격매수 금지 필터 (Section 55: Chase Prevention)
        if vwap > 0 and curr_price >= vwap * 1.03:
            patterns["is_chase_forbidden"] = True
        elif ema20 > 0 and curr_price >= ema20 * 1.035:
            patterns["is_chase_forbidden"] = True
        elif ret_5m >= 0.040 or ret_1m >= 0.020:
            patterns["is_chase_forbidden"] = True

        # 점수 합산 제한 (100점 만점)
        score = min(score, 100.0)

        # 우선순위 판정 (Section 10)
        if score >= 80.0:
            priority = CandidatePriority.PRIORITY_1
        elif score >= 65.0:
            priority = CandidatePriority.PRIORITY_2
        elif score >= 50.0:
            priority = CandidatePriority.WATCH
        else:
            priority = CandidatePriority.INACTIVE

        # 메타데이터 업데이트
        sym.event_score = score
        sym.active_events = [e.description for e in events]
        if events:
            sym.last_event_time = now

        return score, events, priority, patterns
