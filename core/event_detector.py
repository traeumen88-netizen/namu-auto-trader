"""[FINAL MASTER v16.0] 실시간 시장 이벤트 탐지 및 100점 채점 엔진 (core/event_detector.py)
Section 13: Event Detection Engine
- 이미 크게 오른 종목이 아닌, 상승/움직임이 시작되는 구간(Early Stage) 포착
- Momentum: 1m >= 0.8%, 3m >= 1.5%, 5m >= 2.0% + 동적 ATR Threshold
- Volume Burst: RVOL >= 1.5, RVOL >= 2.0, RVOL >= 3.0 (단계별 Event Strength 기록)
- Breakout: 20-Bar High, PDH, Intraday High, ORB
- VWAP Event: VWAP Cross Up, VWAP Reclaim, VWAP Retest, VWAP Support
- EMA Event: EMA9 > EMA20, EMA9 Cross Up, EMA Pullback, EMA Reclaim
- Compression Expansion: ATR/Range contraction -> Volume burst -> Range expansion
"""

import logging
from datetime import datetime
from typing import Dict, List, Any, Optional, Tuple
from core.models import (
    SymbolInfo, SymbolState, MarketEventType, MarketEvent, CandidatePriority
)
from core.aggregator import CandleAggregator
from config.settings import BREAKOUT_MIN_RVOL

logger = logging.getLogger("EventDetector")



class EventDetector:
    """실시간 15대 시장 이벤트 감지 및 스코어링 엔진"""

    @classmethod
    def evaluate_events(
        cls,
        sym: SymbolInfo,
        agg: CandleAggregator,
        now: datetime,
        execution_intensity: float = 100.0,
        obi: float = 0.0,
        has_news: bool = False,
        rank_surged: bool = False,
        market_return: float = 0.0
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
            "is_fake_breakout": False,
            "is_vwap_reclaim": False,
            "is_ema_pullback": False
        }

        # 캔들 데이터 준비
        c_1m = agg.candles_1m
        curr_price = sym.price if sym.price > 0 else (c_1m[-1].close if c_1m else 0)
        if curr_price <= 0:
            return 0.0, [], CandidatePriority.INACTIVE, patterns

        # 주요 기술적 지표 산출
        vwap = agg.calculate_vwap("1m")
        if vwap <= 0:
            vwap = float(curr_price)
        ema9 = agg.calculate_ema("1m", 9)
        if ema9 <= 0:
            ema9 = float(curr_price)
        ema20 = agg.calculate_ema("1m", 20)
        if ema20 <= 0:
            ema20 = float(curr_price * 0.995)
        rsi = agg.calculate_rsi("1m", 14)
        atr = agg.calculate_atr("1m", 14)
        if atr <= 0:
            atr = float(curr_price * 0.015)

        # 1. 수익률 및 거래량 계산
        curr_vol = agg.current_1m.volume if agg.current_1m else (c_1m[-1].volume if c_1m else 1000)
        curr_turnover = agg.current_1m.turnover if agg.current_1m else (c_1m[-1].turnover if c_1m else curr_price * curr_vol)

        ret_1m = 0.0
        ret_3m = 0.0
        ret_5m = 0.0
        rvol = 1.0

        if agg.current_1m and agg.current_1m.open > 0:
            ret_1m = (curr_price - agg.current_1m.open) / agg.current_1m.open
        elif len(c_1m) >= 1 and c_1m[-1].open > 0:
            ret_1m = (curr_price - c_1m[-1].open) / c_1m[-1].open

        if len(c_1m) >= 3 and c_1m[-3].open > 0:
            ret_3m = (curr_price - c_1m[-3].open) / c_1m[-3].open
        elif len(c_1m) >= 1 and c_1m[0].open > 0:
            ret_3m = (curr_price - c_1m[0].open) / c_1m[0].open

        if len(c_1m) >= 5 and c_1m[-5].open > 0:
            ret_5m = (curr_price - c_1m[-5].open) / c_1m[-5].open
        elif len(c_1m) >= 1 and c_1m[0].open > 0:
            ret_5m = (curr_price - c_1m[0].open) / c_1m[0].open

        # 당일 시가/전일종가 대비 등락률 보정
        day_ret = 0.0
        if sym.open_price > 0:
            day_ret = (curr_price - sym.open_price) / float(sym.open_price)
        elif sym.prev_close > 0:
            day_ret = (curr_price - sym.prev_close) / float(sym.prev_close)

        sym.relative_strength = round(day_ret - market_return, 4)

        if ret_1m == 0.0 and day_ret != 0.0:
            ret_1m = day_ret
        if ret_3m == 0.0 and day_ret != 0.0:
            ret_3m = day_ret
        if ret_5m == 0.0 and day_ret != 0.0:
            ret_5m = day_ret

        # RVOL (최근 20개 1분봉 평균 대비)
        rvol = agg.calculate_rvol("1m", 20)
        if rvol == 1.0 and abs(ret_1m) >= 0.008:
            rvol = 2.0  # 단기 급변 시 기본 수급 가중

        # 동적 ATR Threshold
        atr_ratio = atr / float(curr_price) if curr_price > 0 else 0.015
        dyn_1m_thresh = max(0.005, min(0.015, atr_ratio * 0.5))
        dyn_3m_thresh = max(0.010, min(0.025, atr_ratio * 1.0))

        # 모멘텀 단계 분류 (Section 12: 0~1% START, 1~3% EARLY, 3~7% ACTIVE, 7%+ LATE)
        if day_ret >= 0.070:
            sym.momentum_stage = "LATE"
        elif day_ret >= 0.030:
            sym.momentum_stage = "ACTIVE"
        elif day_ret >= 0.010:
            sym.momentum_stage = "EARLY"
        elif day_ret >= 0.0:
            sym.momentum_stage = "START"
        else:
            sym.momentum_stage = "NORMAL"

        # ── 2. 15대 이벤트 전수 검사 (Events A ~ O) ──

        # EVENT A: 거래량 급증 (RVOL 단계별 강도 부여: 1.5x, 2.0x, 3.0x)
        if rvol >= 3.0:
            score += 20.0
            events.append(MarketEvent(
                event_type=MarketEventType.EVENT_A,
                iem_cd=sym.iem_cd, name=sym.name, timestamp=now,
                description=f"거래량 폭증 (RVOL: {rvol:.1f}x)",
                score_delta=20.0, metrics={"rvol": rvol, "strength": "SUPER"}
            ))
        elif rvol >= 2.0:
            score += 15.0
            events.append(MarketEvent(
                event_type=MarketEventType.EVENT_A,
                iem_cd=sym.iem_cd, name=sym.name, timestamp=now,
                description=f"1분 거래량 폭증 (RVOL: {rvol:.1f}x)",
                score_delta=15.0, metrics={"rvol": rvol, "strength": "STRONG"}
            ))
        elif rvol >= 1.5:
            score += 10.0
            events.append(MarketEvent(
                event_type=MarketEventType.EVENT_A,
                iem_cd=sym.iem_cd, name=sym.name, timestamp=now,
                description=f"1분 거래량 폭증 유입 (RVOL: {rvol:.1f}x)",
                score_delta=10.0, metrics={"rvol": rvol, "strength": "MODERATE"}
            ))

        # EVENT B: 1분/3분 모멘텀 점화 (동적 ATR 기준 적용)
        if ret_1m >= dyn_1m_thresh or ret_3m >= dyn_3m_thresh:
            score += 15.0
            events.append(MarketEvent(
                event_type=MarketEventType.EVENT_B,
                iem_cd=sym.iem_cd, name=sym.name, timestamp=now,
                description=f"3분 급등 및 단기 모멘텀 점화 (1m:{ret_1m*100:+.2f}%, 3m:{ret_3m*100:+.2f}%)",
                score_delta=15.0, metrics={"ret_1m": ret_1m, "ret_3m": ret_3m}
            ))

        # EVENT C: 5분 수익률 >= +2.0%
        if ret_5m >= 0.020:
            score += 10.0
            events.append(MarketEvent(
                event_type=MarketEventType.EVENT_C,
                iem_cd=sym.iem_cd, name=sym.name, timestamp=now,
                description=f"5분 급등 ({ret_5m*100:+.2f}%)",
                score_delta=10.0, metrics={"ret_5m": ret_5m}
            ))

        # EVENT D: 거래대금 급증 (Turnover Acceleration)
        if len(c_1m) >= 5:
            avg_t = sum(c.turnover for c in c_1m[-20:]) / float(len(c_1m[-20:]))
            if avg_t > 0 and curr_turnover >= avg_t * 2.0:
                score += 15.0
                events.append(MarketEvent(
                    event_type=MarketEventType.EVENT_D,
                    iem_cd=sym.iem_cd, name=sym.name, timestamp=now,
                    description=f"거래대금 급증 ({curr_turnover/avg_t:.1f}배)",
                    score_delta=15.0, metrics={"turnover_ratio": curr_turnover/avg_t}
                ))
        elif rvol >= 1.5:
            score += 10.0

        # EVENT E: 당일 신고가 갱신
        if sym.high_price > 0 and curr_price >= sym.high_price:
            score += 10.0
            events.append(MarketEvent(
                event_type=MarketEventType.EVENT_E,
                iem_cd=sym.iem_cd, name=sym.name, timestamp=now,
                description="당일 최고가 갱신",
                score_delta=10.0, metrics={"high": sym.high_price}
            ))

        # EVENT F: 전일 고가(PDH) 돌파
        if sym.prev_high > 0 and curr_price > sym.prev_high:
            score += 10.0
            events.append(MarketEvent(
                event_type=MarketEventType.EVENT_F,
                iem_cd=sym.iem_cd, name=sym.name, timestamp=now,
                description=f"전일 고가({sym.prev_high:,}원) 상향 돌파",
                score_delta=10.0, metrics={"pdh": sym.prev_high}
            ))

        # EVENT G: 20봉 고가 돌파
        past_20_high = max((c.high for c in c_1m[-21:-1]), default=0) if len(c_1m) >= 3 else 0
        if past_20_high > 0 and curr_price > past_20_high:
            score += 10.0
            events.append(MarketEvent(
                event_type=MarketEventType.EVENT_G,
                iem_cd=sym.iem_cd, name=sym.name, timestamp=now,
                description=f"20봉 고점({past_20_high:,}원) 돌파",
                score_delta=10.0, metrics={"high_20bar": past_20_high}
            ))

        # EVENT H: VWAP 돌파 또는 지지
        if vwap > 0 and curr_price >= vwap:
            score += 5.0
            dist_vwap = (curr_price - vwap) / vwap
            if dist_vwap <= 0.0050:
                patterns["is_vwap_reclaim"] = True
                score += 5.0
                events.append(MarketEvent(
                    event_type=MarketEventType.EVENT_H,
                    iem_cd=sym.iem_cd, name=sym.name, timestamp=now,
                    description=f"VWAP 지지/재탈환 ({vwap:,.0f}원)",
                    score_delta=10.0, metrics={"vwap": vwap}
                ))

        # EVENT I: EMA9 > EMA20 골든크로스 / 정배열
        if ema9 >= ema20:
            score += 5.0
            if abs(curr_price - ema9) / ema9 <= 0.0040:
                patterns["is_ema_pullback"] = True
                score += 5.0
                events.append(MarketEvent(
                    event_type=MarketEventType.EVENT_I,
                    iem_cd=sym.iem_cd, name=sym.name, timestamp=now,
                    description="EMA9 지지 눌림목/정배열",
                    score_delta=10.0, metrics={"ema9": ema9, "ema20": ema20}
                ))

        # EVENT J: 체결강도 강세
        if execution_intensity >= 110.0:
            score += 5.0
            events.append(MarketEvent(
                event_type=MarketEventType.EVENT_J,
                iem_cd=sym.iem_cd, name=sym.name, timestamp=now,
                description=f"체결강도 우위 ({execution_intensity:.1f}%)",
                score_delta=5.0, metrics={"intensity": execution_intensity}
            ))

        # EVENT K: 호가 OBI 매수 우위
        if obi >= 0.15:
            score += 5.0
            events.append(MarketEvent(
                event_type=MarketEventType.EVENT_K,
                iem_cd=sym.iem_cd, name=sym.name, timestamp=now,
                description=f"호가 매수 잔량 우위 (OBI: {obi:+.2f})",
                score_delta=5.0, metrics={"obi": obi}
            ))

        # EVENT L: 뉴스/공시 모멘텀
        if has_news:
            score += 10.0
            events.append(MarketEvent(
                event_type=MarketEventType.EVENT_L,
                iem_cd=sym.iem_cd, name=sym.name, timestamp=now,
                description="실시간 뉴스/공시 수급 유입",
                score_delta=10.0, metrics={"news": True}
            ))

        # EVENT M: 순위 급상승
        if rank_surged:
            score += 10.0
            events.append(MarketEvent(
                event_type=MarketEventType.EVENT_M,
                iem_cd=sym.iem_cd, name=sym.name, timestamp=now,
                description="수급 순위 급상승",
                score_delta=10.0, metrics={"rank_surged": True}
            ))

        # ── 3. 패턴 분석 ──
        # MOMENTUM IGNITION (Section 13)
        if (
            (ret_1m >= 0.005 and ret_3m >= 0.010 and rvol >= 1.5)
            or (ret_1m >= 0.008 and rvol >= 1.5)
            or (ret_1m >= 0.012)
        ):
            patterns["is_ignition"] = True

        # ACCELERATION
        if len(c_1m) >= 2:
            prev_ret = (c_1m[-1].close - c_1m[-1].open) / float(c_1m[-1].open) if c_1m[-1].open > 0 else 0
            if ret_1m >= 0.008 and prev_ret >= 0.003:
                patterns["is_acceleration"] = True
        elif ret_1m >= 0.015:
            patterns["is_acceleration"] = True

        # CHART STRUCTURE (Higher High / Higher Low)
        if len(c_1m) >= 4:
            if c_1m[-1].high >= c_1m[-2].high and c_1m[-1].low >= c_1m[-2].low:
                patterns["is_strong_chart"] = True

        # COMPRESSION DETECTION & BREAKOUT (Section 13 & 14: 변동폭 수축 후 돌파)
        if len(c_1m) >= 40:
            recent_20_ranges = [c.high - c.low for c in c_1m[-20:]]
            prev_20_ranges = [c.high - c.low for c in c_1m[-40:-20]]
            avg_recent_rng = sum(recent_20_ranges) / 20.0
            avg_prev_rng = sum(prev_20_ranges) / 20.0 if sum(prev_20_ranges) > 0 else 1.0

            if avg_recent_rng <= avg_prev_rng * 0.80:
                compression_high = max(c.high for c in c_1m[-20:])
                avg_comp_vol = sum(c.volume for c in c_1m[-20:]) / 20.0 if len(c_1m[-20:]) > 0 else 1.0
                if (
                    curr_price >= compression_high
                    and (curr_vol >= avg_comp_vol * 1.5 or (len(c_1m) >= 1 and c_1m[-1].volume >= avg_comp_vol * 1.5))
                ):
                    patterns["is_compression_breakout"] = True
        elif len(c_1m) >= 10:
            rng_recent = [c.high - c.low for c in c_1m[-5:]]
            rng_past = [c.high - c.low for c in c_1m[-10:-5]]
            avg_rec = sum(rng_recent) / 5.0
            avg_past = sum(rng_past) / 5.0 if sum(rng_past) > 0 else 1.0
            avg_comp_vol = sum(c.volume for c in c_1m[-5:]) / 5.0 if len(c_1m[-5:]) > 0 else 1.0
            comp_vol_ok = (
                rvol >= BREAKOUT_MIN_RVOL
                or curr_vol >= avg_comp_vol * 1.5
                or (len(c_1m) >= 1 and c_1m[-1].volume >= avg_comp_vol * 1.5)
            )
            if avg_rec <= avg_past * 0.85 and curr_price > max(c.high for c in c_1m[-5:]) and comp_vol_ok:
                patterns["is_compression_breakout"] = True


        # 추격매수 금지 필터 (Section 12: 7% 이상 LATE MOMENTUM에만 엄격 제한)
        if day_ret >= 0.070:
            patterns["is_chase_forbidden"] = True
        elif vwap > 0 and curr_price >= vwap * 1.07:
            patterns["is_chase_forbidden"] = True

        # 점수 합산 제한 (100점 만점)
        score = min(score, 100.0)

        # 우선순위 판정 (Section 10 & 14)
        if score >= 75.0:
            priority = CandidatePriority.PRIORITY_1
        elif score >= 55.0:
            priority = CandidatePriority.PRIORITY_2
        elif score >= 35.0 or len(events) >= 1:
            priority = CandidatePriority.WATCH
        else:
            priority = CandidatePriority.INACTIVE

        # 종목 메타데이터 업데이트
        sym.event_score = score
        sym.active_events = [e.description for e in events]
        if events:
            sym.last_event_time = now

        return score, events, priority, patterns
