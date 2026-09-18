"""통합 전략 스위트 (Full Strategy Suite v6.0)
- Execution-Grade Specification v6.0 (Section 11 ~ 25)
- 단타 11대 전략 (Momentum Ignition, Acceleration, ORB, PDH, Day High, 20-Bar High,
                 VWAP Pullback, EMA Pullback, Compression Breakout, HH/HL Structure, News Momentum)
- 스윙 9대 전략 (Trend Align, HH20, HH60, MA20 Pullback, MA60 Pullback, Box Breakout,
                Volume Expansion, Weekly Trend, Catalyst Breakout)
- 추격매수 금지 (Chase Prevention: Section 55) & Fake Breakout 방어 (Section 56)
"""

import logging
from datetime import datetime, time as dtime
from typing import Dict, List, Any, Optional
from core.models import (
    TradeSignal, TimeHorizon, OrderSide, OrderType, MarketRegime, Candle, SymbolInfo
)
from core.aggregator import CandleAggregator
from core.tick_normalizer import normalize_price
from config.settings import BREAKOUT_MIN_RVOL
from strategies.breakout_gate import validate_breakout_entry

logger = logging.getLogger("FullStrategySuite")



class FullStrategySuite:
    """단타 11대전략 + 스윙 9대전략 통합 평가 엔진"""

    # =========================================================================
    # [1] 단타 전략 평가 (Section 4 ~ 12: 3-Tier Gating & Strategies A ~ F)
    # =========================================================================
    @classmethod
    def evaluate_intraday_all(
        cls,
        sym: SymbolInfo,
        agg: CandleAggregator,
        regime: MarketRegime,
        now: datetime,
        patterns: Dict[str, Any],
        spread_ratio: float = 0.001,
        or_high: Optional[int] = None,
        has_news: bool = False,
        market_return: float = 0.0
    ) -> List[TradeSignal]:
        """
        ACTIVE 승격 종목 대상 3단계 게이팅(Hard Gate -> Setup -> Soft Score) 및 전략 A~F 평가
        """
        from strategies.scoring_engine import ScoringEngine

        signals: List[TradeSignal] = []
        sym.buy_block_reasons = []

        # ── [A. HARD GATE: 필수 최소 안전 조건 (Section 4 & 8)] ──
        if not sym.is_tradable or sym.is_halted:
            sym.buy_block_reasons.append("NOT_TRADABLE")
            return []
        if sym.is_managed:
            sym.buy_block_reasons.append("MANAGED_STOCK")
            return []
        if regime == MarketRegime.PANIC:
            sym.buy_block_reasons.append("MARKET_PANIC")
            return []
        if spread_ratio > 0.0030:
            sym.buy_block_reasons.append("SPREAD_TOO_WIDE")
            return []
        if patterns.get("is_chase_forbidden", False):
            sym.buy_block_reasons.append("CHASE_RESTRICTED_LATE_MOMENTUM")
            return []

        c_1m = agg.candles_1m
        curr_price = sym.price if sym.price > 0 else (c_1m[-1].close if c_1m else 0)
        if curr_price <= 0:
            sym.buy_block_reasons.append("INVALID_PRICE")
            return []

        # ATR 및 손절가 계산 (데이터 부족 시 당일 호가 진폭 또는 1.5% 보정)
        atr14 = agg.calculate_atr("1m", 14)
        if atr14 <= 0:
            if sym.high_price > sym.low_price and sym.low_price > 0:
                atr14 = float(sym.high_price - sym.low_price)
            elif agg.current_1m and (agg.current_1m.high - agg.current_1m.low) > 0:
                atr14 = float(agg.current_1m.high - agg.current_1m.low)
            else:
                atr14 = float(curr_price * 0.015)

        stop_price = normalize_price(int(curr_price - 1.5 * atr14), OrderSide.BUY)
        if stop_price >= curr_price or (curr_price - stop_price) <= 0:
            stop_price = normalize_price(int(curr_price * 0.98), OrderSide.BUY)

        risk_per_share = curr_price - stop_price
        if risk_per_share <= 0:
            sym.buy_block_reasons.append("NO_STOP")
            return []

        # 손절폭 보정 (최대 3.5% 상한 - LIVE와 동일)
        if (risk_per_share / curr_price) > 0.035:
            stop_price = normalize_price(int(curr_price * 0.975), OrderSide.BUY)
            risk_per_share = curr_price - stop_price

        target_1r = normalize_price(int(curr_price + 1.0 * risk_per_share), OrderSide.BUY)
        target_2r = normalize_price(int(curr_price + 2.0 * risk_per_share), OrderSide.BUY)
        target_3r = normalize_price(int(curr_price + 3.0 * risk_per_share), OrderSide.BUY)

        # ── 지표 산출 ──
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

        curr_vol = agg.current_1m.volume if agg.current_1m else (c_1m[-1].volume if c_1m else 1000)
        rvol = 1.0
        if len(c_1m) >= 20:
            avg_vol = sum(c.volume for c in c_1m[-20:]) / 20.0
            if avg_vol > 0:
                rvol = curr_vol / avg_vol
        elif len(c_1m) >= 1:
            avg_vol = sum(c.volume for c in c_1m) / float(len(c_1m))
            if avg_vol > 0:
                rvol = curr_vol / avg_vol
        elif curr_vol > 0:
            rvol = 2.0 if (sym.price > (sym.open_price or sym.prev_close or 0)) else 1.0

        # 단기 수익률 및 기준 가격 산출
        ret_1m = 0.0
        c_1m_open = curr_price
        if agg.current_1m and agg.current_1m.open > 0:
            ret_1m = (curr_price - agg.current_1m.open) / agg.current_1m.open
            c_1m_open = agg.current_1m.open
        elif len(c_1m) >= 1 and c_1m[-1].open > 0:
            ret_1m = (curr_price - c_1m[-1].open) / c_1m[-1].open
            c_1m_open = c_1m[-1].open

        p_3m_ago = c_1m[-3].open if len(c_1m) >= 3 and c_1m[-3].open > 0 else (c_1m[0].open if c_1m and c_1m[0].open > 0 else None)
        ret_3m = (curr_price - p_3m_ago) / p_3m_ago if p_3m_ago else ret_1m


        # 당일 시가/전일대비 등락률 보정
        day_ret = 0.0
        if sym.open_price > 0:
            day_ret = (curr_price - sym.open_price) / sym.open_price
        elif sym.prev_close > 0:
            day_ret = (curr_price - sym.prev_close) / sym.prev_close

        # 상대강도 (RS) 산출
        sym.relative_strength = round(day_ret - market_return, 4)

        # 거래대금 급증 배수
        turnover_ratio = 1.0
        curr_turnover = agg.current_1m.turnover if agg.current_1m else (c_1m[-1].turnover if c_1m else 0)
        if len(c_1m) >= 20:
            avg_t20 = sum(c.turnover for c in c_1m[-20:]) / 20.0
            if avg_t20 > 0:
                turnover_ratio = curr_turnover / avg_t20
        elif rvol > 1.0:
            turnover_ratio = rvol

        past_20_high = max(c.high for c in c_1m[-20:]) if len(c_1m) >= 2 else (sym.high_price if sym.high_price > 0 else curr_price)
        is_breakout_20 = (curr_price >= past_20_high)

        if ret_1m == 0.0 and day_ret != 0.0:
            ret_1m = day_ret
        if ret_3m == 0.0 and day_ret != 0.0:
            ret_3m = day_ret

        # ── [데이터 검증 기반 안전 게이트 (Section 1 & 2)] ──
        # 1. 3분 모멘텀 음수(ret_3m < -0.003) 진입 차단 (실증 데이터: 승률 43.8%, 누적손실 -4,401만원 방어)
        if ret_3m < -0.003:
            sym.buy_block_reasons.append(f"NEGATIVE_3M_MOMENTUM ({ret_3m*100:.2f}%)")
            return []

        # 2. 저거래량(RVOL < 1.0) 진입 차단 (실증 데이터: 승률 32.0%, 누적손실 -635만원 방어)
        if rvol < 1.0:
            sym.buy_block_reasons.append(f"LOW_RVOL ({rvol:.2f} < 1.0)")
            return []

        # ── [모멘텀 구간 판정 (Section 12)] ──
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

        # ── [B. SETUP CONDITION & C. SOFT SCORE (Section 4 ~ 11)] ──
        matched_any_setup = False

        def evaluate_and_add(strat_id: str, reason: str, breakout_type: str, setup_ok: bool, order_type: OrderType, body_ratio: float = 0.55, entry_timing_valid: bool = True, timing_reason: str = ""):
            nonlocal matched_any_setup
            if not setup_ok:
                return False
            matched_any_setup = True

            score, grade = ScoringEngine.score_intraday(
                market_regime=regime,
                rvol=rvol,
                price_above_vwap=(curr_price > vwap),
                vwap_rising=(vwap > 0 and curr_price >= vwap),
                ema_aligned=(ema9 >= ema20),
                rsi=rsi,
                breakout_type=breakout_type,
                body_ratio=body_ratio,
                is_day_high=(sym.high_price > 0 and curr_price >= sym.high_price),
                is_pdh=(sym.prev_high > 0 and curr_price > sym.prev_high),
                relative_strength=sym.relative_strength,
                turnover_ratio=turnover_ratio,
                is_breakout_20=is_breakout_20
            )

            # LATE 모멘텀은 추격매수 페널티 (-10점)
            if stage == "LATE":
                score = max(0.0, score - 10.0)

            # 최소 60점 이상이면 BUY 신호 승인 (Section 4 & 6~11)
            if score >= 60.0:
                sig = TradeSignal(
                    strategy_id=strat_id,
                    time_horizon=TimeHorizon.INTRADAY,
                    iem_cd=sym.iem_cd,
                    name=sym.name,
                    side=OrderSide.BUY,
                    strategy_price=curr_price,
                    stop_price=stop_price,
                    score=score,
                    reason=f"{reason} (점수: {score:.1f}, {grade})",
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
                    relative_strength=sym.relative_strength,
                    rvol=round(float(rvol), 2),
                    entry_timing_valid=entry_timing_valid,
                    timing_reason=timing_reason or reason
                )
                signals.append(sig)
                return True
            else:
                sym.buy_block_reasons.append(f"LOW_SCORE ({score:.1f}<60)")
                return False

        # [전략 A: MOMENTUM IGNITION (Section 6)]
        # 1분 >= +0.5% and 3분 >= +1.0% and RVOL >= 1.8 (고점 대비 -1.5% 이내 유지 필수)
        near_high = (curr_price >= sym.high_price * 0.985) if sym.high_price > 0 else True
        setup_a = bool(
            (
                (ret_1m >= 0.005 and ret_3m >= 0.008 and rvol >= 1.8)
                or (patterns.get("is_ignition", False) and rvol >= 1.5)
                or (ret_1m >= 0.010 and rvol >= 2.0 and ret_3m >= 0.005)
            )
            and near_high
            and (ret_3m >= 0.0)
        )
        evaluate_and_add("INT_MOMENTUM_IGNITION", "모멘텀 점화 (1분/3분 급등 + 수급 폭발)", "MOMENTUM", setup_a, OrderType.MARKET, entry_timing_valid=setup_a, timing_reason="모멘텀점화_수급폭발")

        # [전략 B: BREAKOUT (Section 7)]
        # HIGH20 돌파 + RVOL >= 1.5 + 가격 상승
        past_20_high = max(c.high for c in c_1m[-20:]) if len(c_1m) >= 2 else (sym.high_price if sym.high_price > 0 else curr_price)
        base_bo = (curr_price >= past_20_high) or patterns.get("is_acceleration", False)
        valid_bo, bo_reason, bo_metrics = validate_breakout_entry(
            curr_price=curr_price,
            breakout_threshold_price=past_20_high if not patterns.get("is_acceleration", False) else curr_price,
            rvol=rvol,
            current_1m_open=c_1m_open,
            price_3m_ago=p_3m_ago,
            high_price=sym.high_price,
            min_rvol=BREAKOUT_MIN_RVOL,
            strategy_id="INT_BREAKOUT",
            symbol=sym.iem_cd,
            vwap=vwap
        )
        setup_b = bool(base_bo and valid_bo)
        timing_b_reason = "20봉신고가_돌파" if setup_b else bo_reason
        evaluate_and_add("INT_BREAKOUT", "20봉 신고가 상향 돌파", "BREAKOUT", setup_b, OrderType.MARKET, body_ratio=0.60, entry_timing_valid=setup_b, timing_reason=timing_b_reason)

        # [전략 C: VWAP PULLBACK (Section 8)]
        # 급등했던 종목이 VWAP까지 눌린 후 거래량을 동반하여 다시 반등하는 시점 진입
        past_high_recent = max(c.high for c in c_1m[-15:]) if len(c_1m) >= 1 else (sym.high_price if sym.high_price > 0 else curr_price)
        peak_high = max(past_high_recent, sym.high_price if sym.high_price > 0 else curr_price)
        # 1) Prior Momentum: 직전 고점 또는 당일 고가가 VWAP 대비 유의미하게 높았는가?
        prior_momentum = (peak_high >= vwap * 1.005) or (day_ret >= 0.010) or (ret_3m >= 0.008)
        # 2) VWAP Proximity: 현재가가 VWAP 지지권(0.8% 이내)에 위치
        dist_vwap = abs(curr_price - vwap) / vwap if vwap > 0 else 0.0
        vwap_prox = (curr_price >= vwap * 0.995 and dist_vwap <= 0.0080)
        # 3) Pullback Evidence: 고점 대비 최소 0.3% 이상 유의미한 눌림이 선행되었는가? (단순 횡보 정체주 배제)
        pullback_depth = (peak_high - curr_price) / peak_high if peak_high > 0 else 0.0
        pullback_evidence = (pullback_depth >= 0.0030)
        # 4) Volume Confirmation: 최소한의 수급/거래량 확인 (RVOL >= 1.2 또는 turnover_ratio >= 1.1)
        volume_conf = (rvol >= 1.2 or turnover_ratio >= 1.1)
        # 5) Rebound Confirmation: 양봉 또는 단기 반등세 확인
        is_green_rebound = (curr_price >= (agg.current_1m.open if agg.current_1m else (c_1m[-1].open if c_1m else curr_price))) or (ret_1m >= 0.0)
        rebound_conf = is_green_rebound and (ema9 >= ema20 or curr_price >= vwap)

        setup_c = bool(prior_momentum and vwap_prox and pullback_evidence and volume_conf and rebound_conf)
        timing_c_reason = "VWAP지지_눌림목_반등확인" if setup_c else "VWAP_조건미흡"
        evaluate_and_add("INT_VWAP_PULLBACK", "VWAP 지지 눌림목 반등", "PULLBACK", setup_c, OrderType.LIMIT, entry_timing_valid=setup_c, timing_reason=timing_c_reason)

        # [전략 D: COMPRESSION BREAKOUT (Section 9)]
        recent_5_high = max(c.high for c in c_1m[-5:]) if len(c_1m) >= 5 else curr_price
        is_comp = patterns.get("is_compression_breakout", False)
        if is_comp:
            valid_comp, comp_reason, comp_metrics = validate_breakout_entry(
                curr_price=curr_price,
                breakout_threshold_price=recent_5_high,
                rvol=rvol,
                current_1m_open=c_1m_open,
                price_3m_ago=p_3m_ago,
                high_price=sym.high_price,
                min_rvol=BREAKOUT_MIN_RVOL,
                strategy_id="INT_COMPRESSION_BREAKOUT",
                symbol=sym.iem_cd,
                vwap=vwap
            )
            evaluate_and_add("INT_COMPRESSION_BREAKOUT", "가격 압축 구간 상향 돌파", "COMPRESSION", valid_comp, OrderType.MARKET, entry_timing_valid=valid_comp, timing_reason=comp_reason)
        else:
            evaluate_and_add("INT_COMPRESSION_BREAKOUT", "가격 압축 구간 상향 돌파", "COMPRESSION", False, OrderType.MARKET)


        # [전략 E: OPENING RANGE BREAKOUT (ORB) (Section 10)]
        # [Section 10] INT_ORB 단독 신규 매수 전면 비활성화 (KRX 실증 데이터 -6,596만원 손실, 승률 11.7%)
        setup_e = False
        orb_reason = "DISABLED_STRATEGY_INT_ORB (실증 -6,596만 손실로 신규 매수 중단)"
        evaluate_and_add("INT_ORB", f"장초반 고가({or_high if or_high else 0:,}원) 돌파 (비활성화)", "ORB", False, OrderType.MARKET, entry_timing_valid=False, timing_reason=orb_reason)

        # [전략 F: PREVIOUS DAY HIGH BREAKOUT (PDH) (Section 11)]
        setup_f = False
        if sym.prev_high > 0 and curr_price > sym.prev_high:
            valid_pdh, pdh_reason, _ = validate_breakout_entry(
                curr_price=curr_price,
                breakout_threshold_price=sym.prev_high,
                rvol=rvol,
                current_1m_open=c_1m_open,
                price_3m_ago=p_3m_ago,
                high_price=sym.high_price,
                min_rvol=1.5,
                strategy_id="INT_PDH",
                symbol=sym.iem_cd,
                vwap=vwap
            )
            setup_f = valid_pdh
        evaluate_and_add("INT_PDH", f"전일 고가({sym.prev_high:,}원) 돌파", "PDH", setup_f, OrderType.MARKET)

        # [보조 전략: 당일 신고가 돌파 (DAY HIGH)]
        setup_dh = False
        if sym.high_price > 0 and curr_price >= sym.high_price and not setup_b:
            valid_dh, dh_reason, _ = validate_breakout_entry(
                curr_price=curr_price,
                breakout_threshold_price=sym.high_price,
                rvol=rvol,
                current_1m_open=c_1m_open,
                price_3m_ago=p_3m_ago,
                high_price=sym.high_price,
                min_rvol=1.5,
                strategy_id="INT_DAY_HIGH",
                symbol=sym.iem_cd,
                vwap=vwap
            )
            setup_dh = valid_dh
        evaluate_and_add("INT_DAY_HIGH", f"당일 최고가({sym.high_price:,}원) 갱신", "BREAKOUT", setup_dh, OrderType.MARKET)

        # [보조 전략: 뉴스/공시 모멘텀]
        if has_news and rvol >= 1.5:
            evaluate_and_add("INT_NEWS_MOMENTUM", "실시간 뉴스/공시 수급 모멘텀", "MOMENTUM", True, OrderType.MARKET)

        # 미충족 탈락 사유 저장
        if not matched_any_setup:
            sym.buy_block_reasons.append("NO_SETUP")

        return signals

    # =========================================================================
    # [2] 스윙 전략 평가 (Section 22: 9 Strategies)
    # =========================================================================
    @classmethod
    def evaluate_swing_all(
        cls,
        sym: SymbolInfo,
        daily_candles: List[Dict[str, Any]],
        regime: MarketRegime,
        now: datetime,
        has_catalyst: bool = False,
        agg: Optional[CandleAggregator] = None
    ) -> List[TradeSignal]:
        """
        스윙 9대 전략 평가 (Section 22 ~ 25)
        """
        if len(daily_candles) < 60:
            return []
        if regime in (MarketRegime.BEAR, MarketRegime.PANIC):
            return []

        signals: List[TradeSignal] = []
        closes = [c["close"] for c in daily_candles]
        curr_price = closes[0]

        # 1분봉 및 RVOL 실시간 지표 (agg 연동)
        c_1m = agg.candles_1m if agg else []
        curr_1m = agg.current_1m if agg else None
        c_1m_open = curr_1m.open if curr_1m and curr_1m.open > 0 else (c_1m[-1].open if c_1m and c_1m[-1].open > 0 else None)
        p_3m_ago = c_1m[-3].open if len(c_1m) >= 3 and c_1m[-3].open > 0 else (c_1m[0].open if c_1m and c_1m[0].open > 0 else None)
        rvol = agg.calculate_rvol("1m", lookback=20) if agg else getattr(sym, "rvol", None)
        vwap = agg.calculate_vwap("1m") if agg else curr_price
        today_open = daily_candles[0].get("open", curr_price) if daily_candles else curr_price
        high_price = max(sym.high_price, daily_candles[0].get("high", curr_price)) if daily_candles else sym.high_price
        ret_3m = ((curr_price - p_3m_ago) / p_3m_ago) if p_3m_ago and p_3m_ago > 0 else 0.0
        is_breakout_20 = (curr_price >= max(c.high for c in c_1m[-20:])) if len(c_1m) >= 2 else True

        # 이동평균선 계산
        ma20 = sum(closes[:20]) / 20.0
        ma60 = sum(closes[:60]) / 60.0

        # ATR 계산
        trs = []
        for i in range(14):
            c_curr = daily_candles[i]
            c_prev = daily_candles[i + 1] if i + 1 < len(daily_candles) else c_curr
            tr = max(
                c_curr["high"] - c_curr["low"],
                abs(c_curr["high"] - c_prev["close"]),
                abs(c_curr["low"] - c_prev["close"])
            )
            trs.append(tr)
        atr_daily = sum(trs) / 14.0 if trs else curr_price * 0.02

        # 손절가 = Entry - 2.0 * ATR (Section 36)
        stop_price = normalize_price(int(curr_price - 2.0 * atr_daily), OrderSide.BUY)
        risk_per_share = curr_price - stop_price
        stop_ratio = risk_per_share / curr_price if curr_price > 0 else 0

        # 손절폭 > 12% 진입 금지 (Section 36)
        if stop_ratio > 0.120 or risk_per_share <= 0:
            return []

        target_1r = normalize_price(int(curr_price + 1.0 * risk_per_share), OrderSide.BUY)
        target_2r = normalize_price(int(curr_price + 2.0 * risk_per_share), OrderSide.BUY)
        target_3r = normalize_price(int(curr_price + 3.0 * risk_per_share), OrderSide.BUY)

        def make_swing_sig(strat_id: str, reason: str, score: float = 85.0) -> TradeSignal:
            return TradeSignal(
                strategy_id=strat_id,
                time_horizon=TimeHorizon.SWING,
                iem_cd=sym.iem_cd,
                name=sym.name,
                side=OrderSide.BUY,
                strategy_price=curr_price,
                stop_price=stop_price,
                score=score,
                reason=reason,
                timestamp=now,
                target_1r=target_1r,
                target_2r=target_2r,
                target_3r=target_3r,
                atr14=atr_daily,
                expected_rr=2.0,
                rule_score=score,
                approved_status="BUY_APPROVED",
                entry_timing_valid=True,
                timing_reason=reason
            )

        # 거래량 기준값 산출 (정체/거래량 소멸 종목 차단)
        vols = [c.get("volume", 0) for c in daily_candles]
        past_vol_10 = sum(vols[1:11]) / 10.0 if len(vols) >= 11 else (sum(vols) / len(vols) if vols else 1.0)
        today_vol = vols[0] if vols else 0

        # 1. 정배열 (Section 23: STRONG SWING TREND)
        # 실증 데이터 검증: 무조건 정배열 매수(승률 0%, -12.8M 손실) 차단
        # -> 당일 양봉 + VWAP 상회 + 3분 모멘텀 양수 + 20봉 돌파 or 당일 +1% 이상 수급 동반 필수
        if len(closes) >= 120:
            ma120 = sum(closes[:120]) / 120.0
            if curr_price > ma20 > ma60 > ma120:
                is_green = (curr_price >= today_open)
                above_vwap = (curr_price >= vwap) if vwap > 0 else True
                mom_ok = (ret_3m >= 0.0) if ret_3m is not None else True
                rvol_ok = (rvol >= 1.2) if rvol else True
                today_ret = (curr_price - today_open) / today_open if today_open > 0 else 0
                trigger_ok = is_breakout_20 or (today_ret >= 0.010)
                if is_green and above_vwap and mom_ok and rvol_ok and trigger_ok:
                    signals.append(make_swing_sig("SWG_TREND_ALIGN", "중기 이평선 정배열 추세 + 당일 수급/돌파 확인", 90.0))

        # 2. 20일 신고가 돌파
        hh20 = max(c["high"] for c in daily_candles[1:21])
        if curr_price > hh20:
            today_ret = (curr_price - today_open) / today_open if today_open > 0 else 0
            if today_ret >= 0.0:
                is_valid_bo, bo_reason, bo_metrics = validate_breakout_entry(
                    curr_price=curr_price,
                    breakout_threshold_price=hh20,
                    rvol=rvol,
                    current_1m_open=c_1m_open,
                    price_3m_ago=p_3m_ago,
                    high_price=high_price,
                    min_rvol=BREAKOUT_MIN_RVOL,
                    strategy_id="SWG_HH20_BREAKOUT",
                    symbol=sym.iem_cd,
                    today_open=today_open,
                    check_daily_open=True,
                    vwap=vwap
                )
                if is_valid_bo:
                    sig = make_swing_sig("SWG_HH20_BREAKOUT", f"20일 신고가({hh20:,}원) 돌파 (RVOL: {rvol:.2f})", 85.0)
                    sig.rvol = round(float(rvol), 2) if rvol else 1.0
                    sig.entry_timing_valid = True
                    sig.timing_reason = "20일신고가_수급확인"
                    signals.append(sig)
                else:
                    sym.buy_block_reasons.append(f"SWG_HH20_REJECT: {bo_reason}")
            else:
                sym.buy_block_reasons.append("SWG_HH20_REJECT: FALLING_DAILY_RED_BAR")

        # 3. 60일 신고가 돌파 (Section 24)
        hh60 = max(c["high"] for c in daily_candles[1:61])
        if curr_price > hh60:
            today_ret = (curr_price - today_open) / today_open if today_open > 0 else 0
            # [수정] 0% 이상(양봉) 및 10% 미만(추격 금지) 필수 확인
            if 0.0 <= today_ret < 0.10:
                is_valid_bo, bo_reason, bo_metrics = validate_breakout_entry(
                    curr_price=curr_price,
                    breakout_threshold_price=hh60,
                    rvol=rvol,
                    current_1m_open=c_1m_open,
                    price_3m_ago=p_3m_ago,
                    high_price=high_price,
                    min_rvol=BREAKOUT_MIN_RVOL,
                    strategy_id="SWG_HH60_BREAKOUT",
                    symbol=sym.iem_cd,
                    today_open=today_open,
                    check_daily_open=True,
                    vwap=vwap
                )
                if is_valid_bo:
                    sig = make_swing_sig("SWG_HH60_BREAKOUT", f"60일 신고가({hh60:,}원) 돌파 (RVOL: {rvol:.2f})", 92.0)
                    sig.rvol = round(float(rvol), 2) if rvol else 1.0
                    sig.entry_timing_valid = True
                    sig.timing_reason = "60일신고가_수급확인"
                    signals.append(sig)
                else:
                    sym.buy_block_reasons.append(f"SWG_HH60_REJECT: {bo_reason}")
            else:
                reason = "CHASE_OVER_10PCT" if today_ret >= 0.10 else "FALLING_DAILY_RED_BAR"
                sym.buy_block_reasons.append(f"SWG_HH60_REJECT: {reason}")


        # 4. MA20 눌림목 (Section 25: 추세 + 지지테스트 + 양봉반등 + 거래량)
        dist_ma20 = abs(curr_price - ma20) / ma20
        if len(daily_candles) >= 10 and curr_price >= ma20 and dist_ma20 <= 0.030 and ma20 >= ma60:
            past_10_high = max(c.get("high", 0) for c in daily_candles[1:11])
            prior_trend = (past_10_high >= ma20 * 1.03)
            low_tested = min(daily_candles[0].get("low", curr_price), daily_candles[1].get("low", curr_price)) <= ma20 * 1.015
            is_green = (curr_price >= daily_candles[0].get("open", curr_price))
            vol_ok = (today_vol >= past_vol_10 * 0.60) if past_vol_10 > 0 else True
            if prior_trend and low_tested and is_green and vol_ok:
                signals.append(make_swing_sig("SWG_MA20_PULLBACK", "20일선 눌림목 반등 (추세+지지+양봉)", 88.0))

        # 5. MA60 눌림목 (추세 + 지지테스트 + 양봉반등 + 거래량)
        dist_ma60 = abs(curr_price - ma60) / ma60
        if len(daily_candles) >= 15 and curr_price >= ma60 and dist_ma60 <= 0.030:
            past_15_high = max(c.get("high", 0) for c in daily_candles[1:16])
            prior_trend = (past_15_high >= ma60 * 1.03)
            low_tested = min(daily_candles[0].get("low", curr_price), daily_candles[1].get("low", curr_price)) <= ma60 * 1.015
            is_green = (curr_price >= daily_candles[0].get("open", curr_price))
            vol_ok = (today_vol >= past_vol_10 * 0.60) if past_vol_10 > 0 else True
            if prior_trend and low_tested and is_green and vol_ok:
                signals.append(make_swing_sig("SWG_MA60_PULLBACK", "60일선 중기 지지선 반등 (추세+지지+양봉)", 84.0))

        # 6. 박스권 돌파
        if len(daily_candles) >= 20:
            box_high = max(c["high"] for c in daily_candles[1:20])
            box_low = min(c["low"] for c in daily_candles[1:20])
            box_rng = (box_high - box_low) / box_low if box_low > 0 else 1.0
            is_green = (curr_price >= today_open)
            above_vwap = (curr_price >= vwap) if vwap > 0 else True
            rvol_ok = (rvol >= 1.2) if rvol else True
            if box_rng <= 0.12 and curr_price > box_high and is_green and above_vwap and rvol_ok:
                signals.append(make_swing_sig("SWG_BOX_BREAKOUT", "20일 박스권(변동폭<=12%) 상단 돌파 (수급확인)", 89.0))

        # 7. 거래량 수축 후 확장 (와이어링크 등 당일 음봉 폭락 매수 차단)
        vols_5 = [c["volume"] for c in daily_candles[:5]]
        if len(daily_candles) >= 10:
            past_vol_avg = sum(c["volume"] for c in daily_candles[5:15]) / 10.0
            is_green = (curr_price >= today_open)
            above_vwap = (curr_price >= vwap) if vwap > 0 else True
            mom_ok = (ret_3m >= 0.0) if ret_3m is not None else True
            rvol_ok = (rvol >= 1.2) if rvol else True
            if past_vol_avg > 0 and vols_5[0] >= past_vol_avg * 2.0 and min(vols_5[1:5]) <= past_vol_avg * 0.6 and is_green and above_vwap and mom_ok and rvol_ok:
                signals.append(make_swing_sig("SWG_VOL_EXPANSION", "거래량 수축 후 폭발적 거래량 확장 돌파 (양봉+VWAP상회)", 91.0))

        # 8. 주봉 추세 (SWG_WEEKLY_TREND: MA 정배열 + 거래량 필터 + 5일신고가 or 지지반등 타이밍)
        if len(daily_candles) >= 50 and curr_price > ma20 and ma20 >= ma60:
            vol_ok = (today_vol >= past_vol_10 * 0.70) if past_vol_10 > 0 else True
            hh5 = max(c.get("high", 0) for c in daily_candles[1:6]) if len(daily_candles) >= 6 else ma20
            is_green = (curr_price >= daily_candles[0].get("open", curr_price))
            timing_trigger = (curr_price >= hh5) or (is_green and abs(curr_price - ma20) / ma20 <= 0.020)
            if vol_ok and timing_trigger:
                signals.append(make_swing_sig("SWG_WEEKLY_TREND", "주봉 상승 추세 지속 및 타이밍 트리거 충족", 82.0))

        # 9. 실적/뉴스 + 기술적 돌파 (Catalyst Breakout)
        if has_catalyst and curr_price > ma20:
            signals.append(make_swing_sig("SWG_CATALYST_BREAKOUT", "실적/모멘텀 모멘텀 + 기술적 돌파", 94.0))

        return signals
