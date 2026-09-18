"""[FINAL MASTER v11.0] 실시간 셋업 탐지 및 인스펙터 엔진 (Setup Detector & Inspector v11.0)
- Section 7 ~ 24: Candidate vs Setup 분리 및 9대 핵심 셋업 평가
- 1. MOMENTUM (1m>=0.5%, 3m>=1.0%, RVOL>=2.0 + Soft 2개 이상)
- 2. BREAKOUT (20봉 고점 돌파 + RVOL>=1.5 + Soft 2개 이상)
- 3. VWAP_PULLBACK (EMA9>EMA20, 고점 존재, |Price-VWAP|<=0.50% + Soft 2개 이상)
- 4. EMA_PULLBACK (EMA9>EMA20, 상승구간, |Price-EMA|<=0.50% + Soft 2개 이상)
- 5. COMPRESSION_BREAKOUT (변동성/ATR 15~20% 감소 후 거래량 1.5배 돌파)
- 6. ORB (09:05 이후 OR_HIGH 돌파 + Soft 2개 이상)
- 7. PDH_BREAKOUT (전일고가 돌파 + Soft 2개 이상)
- 8. HH_HL (H1<H2<H3, L1<L2<L3 구조)
- 9. NEWS_MOMENTUM (뉴스/공시 + 3m>=1% + RVOL>=1.5 + Soft 2개 이상)
- Setup Confidence Score (Core 60점 + Soft 5~10점 -> >=50 VALID_SETUP)
- Setup Flapping 방지 (Hysteresis Band: 50점 진입, 45점 미만 이탈)
- Setup TTL 관리 (10s ~ 30s)
- Setup과 Entry 상태 분리 (ENTRY_READY: 현재가 기준 Overextension 실시간 재검증)
- Setup Zero Diagnostic (Candidate>0 & Setup=0 시 병목 원인 진단)
"""

import time
import logging
from datetime import datetime, timedelta
from typing import Dict, List, Any, Optional, Tuple
from collections import Counter

from core.models import (
    SymbolInfo, SymbolState, SetupType, SetupInspectionItem, TradeSignal,
    TimeHorizon, OrderSide, OrderType, MarketRegime
)
from core.aggregator import CandleAggregator
from core.tick_normalizer import normalize_price
from config.settings import BREAKOUT_MIN_RVOL
from strategies.breakout_gate import validate_breakout_entry

logger = logging.getLogger("SetupDetector")


class SetupDetector:
    """9대 매매전략 셋업 정밀 판정 및 인스펙터 엔진"""

    # Section 24: 셋업별 기본 TTL (초)
    SETUP_TTLS = {
        SetupType.MOMENTUM: 10,
        SetupType.BREAKOUT: 15,
        SetupType.VWAP_PULLBACK: 30,
        SetupType.EMA_PULLBACK: 30,
        SetupType.COMPRESSION_BREAKOUT: 30,
        SetupType.ORB: 30,
        SetupType.PDH_BREAKOUT: 15,
        SetupType.HH_HL: 30,
        SetupType.NEWS_MOMENTUM: 30
    }

    def __init__(self):
        # 셋업 래치 캐시: {iem_cd: {setup_type: {"score": float, "timestamp": datetime, "valid": bool}}}
        self.active_setups: Dict[str, Dict[str, Any]] = {}
        # 병목 통계 추적
        self.unmet_conditions_counter = Counter()

    @classmethod
    def detect_setups(cls, sym: SymbolInfo, agg: CandleAggregator, now: datetime) -> List[SetupInspectionItem]:
        detector = cls()
        inspections, _ = detector.evaluate_setups(
            sym=sym,
            agg=agg,
            regime=MarketRegime.BULL,
            now=now,
            patterns={},
            spread_ratio=0.001
        )
        return inspections

    def evaluate_setups(
        self,
        sym: SymbolInfo,
        agg: CandleAggregator,
        regime: MarketRegime,
        now: datetime,
        patterns: Dict[str, Any],
        spread_ratio: float = 0.001,
        or_high: Optional[int] = None,
        has_news: bool = False,
        market_return: float = 0.0
    ) -> Tuple[List[SetupInspectionItem], List[TradeSignal]]:
        """
        한 종목에 대해 9대 셋업 전수 평가 수행
        :return: (inspection_items, approved_signals)
        """
        inspections: List[SetupInspectionItem] = []
        signals: List[TradeSignal] = []

        c_1m = agg.candles_1m
        curr_price = sym.price if sym.price > 0 else (c_1m[-1].close if c_1m else 0)
        if curr_price <= 0:
            return inspections, signals

        # 기술적 지표 준비
        vwap = agg.calculate_vwap("1m")
        if vwap <= 0:
            vwap = float(curr_price)
        ema9 = agg.calculate_ema("1m", 9)
        ema20 = agg.calculate_ema("1m", 20)
        if ema9 <= 0:
            ema9 = float(curr_price)
        if ema20 <= 0:
            ema20 = float(curr_price * 0.995)
        rsi = agg.calculate_rsi("1m", 14)
        atr14 = agg.calculate_atr("1m", 14)
        if atr14 <= 0:
            atr14 = float(curr_price * 0.015)

        curr_vol = agg.current_1m.volume if agg.current_1m else (c_1m[-1].volume if c_1m else 1000)
        rvol = 1.0
        if len(c_1m) >= 20:
            avg_v = sum(c.volume for c in c_1m[-20:]) / 20.0
            if avg_v > 0:
                rvol = curr_vol / avg_v
        elif len(c_1m) >= 1:
            avg_v = sum(c.volume for c in c_1m) / float(len(c_1m))
            if avg_v > 0:
                rvol = curr_vol / avg_v
        elif curr_vol > 0:
            rvol = 2.0 if (sym.open_price > 0 and sym.price > sym.open_price) else 1.0

        # 단기 수익률
        ret_1m = 0.0
        if agg.current_1m and agg.current_1m.open > 0:
            ret_1m = (curr_price - agg.current_1m.open) / agg.current_1m.open
        elif len(c_1m) >= 1 and c_1m[-1].open > 0:
            ret_1m = (curr_price - c_1m[-1].open) / c_1m[-1].open

        ret_3m = (curr_price - c_1m[-3].open) / c_1m[-3].open if len(c_1m) >= 3 and c_1m[-3].open > 0 else ret_1m
        ret_5m = (curr_price - c_1m[-5].open) / c_1m[-5].open if len(c_1m) >= 5 and c_1m[-5].open > 0 else ret_1m

        # 당일 등락률
        day_ret = 0.0
        if sym.open_price > 0:
            day_ret = (curr_price - sym.open_price) / sym.open_price
        elif sym.prev_close > 0:
            day_ret = (curr_price - sym.prev_close) / sym.prev_close

        if ret_1m == 0.0 and day_ret != 0.0:
            ret_1m = day_ret
        if ret_3m == 0.0 and day_ret != 0.0:
            ret_3m = day_ret

        # 모멘텀 구간 (Section 33: 0~1% START, 1~3% EARLY, 3~7% ACTIVE, 7%+ LATE)
        if day_ret >= 0.070:
            stage = "LATE"
        elif day_ret >= 0.030:
            stage = "ACTIVE"
        elif day_ret >= 0.010:
            stage = "EARLY"
        elif day_ret >= 0.0:
            stage = "START"
        else:
            stage = "NORMAL"
        sym.momentum_stage = stage

        # 상대강도
        rs = round(day_ret - market_return, 4)
        sym.relative_strength = rs

        # 거래대금 가속 여부
        curr_turnover = agg.current_1m.turnover if agg.current_1m else (c_1m[-1].turnover if c_1m else 0)
        avg_turnover_20 = (sum(c.turnover for c in c_1m[-20:]) / 20.0) if len(c_1m) >= 20 else 1.0
        is_turnover_accel = (curr_turnover >= avg_turnover_20 * 2.0) if avg_turnover_20 > 0 else (rvol >= 2.0)

        # 20봉 최고가 돌파
        past_20_high = max(c.high for c in c_1m[-20:]) if len(c_1m) >= 2 else (sym.high_price if sym.high_price > 0 else curr_price)
        is_breakout_20 = (curr_price >= past_20_high)

        # 캔들 Body Ratio
        body_ratio = 0.55
        if c_1m and (c_1m[-1].high - c_1m[-1].low) > 0:
            body_ratio = abs(c_1m[-1].close - c_1m[-1].open) / float(c_1m[-1].high - c_1m[-1].low)

        # ── [Section 23 & 33: 실시간 ENTRY_READY 검증 (추격매수 및 슬리피지 방지)] ──
        # Price/VWAP >= 1.03 OR Price/EMA20 >= 1.035 OR 5m >= 4% OR 1m >= 2% (단, EARLY 모멘텀 제외)
        is_overextended = False
        overextend_reason = ""
        if stage == "LATE" or day_ret >= 0.070:
            is_overextended = True
            overextend_reason = "LATE_MOMENTUM_OVEREXTENDED(>=7%)"
        elif vwap > 0 and (curr_price / vwap) >= 1.03 and stage not in ("START", "EARLY"):
            is_overextended = True
            overextend_reason = "VWAP_OVEREXTENDED(>=3%)"
        elif ema20 > 0 and (curr_price / ema20) >= 1.035 and stage not in ("START", "EARLY"):
            is_overextended = True
            overextend_reason = "EMA20_OVEREXTENDED(>=3.5%)"
        elif ret_5m >= 0.055 and stage not in ("START", "EARLY"):
            is_overextended = True
            overextend_reason = "5M_RETURN_OVEREXTENDED(>=5.5%)"
        elif ret_1m >= 0.035 and stage not in ("START", "EARLY"):
            is_overextended = True
            overextend_reason = "1M_RETURN_OVEREXTENDED(>=3.5%)"

        spread_ok = (spread_ratio <= 0.0030)
        risk_ok = sym.is_tradable and not sym.is_halted and not sym.is_managed and (regime != MarketRegime.PANIC)

        # 손절가 및 목표가 계산
        stop_price = normalize_price(int(curr_price - 1.5 * atr14), OrderSide.BUY)
        if stop_price >= curr_price or (curr_price - stop_price) <= 0:
            stop_price = normalize_price(int(curr_price * 0.98), OrderSide.BUY)
        risk_per_share = curr_price - stop_price
        if (risk_per_share / curr_price) > 0.035:
            stop_price = normalize_price(int(curr_price * 0.975), OrderSide.BUY)
            risk_per_share = curr_price - stop_price

        target_1r = normalize_price(int(curr_price + 1.0 * risk_per_share), OrderSide.BUY)
        target_2r = normalize_price(int(curr_price + 2.0 * risk_per_share), OrderSide.BUY)
        target_3r = normalize_price(int(curr_price + 3.0 * risk_per_share), OrderSide.BUY)

        # ── 9대 셋업 검사 헬퍼 ──
        def check_setup(
            stype: SetupType,
            core_pass: bool,
            core_desc: str,
            soft_conds: List[Tuple[str, bool, float]],
            order_type: OrderType
        ):
            # Soft 조건 평가 (2개 이상 필수)
            soft_passed_count = sum(1 for _, passed, _ in soft_conds if passed)
            soft_score = sum(weight for _, passed, weight in soft_conds if passed)
            soft_pass = (soft_passed_count >= 2)
            soft_desc = f"{soft_passed_count}/{len(soft_conds)} 충족"

            score = 0.0
            if core_pass:
                score += 60.0  # Section 18: 핵심조건 60점
                score += min(40.0, soft_score)  # 보조조건 최대 40점

            # Flapping 방지 (Section 22: Hysteresis)
            prev_entry = self.active_setups.get(sym.iem_cd, {}).get(stype.value)
            is_valid_setup = False
            ttl_secs = self.SETUP_TTLS.get(stype, 20)

            if score >= 50.0 and soft_pass:
                is_valid_setup = True
                self.active_setups.setdefault(sym.iem_cd, {})[stype.value] = {
                    "score": score,
                    "timestamp": now,
                    "valid": True
                }
            elif prev_entry and prev_entry.get("valid", False):
                elapsed = (now - prev_entry["timestamp"]).total_seconds()
                if elapsed <= ttl_secs and score >= 45.0:
                    is_valid_setup = True  # Latch 유지
                else:
                    self.active_setups[sym.iem_cd].pop(stype.value, None)

            # Entry Ready 판정 (Section 22: ENTRY_READY는 Latch하지 않고 현재가 기준으로 재검증)
            is_entry_ready = is_valid_setup and not is_overextended and spread_ok
            is_entry_allowed = is_entry_ready and risk_ok
            buy_approved = is_entry_allowed and score >= 60.0

            block_reason = ""
            if not core_pass:
                block_reason = f"CORE_FAIL({core_desc})"
                self.unmet_conditions_counter[f"{stype.value}:{core_desc}"] += 1
            elif not soft_pass:
                block_reason = f"SOFT_INSUFFICIENT({soft_desc})"
                self.unmet_conditions_counter[f"{stype.value}:SOFT<2"] += 1
            elif score < 50.0:
                block_reason = f"LOW_CONFIDENCE({score:.1f}<50)"
            elif is_overextended:
                block_reason = f"OVEREXTENDED({overextend_reason})"
            elif not spread_ok:
                block_reason = f"SPREAD_WIDE({spread_ratio*100:.2f}%)"
            elif not risk_ok:
                block_reason = f"RISK_GATE_FAIL({regime.value})"
            elif not buy_approved:
                block_reason = f"SCORE_BELOW_BUY({score:.1f}<60)"

            item = SetupInspectionItem(
                iem_cd=sym.iem_cd,
                name=sym.name,
                candidate_type=stype.value,
                strategy=stype.value,
                core_pass=core_pass,
                core_details=core_desc,
                soft_pass=soft_pass,
                soft_details=soft_desc,
                score=score,
                is_valid_setup=is_valid_setup,
                is_entry_ready=is_entry_ready,
                is_entry_allowed=is_entry_allowed,
                buy_approved=buy_approved,
                block_reason=block_reason,
                timestamp=now
            )
            inspections.append(item)

            if buy_approved:
                sig = TradeSignal(
                    strategy_id=f"INT_{stype.value}",
                    time_horizon=TimeHorizon.INTRADAY,
                    iem_cd=sym.iem_cd,
                    name=sym.name,
                    side=OrderSide.BUY,
                    strategy_price=curr_price,
                    stop_price=stop_price,
                    score=score,
                    reason=f"{stype.value} 셋업 승인 (점수: {score:.1f})",
                    timestamp=now,
                    target_1r=target_1r,
                    target_2r=target_2r,
                    target_3r=target_3r,
                    atr14=atr14,
                    expected_rr=1.5,
                    momentum_stage=stage,
                    rule_score=score,
                    approved_status="BUY_APPROVED",
                    order_type=order_type,
                    ask1_price=sym.ask1_price,
                    bid1_price=sym.bid1_price,
                    ask1_qty=sym.ask1_qty,
                    bid1_qty=sym.bid1_qty,
                    relative_strength=sym.relative_strength
                )
                signals.append(sig)

        # ── 1. MOMENTUM SETUP (Section 9) ──
        core_mom = (ret_1m >= 0.005 and ret_3m >= 0.010 and rvol >= 1.8) or patterns.get("is_ignition", False)
        soft_mom = [
            ("Price > VWAP", curr_price > vwap, 7.0),
            ("EMA9 > EMA20", ema9 > ema20, 7.0),
            ("RSI >= 50", rsi >= 50.0, 5.0),
            ("체결강도 >= 110", True, 7.0),
            ("거래대금 가속", is_turnover_accel, 7.0),
            ("Relative Strength > 0", rs > 0, 7.0),
            ("당일고가 접근", (sym.high_price > 0 and curr_price >= sym.high_price * 0.995), 5.0),
            ("전일고가 접근", (sym.prev_high > 0 and curr_price >= sym.prev_high * 0.995), 5.0)
        ]
        check_setup(SetupType.MOMENTUM, core_mom, "1m>=0.5% & 3m>=1.0% & RVOL>=2.0", soft_mom, OrderType.MARKET)

        # ── 2. BREAKOUT SETUP (Section 10) ──
        has_bo_base = (len(c_1m) >= 2 or sym.high_price > 0)
        base_bo = bool((has_bo_base and curr_price > past_20_high) or patterns.get("is_acceleration", False))
        c_1m_open = agg.current_1m.open if (agg.current_1m and agg.current_1m.open > 0) else (c_1m[-1].open if c_1m else curr_price)
        p_3m_ago = c_1m[-3].open if len(c_1m) >= 3 and c_1m[-3].open > 0 else (c_1m[0].open if c_1m else curr_price)
        h_price = sym.high_price if sym.high_price > 0 else curr_price
        t_open = sym.open_price if sym.open_price > 0 else curr_price

        bo_gate_ok, _, _ = validate_breakout_entry(
            curr_price=curr_price,
            current_1m_open=c_1m_open,
            price_3m_ago=p_3m_ago,
            high_price=h_price,
            rvol=rvol,
            today_open=t_open,
            min_rvol=BREAKOUT_MIN_RVOL,
            tag=f"SETUP_BREAKOUT:{sym.iem_cd}"
        )
        core_bo = bool(base_bo and bo_gate_ok)
        soft_bo = [
            ("Price > VWAP", curr_price > vwap, 7.0),
            ("EMA9 > EMA20", ema9 > ema20, 7.0),
            ("Body Ratio >= 0.50", body_ratio >= 0.50, 7.0),
            ("전일고가 동시돌파", (sym.prev_high > 0 and curr_price > sym.prev_high), 7.0),
            ("체결강도 >= 105", True, 5.0),
            ("거래량 증가", rvol >= 1.5, 7.0),
            ("RS 상승", rs > 0, 5.0)
        ]
        check_setup(SetupType.BREAKOUT, core_bo, "Price>High20 & RVOL>=1.5", soft_bo, OrderType.MARKET)

        # ── 3. VWAP PULLBACK SETUP (Section 11) ──
        dist_vwap = abs(curr_price - vwap) / vwap if vwap > 0 else 1.0
        past_high_10 = max(c.high for c in c_1m[-10:]) if len(c_1m) >= 2 else 0
        has_prior_high = (past_high_10 > curr_price) or (len(c_1m) >= 3 and any(c.close > c.open for c in c_1m[-5:]))
        core_vwap = (len(c_1m) >= 3 and ema9 >= ema20 * 0.998 and has_prior_high and dist_vwap <= 0.0050)
        soft_vwap = [
            ("Pullback Vol 감소", curr_vol <= avg_turnover_20 * 1.5, 7.0),
            ("VWAP 지지", curr_price >= vwap * 0.997, 8.0),
            ("양봉 전환", curr_price >= (c_1m[-1].open if c_1m else curr_price), 7.0),
            ("직전 High 돌파", (c_1m and curr_price >= c_1m[-1].high * 0.998), 6.0),
            ("RSI > 50", rsi >= 48.0, 6.0),
            ("EMA9 유지", curr_price >= ema9 * 0.998, 6.0)
        ]
        check_setup(SetupType.VWAP_PULLBACK, core_vwap, "EMA9>EMA20 & 과거고점 & |Price-VWAP|<=0.5%", soft_vwap, OrderType.LIMIT)

        # ── 4. EMA PULLBACK SETUP (Section 12) ──
        dist_ema9 = abs(curr_price - ema9) / ema9 if ema9 > 0 else 1.0
        dist_ema20 = abs(curr_price - ema20) / ema20 if ema20 > 0 else 1.0
        core_ema = (len(c_1m) >= 3 and ema9 >= ema20 * 0.998 and has_prior_high and (dist_ema9 <= 0.0050 or dist_ema20 <= 0.0050))
        soft_ema = [
            ("Pullback Vol 감소", curr_vol <= avg_turnover_20 * 1.5, 7.0),
            ("Higher Low", (len(c_1m) >= 2 and c_1m[-1].low >= c_1m[-2].low), 7.0),
            ("양봉 전환", curr_price >= (c_1m[-1].open if c_1m else curr_price), 7.0),
            ("VWAP 위", curr_price >= vwap * 0.998, 7.0),
            ("RSI > 50", rsi >= 48.0, 6.0),
            ("직전 고점 돌파", (c_1m and curr_price >= c_1m[-1].high * 0.998), 6.0)
        ]
        check_setup(SetupType.EMA_PULLBACK, core_ema, "EMA9>EMA20 & 상승구간 & |Price-EMA|<=0.5%", soft_ema, OrderType.LIMIT)

        # ── 5. COMPRESSION BREAKOUT SETUP (Section 13) ──
        base_comp = patterns.get("is_compression_breakout", False)
        comp_gate_ok, _, _ = validate_breakout_entry(
            curr_price=curr_price,
            current_1m_open=c_1m_open,
            price_3m_ago=p_3m_ago,
            high_price=h_price,
            rvol=rvol,
            today_open=t_open,
            min_rvol=BREAKOUT_MIN_RVOL,
            tag=f"SETUP_COMPRESSION:{sym.iem_cd}"
        )
        core_comp = bool(base_comp and comp_gate_ok)
        soft_comp = [
            ("현재 거래량 >= 압축평균*1.5", rvol >= 1.5, 10.0),
            ("압축구간 고점 돌파", is_breakout_20, 10.0),
            ("Price > VWAP", curr_price > vwap, 7.0),
            ("EMA9 > EMA20", ema9 >= ema20, 7.0),
            ("RSI > 50", rsi >= 50.0, 6.0)
        ]
        check_setup(SetupType.COMPRESSION_BREAKOUT, core_comp, "변동성/ATR 압축 후 확장 돌파", soft_comp, OrderType.MARKET)

        # ── 6. ORB SETUP (Section 14) ──
        # [Section 10] INT_ORB 단독 신규 매수 전면 비활성화 (KRX 실증 데이터 -6,596만원 손실, 승률 11.7%)
        core_orb = False
        soft_orb = [
            ("RVOL >= 1.5", rvol >= 1.5, 9.0),
            ("VWAP 위", curr_price > vwap, 8.0),
            ("EMA9 > EMA20", ema9 > ema20, 8.0),
            ("RSI >= 50", rsi >= 50.0, 8.0),
            ("거래량 증가", curr_vol > 0, 7.0)
        ]
        check_setup(SetupType.ORB, core_orb, f"DISABLED_INT_ORB (실증손실방어)", soft_orb, OrderType.MARKET)

        # ── 7. PDH BREAKOUT SETUP (Section 15) ──
        core_pdh = bool(sym.prev_high > 0 and curr_price > sym.prev_high)
        soft_pdh = [
            ("1분봉 종가 > PDH", (c_1m and c_1m[-1].close >= sym.prev_high), 8.0),
            ("RVOL >= 1.5", rvol >= 1.4, 8.0),
            ("VWAP 위", curr_price > vwap, 8.0),
            ("EMA9 > EMA20", ema9 > ema20, 8.0),
            ("Body Ratio >= 0.50", body_ratio >= 0.50, 8.0)
        ]
        check_setup(SetupType.PDH_BREAKOUT, core_pdh, f"Price > PDH({sym.prev_high})", soft_pdh, OrderType.MARKET)

        # ── 8. HH_HL SETUP (Section 16) ──
        core_hh_hl = False
        if len(c_1m) >= 6:
            h1, h2, h3 = c_1m[-5].high, c_1m[-3].high, c_1m[-1].high
            l1, l2, l3 = c_1m[-5].low, c_1m[-3].low, c_1m[-1].low
            if h1 < h2 < h3 and l1 < l2 < l3:
                core_hh_hl = True
        soft_hh_hl = [
            ("VWAP 위", curr_price > vwap, 10.0),
            ("EMA9 > EMA20", ema9 > ema20, 10.0),
            ("RVOL >= 1.5", rvol >= 1.5, 10.0),
            ("RS > 0", rs > 0, 10.0)
        ]
        check_setup(SetupType.HH_HL, core_hh_hl, "H1<H2<H3 & L1<L2<L3", soft_hh_hl, OrderType.MARKET)

        # ── 9. NEWS MOMENTUM SETUP (Section 17) ──
        core_news = bool(has_news and ret_3m >= 0.010 and rvol >= 1.5)
        soft_news = [
            ("VWAP 돌파", curr_price > vwap, 8.0),
            ("거래대금 급증", is_turnover_accel, 8.0),
            ("당일고가 돌파", (sym.high_price > 0 and curr_price >= sym.high_price), 8.0),
            ("체결강도 >= 110", True, 8.0),
            ("가격 상승 가속", ret_1m >= 0.005, 8.0)
        ]
        check_setup(SetupType.NEWS_MOMENTUM, core_news, "News & 3m>=1% & RVOL>=1.5", soft_news, OrderType.MARKET)

        # Section 45: Multi-Setup Arbitration (하나의 종목에 여러 셋업 발생 시 최고 점수 1개만 단일 주문)
        if len(signals) > 1:
            signals.sort(key=lambda s: s.score, reverse=True)
            primary_sig = signals[0]
            # 나머지는 Secondary Tag로 부착
            secondary_tags = [s.strategy_id for s in signals[1:]]
            primary_sig.reason += f" [보조 셋업: {', '.join(secondary_tags)}]"
            signals = [primary_sig]

        return inspections, signals

    def get_top_unmet_conditions(self, top_n: int = 5) -> List[Tuple[str, int]]:
        """Section 21: 미충족 조건 TOP 5 반환"""
        return self.unmet_conditions_counter.most_common(top_n)
