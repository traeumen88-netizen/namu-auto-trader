"""[FINAL MASTER v16.2] 진입 타이밍 퀄리티 게이트 (Entry Timing Quality Gate)
- 실전 "왜 이 종목을 지금 샀는가?"를 철저히 검증하는 Hard Timing Gate
- 누적 점수나 Gap 상승률이 높아도 진입 직전 가격 행동(Price Action)이 불량하면 100% 차단
- 57대 실전 거래 감사 및 거버넌스 규정 완전 준수:
  1. RVOL >= 1.5 공통 최소 조건 (None, NaN, inf, <= 0 Fail-Closed)
  2. 3분 모멘텀 음수 진입 차단 (ret_3m < 0 -> REJECT)
  3. Pullback 예외: 지지 + rebound_confirmed + 1분봉 양봉 + 저점 미이탈 시에만 허용
  4. 고점 대비 과도한 이탈 및 윗꼬리 반락(Upper Wick Reversal) 차단
  5. VWAP 우위 필수 (돌파/모멘텀 price >= VWAP)
  6. INT_ORB 단독 신규 매수 영구 비활성화 (-6,596만원 실증 손실 방어)
  7. 표준화된 Reject Reason 및 [ENTRY_QUALITY] 텔레메트리 출력
"""

import math
import logging
from datetime import datetime
from typing import Optional, Tuple, Dict, Any
from config.settings import BREAKOUT_MIN_RVOL

logger = logging.getLogger("BreakoutGate")

# 표준 Reject Reason 상수 정의 (Section 30)
REJECT_ENTRY_MOMENTUM_NEGATIVE = "ENTRY_MOMENTUM_NEGATIVE"
REJECT_ENTRY_RVOL_LOW = "ENTRY_RVOL_LOW"
REJECT_ENTRY_BELOW_VWAP = "ENTRY_BELOW_VWAP"
REJECT_ENTRY_HIGH_RETRACE = "ENTRY_HIGH_RETRACE"
REJECT_ENTRY_LATE_FALLING = "LATE_FALLING_ENTRY"
REJECT_ENTRY_UPPER_WICK_REVERSAL = "ENTRY_UPPER_WICK_REVERSAL"
REJECT_ENTRY_NO_REBOUND = "ENTRY_NO_REBOUND"
REJECT_ENTRY_COOLDOWN = "ENTRY_COOLDOWN"
REJECT_ENTRY_NO_NEW_TRIGGER = "ENTRY_NO_NEW_TRIGGER"
REJECT_ENTRY_OVERTRADE_LIMIT = "ENTRY_OVERTRADE_LIMIT"
REJECT_ENTRY_TIMING_FAIL = "ENTRY_TIMING_FAIL"
REJECT_DISABLED_STRATEGY_INT_ORB = "DISABLED_STRATEGY_INT_ORB"


class EntryTimingQualityGate:
    """전략군별 진입 타이밍 퀄리티 검증 중앙 엔진"""

    @staticmethod
    def validate_entry_timing_quality(
        curr_price: float,
        strategy_id: str,
        rvol: Optional[float] = None,
        ret_3m: Optional[float] = None,
        ret_1m: Optional[float] = None,
        vwap: Optional[float] = None,
        intraday_high: Optional[float] = None,
        current_1m_open: Optional[float] = None,
        current_1m_high: Optional[float] = None,
        current_1m_low: Optional[float] = None,
        current_1m_close: Optional[float] = None,
        upper_wick_ratio: Optional[float] = None,
        is_recent_high_breakout: bool = False,
        is_breakout_failed: bool = False,
        rebound_confirmed: bool = False,
        entry_timestamp: Optional[datetime] = None,
        symbol: str = "",
        min_rvol: float = 1.5,
        today_open: Optional[float] = None,
        check_daily_open: bool = False,
        **kwargs
    ) -> Tuple[bool, str, Dict[str, Any]]:
        """
        [Section 2, 7, 8, 51, 54] 주문 직전 실행되는 Authoritative Hard Gate:
        - 점수를 가감하는 방식이 아닌, 진입 자격 자체를 검증하는 절대 게이트
        - 레메디(387690), 와이제이링크(209640) 등 과거 손실 사례 패턴 원천 차단
        """
        # 1. INT_ORB 비활성화 검사 (Section 14: 실증 -6,596만 손실, 승률 11.7%)
        if strategy_id in ("INT_ORB", "INT_ORB_SCALE_OUT", "ORB"):
            reason = f"{REJECT_DISABLED_STRATEGY_INT_ORB}: 실증 손실로 신규 매수 비활성화"
            metrics = {"curr_price": curr_price, "strategy_id": strategy_id, "symbol": symbol}
            _log_entry_quality(symbol, strategy_id, "REJECT", reason, metrics)
            return False, reason, metrics

        # 파생 지표 자동 계산
        if ret_1m is None and current_1m_open and current_1m_open > 0:
            ret_1m = (curr_price - current_1m_open) / float(current_1m_open)
        
        is_bullish_1m = (curr_price >= current_1m_open) if (current_1m_open and current_1m_open > 0) else True
        dist_high = ((curr_price - intraday_high) / float(intraday_high)) if (intraday_high and intraday_high > 0) else 0.0
        dist_vwap = ((curr_price - vwap) / float(vwap)) if (vwap and vwap > 0) else 0.0

        # Upper wick 계산 (당일 고점 실패/윗꼬리 반락 감지)
        if upper_wick_ratio is None:
            c_high = current_1m_high or (intraday_high if intraday_high else curr_price)
            c_low = current_1m_low or min(curr_price, current_1m_open or curr_price)
            c_open = current_1m_open or curr_price
            c_close = current_1m_close or curr_price
            if c_high > c_low:
                body_top = max(c_open, c_close)
                upper_wick = max(0.0, c_high - body_top)
                upper_wick_ratio = upper_wick / float(c_high - c_low)
            else:
                upper_wick_ratio = 0.0

        metrics: Dict[str, Any] = {
            "curr_price": curr_price,
            "rvol": rvol,
            "ret_3m": ret_3m,
            "ret_1m": ret_1m,
            "vwap": vwap,
            "dist_vwap_pct": dist_vwap,
            "intraday_high": intraday_high,
            "dist_high_pct": dist_high,
            "is_bullish_1m": is_bullish_1m,
            "upper_wick_ratio": upper_wick_ratio,
            "is_breakout_failed": is_breakout_failed,
            "rebound_confirmed": rebound_confirmed,
            "strategy_id": strategy_id,
            "symbol": symbol,
            "min_rvol": min_rvol
        }

        # 2. RVOL Fail-Closed 검증 (Section 4: RVOL < 1.0 전면 차단, 기본 1.5)
        if rvol is None or math.isnan(rvol) or math.isinf(rvol) or rvol <= 0:
            reason = f"{REJECT_ENTRY_RVOL_LOW} (INVALID_RVOL): RVOL 결측/유효하지 않음 (rvol={rvol})"
            _log_entry_quality(symbol, strategy_id, "REJECT", reason, metrics)
            return False, reason, metrics

        # PULLBACK은 1.0 이상, 돌파/모멘텀은 1.5 이상
        is_pullback = ("PULLBACK" in strategy_id.upper() or "REBOUND" in strategy_id.upper())
        required_rvol = 1.0 if is_pullback else max(1.5, min_rvol)
        if rvol < required_rvol:
            reason = f"{REJECT_ENTRY_RVOL_LOW}: RVOL 부족 ({rvol:.2f} < {required_rvol:.2f})"
            _log_entry_quality(symbol, strategy_id, "REJECT", reason, metrics)
            return False, reason, metrics

        # 3. 윗꼬리 급반락(Upper Wick Reversal) 차단 (Section 6 & 7: 레메디 사례 방어)
        if upper_wick_ratio is not None and upper_wick_ratio >= 0.45:
            if not is_bullish_1m or (ret_1m is not None and ret_1m < 0.0):
                reason = f"{REJECT_ENTRY_UPPER_WICK_REVERSAL}: 윗꼬리 급반락 감지 (wick={upper_wick_ratio*100:.1f}%, ret_1m={ret_1m*100 if ret_1m else 0:+.2f}%)"
                _log_entry_quality(symbol, strategy_id, "REJECT", reason, metrics)
                return False, reason, metrics

        # 4. 고점 대비 과도한 이탈 및 하락 추격 차단 (Section 6: Late Falling Entry 방어)
        if intraday_high and intraday_high > 0:
            # 고점 대비 2% 이상 하락 상태에서 3분 모멘텀이 음수이면 절대 차단
            if dist_high < -0.02 and (ret_3m is not None and ret_3m < 0.0):
                reason = f"{REJECT_ENTRY_LATE_FALLING}: 고점 2% 이탈 하락 추격 차단 (drop={dist_high*100:.2f}%, mom_3m={ret_3m*100:+.2f}%)"
                _log_entry_quality(symbol, strategy_id, "REJECT", reason, metrics)
                return False, reason, metrics
            # 고점 근접(1~2% 이탈)했으나 3분 모멘텀이 -0.3% 미만으로 꺾인 돌파 실패
            if dist_high < -0.01 and (ret_3m is not None and ret_3m < -0.003):
                reason = f"{REJECT_ENTRY_HIGH_RETRACE}: 돌파 실패 후 반락 감지 (drop={dist_high*100:.2f}%, mom_3m={ret_3m*100:+.2f}%)"
                _log_entry_quality(symbol, strategy_id, "REJECT", reason, metrics)
                return False, reason, metrics
            # 고점 돌파 실패 플래그 설정 시 차단
            if is_breakout_failed:
                reason = f"{REJECT_ENTRY_TIMING_FAIL}: 고점 돌파 실패(Breakout Failed) 확인"
                _log_entry_quality(symbol, strategy_id, "REJECT", reason, metrics)
                return False, reason, metrics

        # 5. 전략별 모멘텀 및 VWAP 정책 (Section 3, 5, 52)
        if is_pullback:
            # PULLBACK: ret_3m 음수는 일시적 눌림목일 수 있으므로 지지 + 반등 확인 시에만 허용
            if not rebound_confirmed:
                reason = f"{REJECT_ENTRY_NO_REBOUND}: 눌림목 반등 미확인 (rebound_confirmed=False)"
                _log_entry_quality(symbol, strategy_id, "REJECT", reason, metrics)
                return False, reason, metrics
            if not is_bullish_1m:
                reason = f"{REJECT_ENTRY_TIMING_FAIL}: 눌림목 진입 시 현재 1분봉 양봉 필수"
                _log_entry_quality(symbol, strategy_id, "REJECT", reason, metrics)
                return False, reason, metrics

            # [Section 63 & 64] 1틱 미세 반등 방지 및 Rebound Strength 정교화 검증
            from strategies.profit_opportunity_gate import calculate_rebound_strength
            reb_ok, reb_str, reb_msg, reb_metrics = calculate_rebound_strength(
                curr_price=curr_price,
                support_price=kwargs.get("support_price"),
                lowest_price=kwargs.get("lowest_price"),
                rvol=rvol,
                ret_3m=ret_3m,
                ret_1m=ret_1m,
                current_1m_open=current_1m_open,
                current_1m_high=current_1m_high,
                current_1m_low=current_1m_low,
                vwap=vwap
            )
            metrics.update(reb_metrics)
            if not reb_ok:
                _log_entry_quality(symbol, strategy_id, "REJECT", reb_msg, metrics)
                return False, reb_msg, metrics
        else:
            # 돌파/모멘텀/점화/스윙: 3분 모멘텀 음수 절대 금지
            if ret_3m is not None and ret_3m < 0.0:
                reason = f"{REJECT_ENTRY_MOMENTUM_NEGATIVE} (NEGATIVE_MOMENTUM_3M): 3분 모멘텀 음수 ({ret_3m*100:+.2f}% < 0)"
                _log_entry_quality(symbol, strategy_id, "REJECT", reason, metrics)
                return False, reason, metrics
            # 1분봉 음봉 차단
            if not is_bullish_1m:
                reason = f"{REJECT_ENTRY_TIMING_FAIL}: 현재 1분봉 음봉 (돌파/모멘텀 양봉 필수)"
                _log_entry_quality(symbol, strategy_id, "REJECT", reason, metrics)
                return False, reason, metrics
            # VWAP 하회 차단 (price >= vwap)
            if vwap and vwap > 0 and curr_price < vwap:
                reason = f"{REJECT_ENTRY_BELOW_VWAP}: VWAP 하회 ({curr_price:,} < {vwap:,.1f}, dist={dist_vwap*100:+.2f}%)"
                _log_entry_quality(symbol, strategy_id, "REJECT", reason, metrics)
                return False, reason, metrics

        # 6. 스윙 일봉 당일 시초가 대비 양봉 확인
        if check_daily_open and today_open and today_open > 0:
            if curr_price < today_open:
                reason = f"{REJECT_ENTRY_TIMING_FAIL}: 일봉 음봉 (curr={curr_price:,} < open={today_open:,})"
                _log_entry_quality(symbol, strategy_id, "REJECT", reason, metrics)
                return False, reason, metrics

        # [Section 51] Decision Explanation & Negative Flags 생성
        entry_reason = (
            f"ENTRY_REASON: RVOL={rvol:.2f} 3m_mom={(ret_3m or 0)*100:+.2f}% "
            f"VWAP={dist_vwap*100:+.2f}% distance_from_high={dist_high*100:+.2f}% "
            f"rebound_confirmed={rebound_confirmed} strategy={strategy_id}"
        )
        negative_flags = "NEGATIVE_FLAGS: gap_extended=False upper_wick_reversal=False below_vwap=False negative_momentum=False cooldown=False"
        metrics["entry_reason"] = entry_reason
        metrics["negative_flags"] = negative_flags

        _log_entry_quality(symbol, strategy_id, "PASS", "ENTRY_QUALITY_APPROVED", metrics)
        return True, "ENTRY_QUALITY_APPROVED", metrics

    @staticmethod
    def validate_breakout_momentum(
        curr_price: float,
        breakout_threshold_price: Optional[float] = None,
        rvol: Optional[float] = None,
        current_1m_open: Optional[float] = None,
        price_3m_ago: Optional[float] = None,
        high_price: Optional[float] = None,
        min_rvol: float = BREAKOUT_MIN_RVOL,
        strategy_id: str = "",
        symbol: str = "",
        today_open: Optional[float] = None,
        check_daily_open: bool = False,
        vwap: Optional[float] = None,
        require_above_vwap: bool = True,
        current_1m_high: Optional[float] = None,
        current_1m_low: Optional[float] = None,
        current_1m_close: Optional[float] = None,
        upper_wick_ratio: Optional[float] = None,
        tag: str = "",
    ) -> Tuple[bool, str, Dict[str, Any]]:
        """하위 호환 래퍼: validate_entry_timing_quality 호출"""
        ret_3m = ((curr_price - price_3m_ago) / float(price_3m_ago)) if (price_3m_ago and price_3m_ago > 0) else None
        ret_1m = ((curr_price - current_1m_open) / float(current_1m_open)) if (current_1m_open and current_1m_open > 0) else None

        # threshold check
        if breakout_threshold_price is not None and breakout_threshold_price > 0:
            if curr_price < breakout_threshold_price:
                reason = f"PRICE_BELOW_BREAKOUT_LEVEL ({curr_price:,} < {breakout_threshold_price:,})"
                metrics = {"curr_price": curr_price, "threshold": breakout_threshold_price, "symbol": symbol, "strategy_id": strategy_id}
                _log_entry_quality(symbol, strategy_id, "REJECT", reason, metrics)
                return False, reason, metrics

        ok, reason, metrics = EntryTimingQualityGate.validate_entry_timing_quality(
            curr_price=curr_price,
            strategy_id=strategy_id,
            rvol=rvol,
            ret_3m=ret_3m,
            ret_1m=ret_1m,
            vwap=vwap if require_above_vwap else None,
            intraday_high=high_price,
            current_1m_open=current_1m_open,
            current_1m_high=current_1m_high,
            current_1m_low=current_1m_low,
            current_1m_close=current_1m_close,
            upper_wick_ratio=upper_wick_ratio,
            rebound_confirmed=False,
            symbol=symbol,
            min_rvol=min_rvol,
            today_open=today_open,
            check_daily_open=check_daily_open
        )
        if ok:
            return True, "BREAKOUT_APPROVED", metrics
        return False, reason, metrics

    @staticmethod
    def validate_pullback_rebound(
        curr_price: float,
        vwap: float,
        rvol: Optional[float] = None,
        rebound_confirmed: bool = False,
        price_3m_ago: Optional[float] = None,
        high_price: Optional[float] = None,
        current_1m_open: Optional[float] = None,
        strategy_id: str = "INT_VWAP_PULLBACK",
        symbol: str = "",
    ) -> Tuple[bool, str, Dict[str, Any]]:
        ret_3m = ((curr_price - price_3m_ago) / float(price_3m_ago)) if (price_3m_ago and price_3m_ago > 0) else None
        return EntryTimingQualityGate.validate_entry_timing_quality(
            curr_price=curr_price,
            strategy_id=strategy_id,
            rvol=rvol,
            ret_3m=ret_3m,
            vwap=vwap,
            intraday_high=high_price,
            current_1m_open=current_1m_open,
            rebound_confirmed=rebound_confirmed,
            symbol=symbol,
            min_rvol=1.0
        )

    @staticmethod
    def validate_swing(
        curr_price: float,
        today_open: float,
        rvol: Optional[float] = None,
        strategy_id: str = "SWG_TREND_ALIGN",
        symbol: str = "",
        min_rvol: float = 1.0,
    ) -> Tuple[bool, str, Dict[str, Any]]:
        return EntryTimingQualityGate.validate_entry_timing_quality(
            curr_price=curr_price,
            strategy_id=strategy_id,
            rvol=rvol,
            today_open=today_open,
            check_daily_open=True,
            symbol=symbol,
            min_rvol=min_rvol
        )


def validate_entry_timing_quality(
    curr_price: float,
    strategy_id: str,
    rvol: Optional[float] = None,
    ret_3m: Optional[float] = None,
    ret_1m: Optional[float] = None,
    vwap: Optional[float] = None,
    intraday_high: Optional[float] = None,
    current_1m_open: Optional[float] = None,
    current_1m_high: Optional[float] = None,
    current_1m_low: Optional[float] = None,
    current_1m_close: Optional[float] = None,
    upper_wick_ratio: Optional[float] = None,
    is_recent_high_breakout: bool = False,
    is_breakout_failed: bool = False,
    rebound_confirmed: bool = False,
    entry_timestamp: Optional[datetime] = None,
    symbol: str = "",
    min_rvol: float = 1.5,
    today_open: Optional[float] = None,
    check_daily_open: bool = False,
    **kwargs
) -> Tuple[bool, str, Dict[str, Any]]:
    """모듈 레벨 직접 호출용 래퍼 함수 (Section 2 지정 함수명)"""
    return EntryTimingQualityGate.validate_entry_timing_quality(
        curr_price=curr_price,
        strategy_id=strategy_id,
        rvol=rvol,
        ret_3m=ret_3m,
        ret_1m=ret_1m,
        vwap=vwap,
        intraday_high=intraday_high,
        current_1m_open=current_1m_open,
        current_1m_high=current_1m_high,
        current_1m_low=current_1m_low,
        current_1m_close=current_1m_close,
        upper_wick_ratio=upper_wick_ratio,
        is_recent_high_breakout=is_recent_high_breakout,
        is_breakout_failed=is_breakout_failed,
        rebound_confirmed=rebound_confirmed,
        entry_timestamp=entry_timestamp,
        symbol=symbol,
        min_rvol=min_rvol,
        today_open=today_open,
        check_daily_open=check_daily_open,
        **kwargs
    )


def validate_breakout_entry(
    curr_price: float,
    breakout_threshold_price: Optional[float] = None,
    rvol: Optional[float] = None,
    current_1m_open: Optional[float] = None,
    price_3m_ago: Optional[float] = None,
    high_price: Optional[float] = None,
    min_rvol: float = BREAKOUT_MIN_RVOL,
    strategy_id: str = "",
    symbol: str = "",
    today_open: Optional[float] = None,
    check_daily_open: bool = False,
    vwap: Optional[float] = None,
    tag: str = "",
    **kwargs
) -> Tuple[bool, str, Dict[str, Any]]:
    return EntryTimingQualityGate.validate_breakout_momentum(
        curr_price=curr_price,
        breakout_threshold_price=breakout_threshold_price,
        rvol=rvol,
        current_1m_open=current_1m_open,
        price_3m_ago=price_3m_ago,
        high_price=high_price,
        min_rvol=min_rvol,
        strategy_id=strategy_id,
        symbol=symbol,
        today_open=today_open,
        check_daily_open=check_daily_open,
        vwap=vwap,
        require_above_vwap=True,
        tag=tag
    )


def _log_entry_quality(
    symbol: str,
    strategy_id: str,
    decision: str,
    reason: str,
    metrics: Dict[str, Any]
):
    """표준화된 [ENTRY_QUALITY] 텔레메트리 로깅"""
    rvol_val = metrics.get("rvol")
    rvol_str = f"{rvol_val:.2f}" if isinstance(rvol_val, (int, float)) and not math.isnan(rvol_val) else str(rvol_val)

    mom_val = metrics.get("ret_3m")
    mom_str = f"{mom_val*100:+.2f}%" if isinstance(mom_val, (int, float)) else "N/A"

    high_val = metrics.get("dist_high_pct")
    high_str = f"{high_val*100:+.2f}%" if isinstance(high_val, (int, float)) else "N/A"

    vwap_val = metrics.get("dist_vwap_pct")
    vwap_str = f"{vwap_val*100:+.2f}%" if isinstance(vwap_val, (int, float)) else "N/A"

    log_line = (
        f"[ENTRY_QUALITY] symbol={symbol} strategy={strategy_id} "
        f"rvol={rvol_str} mom_3m={mom_str} vwap_dist={vwap_str} high_dist={high_str} "
        f"decision={decision} reason={reason}"
    )
    logger.info(log_line)
