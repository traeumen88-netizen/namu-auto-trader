"""[FINAL MASTER v16.3] Minimum Profit Opportunity Gate & Scalp Churn Preventer
(strategies/profit_opportunity_gate.py)
Section 58 ~ 79: 핵심 전략 성격 보완 — 짤짤이(Scalp Churn) 방지 & 유효 이동폭 검증 엔진

핵심 원칙:
1. 1~2틱 짤짤이 및 초단타 Churn 원천 배제: 거래비용을 충분히 상회하는 유효 상승 여력 필수
2. validate_profit_opportunity(...) Hard Gate 신설:
   - expected_gross_move - (fee + tax + slippage + spread) = expected_net_move
   - expected_net_r 검증
   - ATR 정규화 기대 이동폭 (expected_move / ATR_pct)
   - Cost-to-Opportunity Ratio (cost / expected_move)
3. Rebound Strength 정교화 (Section 64):
   - 1틱 미세 반등으로 인한 허위 rebound_confirmed 원천 차단
   - 반등폭, 반등 거래량(RVOL), 캔들 몸통 비율(body ratio), 지지선 이격도 종합 평가
4. Latest Order Quote 기준 재계산 (Section 72) 및 Signal TTL (Section 73) 연계
5. Counterfactual Tracking (Section 74): 차단된 거래의 사후 MFE/MAE/수익률 추적 및 로깅
"""

import math
import logging
from datetime import datetime, timedelta
from typing import Optional, Tuple, Dict, Any, List

from core.tick_normalizer import get_tick_size

logger = logging.getLogger("ProfitOpportunityGate")

# Reject Reason 상수 정의 (Section 60, 62, 64, 71, 73)
REJECT_MICRO_PROFIT = "MICRO_PROFIT_OPPORTUNITY"
REJECT_LOW_EXPECTED_MOVE = "LOW_EXPECTED_MOVE"
REJECT_COST_TO_MOVE_RATIO_HIGH = "COST_TO_MOVE_RATIO_HIGH"
REJECT_EXPECTED_MFE_LOW = "EXPECTED_MFE_LOW"
REJECT_ONE_TICK_REBOUND = "ONE_TICK_REBOUND_INSUFFICIENT"
REJECT_REBOUND_STRENGTH_LOW = "REBOUND_STRENGTH_INSUFFICIENT"
REJECT_SIGNAL_EXPIRED = "SIGNAL_EXPIRED"
APPROVE_PROFIT_OPPORTUNITY = "PROFIT_OPPORTUNITY_APPROVED"


def calculate_rebound_strength(
    curr_price: float,
    support_price: Optional[float] = None,
    lowest_price: Optional[float] = None,
    rvol: Optional[float] = None,
    ret_3m: Optional[float] = None,
    ret_1m: Optional[float] = None,
    current_1m_open: Optional[float] = None,
    current_1m_high: Optional[float] = None,
    current_1m_low: Optional[float] = None,
    vwap: Optional[float] = None,
    atr14: Optional[float] = None,
) -> Tuple[bool, float, str, Dict[str, Any]]:
    """
    [Section 63 & 64] 눌림목 반등 강도(Rebound Strength) 정밀 평가
    - 1틱 반등(0.01~0.05%) 후 rebound_confirmed=True로 매수하는 초단타 오류 원천 차단
    - 반등폭, 거래량 유입, 모멘텀 반전, 양봉 캔들 몸통 비율을 복합 평가하여 0.0 ~ 1.0 점수화
    """
    ref_low = lowest_price or support_price
    tick_size = get_tick_size(int(curr_price)) if curr_price > 0 else 1.0

    # 1. 1틱 반등 검증 (Section 63)
    if lowest_price is None and support_price is None and current_1m_open is None:
        # 캔들/저점 상세 정보가 주어지지 않은 상위 레거시 호출 호환 (rebound_confirmed 신뢰)
        return True, 0.70, "REBOUND_STRENGTH_APPROVED", {
            "rebound_strength": 0.70,
            "rebound_won": 0.0,
            "rebound_pct": 0.01,
            "candle_body_ratio": 0.50,
            "rvol": rvol or 1.5,
            "momentum_3m": ret_3m or 0.0,
            "tick_size": tick_size
        }

    if ref_low and ref_low > 0:
        rebound_won = max(0.0, curr_price - ref_low)
        rebound_pct = rebound_won / float(ref_low)
    else:
        rebound_won = (curr_price - current_1m_open) if current_1m_open else 0.0
        rebound_pct = (rebound_won / float(current_1m_open)) if (current_1m_open and current_1m_open > 0) else 0.0

    # 1틱 이하 또는 0.15% 미만의 초미세 반등은 즉각 탈락
    if rebound_won <= tick_size or rebound_pct < 0.0015:
        reason = f"{REJECT_ONE_TICK_REBOUND}: 반등폭 {rebound_won:,.0f}원 ({rebound_pct*100:.2f}%)이 1틱({tick_size:,.0f}원) 수준으로 극미"
        metrics = {
            "rebound_strength": 0.10,
            "rebound_won": rebound_won,
            "rebound_pct": rebound_pct,
            "tick_size": tick_size
        }
        return False, 0.10, reason, metrics

    # 2. 캔들 몸통 비율 (Bullish Candle Body Ratio)
    c_open = current_1m_open or curr_price
    c_high = current_1m_high or max(curr_price, c_open)
    c_low = current_1m_low or min(curr_price, c_open)
    c_range = max(1.0, c_high - c_low)
    body = curr_price - c_open
    candle_body_ratio = max(0.0, body / c_range)

    # 3. 거래량 (RVOL) 및 모멘텀
    eff_rvol = rvol if (rvol and not math.isnan(rvol) and rvol > 0) else 1.0
    mom_3m = ret_3m or 0.0

    # 4. 필수 최소 요건 검사 (Section 64: 약한 반등 즉각 탈락)
    if candle_body_ratio < 0.20:
        reason = f"{REJECT_REBOUND_STRENGTH_LOW}: 캔들 몸통 비율 극미 ({candle_body_ratio*100:.1f}% < 20.0%, 윗꼬리/도지 반락)"
        metrics = {
            "rebound_strength": 0.20,
            "rebound_won": rebound_won,
            "rebound_pct": rebound_pct,
            "candle_body_ratio": round(candle_body_ratio, 3),
            "rvol": eff_rvol,
            "momentum_3m": mom_3m,
            "tick_size": tick_size
        }
        return False, 0.20, reason, metrics

    if mom_3m < -0.003:
        reason = f"{REJECT_REBOUND_STRENGTH_LOW}: 반등 중 3분 모멘텀 음수 지속 ({mom_3m*100:+.2f}% < -0.30%)"
        metrics = {
            "rebound_strength": 0.25,
            "rebound_won": rebound_won,
            "rebound_pct": rebound_pct,
            "candle_body_ratio": round(candle_body_ratio, 3),
            "rvol": eff_rvol,
            "momentum_3m": mom_3m,
            "tick_size": tick_size
        }
        return False, 0.25, reason, metrics

    if eff_rvol < 0.80:
        reason = f"{REJECT_REBOUND_STRENGTH_LOW}: 반등 거래량 부족 (RVOL {eff_rvol:.2f} < 0.80)"
        metrics = {
            "rebound_strength": 0.30,
            "rebound_won": rebound_won,
            "rebound_pct": rebound_pct,
            "candle_body_ratio": round(candle_body_ratio, 3),
            "rvol": eff_rvol,
            "momentum_3m": mom_3m,
            "tick_size": tick_size
        }
        return False, 0.30, reason, metrics

    # 5. 점수 가중치 합산 (0.0 ~ 1.0)
    score_rebound = min(1.0, rebound_pct / 0.005) * 0.35
    score_candle = min(1.0, candle_body_ratio / 0.40) * 0.25
    score_vol = min(1.0, eff_rvol / 1.5) * 0.20
    score_mom = (0.20 if mom_3m >= 0 else max(0.0, 0.20 + mom_3m * 10.0))

    total_strength = score_rebound + score_candle + score_vol + score_mom

    metrics = {
        "rebound_strength": round(total_strength, 3),
        "rebound_won": rebound_won,
        "rebound_pct": rebound_pct,
        "candle_body_ratio": round(candle_body_ratio, 3),
        "rvol": eff_rvol,
        "momentum_3m": mom_3m,
        "tick_size": tick_size
    }

    if total_strength < 0.40:
        reason = f"{REJECT_REBOUND_STRENGTH_LOW}: 종합 반등 강도 부족 ({total_strength:.2f} < 0.40)"
        return False, total_strength, reason, metrics

    return True, total_strength, "REBOUND_STRENGTH_APPROVED", metrics


class ProfitOpportunityGate:
    """
    [Section 59 ~ 73] Minimum Profit Opportunity Gate
    거래비용을 상회하는 유효 기대 이동폭 및 기대 MFE 검증 중앙 엔진
    """

    @staticmethod
    def validate_profit_opportunity(
        current_price: float,
        stop_price: float,
        target_price_1r: float = 0.0,
        target_price_2r: float = 0.0,
        strategy_id: str = "INT_BREAKOUT",
        atr14: Optional[float] = None,
        expected_move_pct: Optional[float] = None,
        expected_mfe_pct: Optional[float] = None,
        expected_net_r: Optional[float] = None,
        spread_pct: float = 0.001,
        fee_rate: float = 0.00015,
        tax_rate: float = 0.0018,
        slippage_rate: float = 0.0005,
        symbol: str = "",
        signal_created_at: Optional[datetime] = None,
        now: Optional[datetime] = None,
        **kwargs
    ) -> Tuple[bool, str, Dict[str, Any]]:
        """
        [Section 59 - 73] 주문 직전 실행되는 Profit Opportunity 절대 검증
        """
        now = now or datetime.now()

        # 1. Signal TTL 연동 검증 (Section 73)
        if signal_created_at:
            sig_age_ms = max(0.0, (now - signal_created_at).total_seconds() * 1000.0)
            if sig_age_ms > 30000.0:
                reason = f"{REJECT_SIGNAL_EXPIRED}: 신호 생성 후 {sig_age_ms:.0f}ms 경과 (TTL 30초 초과) -> 재평가 필요"
                metrics = {"signal_age_ms": sig_age_ms, "symbol": symbol, "strategy_id": strategy_id}
                return False, reason, metrics

        # 2. 거래비용 총합율 추정 (Section 60 & 71)
        # 매수(수수료+슬리피지+반호가) + 매도(수수료+거래세+슬리피지+반호가)
        roundtrip_cost_rate = (fee_rate * 2.0) + tax_rate + (slippage_rate * 2.0) + spread_pct

        # 3. 목표가 및 기대 총상승폭(Expected Gross Move) 산출 (Section 60, 72)
        if expected_move_pct is None or expected_move_pct <= 0:
            if target_price_1r > current_price:
                expected_move_pct = (target_price_1r - current_price) / float(current_price)
            elif target_price_2r > current_price:
                expected_move_pct = (target_price_2r - current_price) / float(current_price)
            elif atr14 and atr14 > 0 and current_price > 0:
                expected_move_pct = (atr14 * 1.2) / float(current_price)
            elif stop_price > 0 and current_price > stop_price:
                # 손절폭 기준 최소 1.5R 목표가 산출
                stop_dist = current_price - stop_price
                expected_move_pct = (stop_dist * 1.5) / float(current_price)
            else:
                expected_move_pct = 0.020  # 기본 기대치 2.0%

        expected_gross_move = max(0.0, float(expected_move_pct))
        expected_net_move = expected_gross_move - roundtrip_cost_rate

        # 4. 손절폭 및 R-비율 (Risk-Reward) 산출
        if stop_price > 0 and current_price > stop_price:
            stop_dist_pct = (current_price - stop_price) / float(current_price)
        elif atr14 and atr14 > 0 and current_price > 0:
            stop_dist_pct = atr14 / float(current_price)
        else:
            stop_dist_pct = 0.015

        if expected_net_r is None:
            expected_net_r = (expected_net_move / stop_dist_pct) if stop_dist_pct > 0 else 0.0

        # 5. ATR 대비 기대 이동폭 (ATR-Normalized Move, Section 62)
        atr_pct = (atr14 / float(current_price)) if (atr14 and atr14 > 0 and current_price > 0) else 0.015
        atr_normalized_move = expected_gross_move / atr_pct if atr_pct > 0 else 1.0

        # 6. 예상 MFE 산출 (Section 65)
        if expected_mfe_pct is None:
            expected_mfe_pct = max(expected_gross_move, atr_pct)
        predicted_mfe = expected_mfe_pct
        predicted_mae = stop_dist_pct

        # 7. Cost-to-Opportunity Ratio (Section 71)
        cost_to_opportunity_ratio = (roundtrip_cost_rate / expected_gross_move) if expected_gross_move > 0 else 999.0

        metrics: Dict[str, Any] = {
            "current_price": current_price,
            "stop_price": stop_price,
            "target_price_1r": target_price_1r,
            "target_price_2r": target_price_2r,
            "expected_gross_move": expected_gross_move,
            "expected_net_move": expected_net_move,
            "roundtrip_cost_rate": roundtrip_cost_rate,
            "cost_to_opportunity_ratio": cost_to_opportunity_ratio,
            "expected_net_r": expected_net_r,
            "atr_pct": atr_pct,
            "atr_normalized_move": atr_normalized_move,
            "predicted_mfe": predicted_mfe,
            "predicted_mae": predicted_mae,
            "strategy_id": strategy_id,
            "symbol": symbol
        }

        # =====================================================================
        # Hard Rejection Rules (Section 60, 62, 65, 70, 71)
        # =====================================================================

        # [규칙 1] 짤짤이/초미세 이동폭 차단 (Micro Profit Opportunity)
        # 총 기대상승폭이 0.40% 미만으로 극미한 짤짤이 패턴
        if expected_gross_move < 0.0040:
            reason = (
                f"{REJECT_MICRO_PROFIT}: 기대 수익폭 극미 (gross={expected_gross_move*100:.2f}% < 0.40%, "
                f"net={expected_net_move*100:.2f}%, net_R={expected_net_r:.2f})"
            )
            _log_profit_gate(symbol, strategy_id, "REJECT", reason, metrics)
            return False, reason, metrics

        # [규칙 2] ATR 대비 비정상적으로 작은 움직임 차단 (Section 62)
        # 정상 일중 변동성(ATR) 대비 기대 이동폭이 30% 미만인 정체 구간
        if atr_normalized_move < 0.30:
            reason = (
                f"{REJECT_LOW_EXPECTED_MOVE}: ATR 대비 기대 이동폭 부족 "
                f"(ATR비 {atr_normalized_move:.2f} < 0.30, gross={expected_gross_move*100:.2f}%, ATR={atr_pct*100:.2f}%)"
            )
            _log_profit_gate(symbol, strategy_id, "REJECT", reason, metrics)
            return False, reason, metrics

        # [규칙 3] 거래비용이 기대수익을 잠식하는 경우 (Cost-to-Opportunity Ratio > 60%)
        if cost_to_opportunity_ratio > 0.60 or expected_net_move <= 0:
            reason = (
                f"{REJECT_COST_TO_MOVE_RATIO_HIGH}: 거래비용 비율 과다 "
                f"({cost_to_opportunity_ratio*100:.1f}% > 60.0%, net_move={expected_net_move*100:+.2f}%)"
            )
            _log_profit_gate(symbol, strategy_id, "REJECT", reason, metrics)
            return False, reason, metrics

        # [규칙 4] 순수익 여력 또는 Net R 극미 차단
        if expected_net_move < 0.0015 or expected_net_r < 0.25:
            reason = (
                f"{REJECT_MICRO_PROFIT}: 순수익 및 R-비율 부족 "
                f"(net={expected_net_move*100:.2f}% < 0.15%, net_R={expected_net_r:.2f} < 0.25)"
            )
            _log_profit_gate(symbol, strategy_id, "REJECT", reason, metrics)
            return False, reason, metrics

        # [규칙 4] 예상 MFE가 비용 대비 1.5배 미만이거나 0.4% 미만인 경우 (Section 65)
        if predicted_mfe < (roundtrip_cost_rate * 1.5) or predicted_mfe < 0.0040:
            reason = (
                f"{REJECT_EXPECTED_MFE_LOW}: 예상 MFE 부족 "
                f"({predicted_mfe*100:.2f}% < 비용 1.5배={roundtrip_cost_rate*1.5*100:.2f}%)"
            )
            _log_profit_gate(symbol, strategy_id, "REJECT", reason, metrics)
            return False, reason, metrics

        # [규칙 5] 전략별 추가 맞춤 기준 (Section 70)
        strat_upper = strategy_id.upper()
        if "BREAKOUT" in strat_upper:
            # 돌파: 후속 추세 모멘텀 잠재력이 필요하므로 Net R >= 0.40, Gross Move >= 0.6%
            if expected_net_r < 0.40 or expected_gross_move < 0.0060:
                reason = f"{REJECT_MICRO_PROFIT}: 돌파 후속 여력 부족 (gross={expected_gross_move*100:.2f}%, net_R={expected_net_r:.2f})"
                _log_profit_gate(symbol, strategy_id, "REJECT", reason, metrics)
                return False, reason, metrics
        elif "MOMENTUM" in strat_upper or "IGNITION" in strat_upper:
            # 모멘텀: 지속 여력 Net R >= 0.35, Gross Move >= 0.5%
            if expected_net_r < 0.35 or expected_gross_move < 0.0050:
                reason = f"{REJECT_LOW_EXPECTED_MOVE}: 모멘텀 지속 여력 부족 (gross={expected_gross_move*100:.2f}%, net_R={expected_net_r:.2f})"
                _log_profit_gate(symbol, strategy_id, "REJECT", reason, metrics)
                return False, reason, metrics

        _log_profit_gate(symbol, strategy_id, "PASS", APPROVE_PROFIT_OPPORTUNITY, metrics)
        return True, APPROVE_PROFIT_OPPORTUNITY, metrics


def _log_profit_gate(symbol: str, strategy: str, result: str, reason: str, metrics: Dict[str, Any]):
    gross = metrics.get("expected_gross_move", 0.0) * 100
    net = metrics.get("expected_net_move", 0.0) * 100
    net_r = metrics.get("expected_net_r", 0.0)
    cost_r = metrics.get("cost_to_opportunity_ratio", 0.0) * 100
    logger.info(
        f"[PROFIT_GATE] [{result}] symbol={symbol} strategy={strategy} | "
        f"gross={gross:+.2f}% net={net:+.2f}% net_R={net_r:.2f} cost_ratio={cost_r:.1f}% | reason={reason}"
    )
    passed = (result == "PASS")
    ProfitOpportunityTelemetry.get_instance().record_attempt(passed, reason, metrics)


# -----------------------------------------------------------------------------
# Counterfactual Tracker (Section 74: 차단된 거래의 사후 성과 추적)
# -----------------------------------------------------------------------------
class CounterfactualTracker:
    """
    [Section 74] Profit Opportunity Gate에 의해 차단된 거래의 사후 가격을 추적하여
    '좋은 거래를 지나치게 잘라내지 않는지' 과학적으로 검증하고 기록하는 추적기
    """
    _instance = None

    def __init__(self):
        self.records: List[Dict[str, Any]] = []

    @classmethod
    def get_instance(cls):
        if cls._instance is None:
            cls._instance = cls()
        return cls._instance

    def reset(self):
        """내부 기록 초기화 (테스트 격리용)"""
        self.records.clear()

    @property
    def blocked_trades(self) -> List[Dict[str, Any]]:
        return self.records

    def record_blocked_trade(
        self,
        *args,
        symbol: str = "",
        strategy_id: str = "",
        ref_price: float = 0.0,
        reason: str = "",
        metrics: Optional[Dict[str, Any]] = None,
        now: Optional[datetime] = None,
        **kwargs
    ) -> Dict[str, Any]:
        """차단된 거래 등록"""
        if args:
            symbol = str(args[0])
            if len(args) >= 5:
                # ("035420", "NAVER", "INT_BREAKOUT", "REJECT_OPPORTUNITY", 200000, now)
                strategy_id = str(args[2])
                reason = str(args[3])
                ref_price = float(args[4])
                if len(args) >= 6 and isinstance(args[5], datetime):
                    now = args[5]
            elif len(args) >= 4:
                if isinstance(args[2], (int, float)):
                    strategy_id = str(args[1])
                    ref_price = float(args[2])
                    reason = str(args[3])
                else:
                    strategy_id = str(args[1])
                    reason = str(args[2])
                    ref_price = float(args[3])
            elif len(args) >= 3:
                strategy_id = str(args[1])
                ref_price = float(args[2])
            elif len(args) >= 2:
                strategy_id = str(args[1])

        if "iem_cd" in kwargs and not symbol:
            symbol = kwargs["iem_cd"]
        if "strategy" in kwargs and not strategy_id:
            strategy_id = kwargs["strategy"]
        if "price" in kwargs and ref_price == 0.0:
            ref_price = float(kwargs["price"])
        if "blocked_reason" in kwargs and not reason:
            reason = kwargs["blocked_reason"]
        if "timestamp" in kwargs and now is None:
            now = kwargs["timestamp"]

        now = now or datetime.now()
        ref_p = float(ref_price)
        rec = {
            "symbol": symbol,
            "strategy_id": strategy_id,
            "blocked_at": now,
            "ref_price": ref_p,
            "entry_price": ref_p,
            "status": "TRACKING",
            "highest_price": ref_p,
            "lowest_price": ref_p,
            "reason": reason,
            "metrics": metrics or {},
            "counterfactual_mfe": 0.0,
            "counterfactual_mae": 0.0,
            "mfe_pct": 0.0,
            "mae_pct": 0.0,
            "counterfactual_5m_return": 0.0,
            "counterfactual_15m_return": 0.0,
            "counterfactual_30m_return": 0.0,
            "price_5m": ref_p,
            "price_15m": ref_p,
            "price_30m": ref_p,
            "return_5m": 0.0,
            "return_15m": 0.0,
            "return_30m": 0.0,
            "is_evaluated": False,
            "reject_category": "MICRO_PROFIT_BLOCKED"
        }
        self.records.append(rec)
        return rec

    def update_price(self, symbol: str, current_price: float, current_time: datetime):
        """사후 가격 업데이트로 MFE/MAE/수익률 산출"""
        for rec in self.records:
            if rec["symbol"] == symbol and not rec["is_evaluated"]:
                ref_p = rec["ref_price"]
                if ref_p <= 0:
                    continue
                elapsed_sec = (current_time - rec["blocked_at"]).total_seconds()
                ret = (current_price - ref_p) / float(ref_p)
                rec["counterfactual_mfe"] = max(rec["counterfactual_mfe"], ret)
                rec["counterfactual_mae"] = min(rec["counterfactual_mae"], ret)
                rec["highest_price"] = max(rec.get("highest_price", ref_p), current_price)
                rec["lowest_price"] = min(rec.get("lowest_price", ref_p), current_price)
                rec["mfe_pct"] = rec["counterfactual_mfe"]
                rec["mae_pct"] = rec["counterfactual_mae"]

                if elapsed_sec >= 300 and rec["counterfactual_5m_return"] == 0.0:
                    rec["counterfactual_5m_return"] = ret
                    rec["price_5m"] = current_price
                    rec["return_5m"] = ret
                if elapsed_sec >= 900 and rec["counterfactual_15m_return"] == 0.0:
                    rec["counterfactual_15m_return"] = ret
                    rec["price_15m"] = current_price
                    rec["return_15m"] = ret
                if elapsed_sec >= 1800 and rec["counterfactual_30m_return"] == 0.0:
                    rec["counterfactual_30m_return"] = ret
                    rec["price_30m"] = current_price
                    rec["return_30m"] = ret
                    rec["is_evaluated"] = True
                    rec["status"] = "COMPLETED"

    def get_summary_stats(self) -> Dict[str, Any]:
        """[Section 5 & 11] 차단 거래 사후 성과 통계 집계"""
        def _avg(lst):
            return sum(lst) / len(lst) if lst else 0.0
        def _p(lst, q):
            if not lst:
                return 0.0
            s = sorted(lst)
            idx = int(len(s) * q)
            return s[min(idx, len(s) - 1)]

        count = len(self.records)
        mfes = [r["counterfactual_mfe"] for r in self.records]
        mfe_5ms = [r["counterfactual_5m_return"] for r in self.records]
        mfe_15ms = [r["counterfactual_15m_return"] for r in self.records]
        mfe_30ms = [r["counterfactual_30m_return"] for r in self.records]

        good_moves = sum(1 for m in mfes if m >= 0.015)
        good_move_ratio = (good_moves / count * 100.0) if count > 0 else 0.0

        return {
            "blocked_trade_count": count,
            "total_blocked": count,
            "completed_count": sum(1 for r in self.records if r.get("is_evaluated")),
            "avg_mfe": _avg(mfes),
            "avg_mfe_pct": _avg(mfes),
            "p50_mfe": _p(mfes, 0.50),
            "p95_mfe": _p(mfes, 0.95),
            "avg_mfe_5m": _avg(mfe_5ms),
            "avg_mfe_15m": _avg(mfe_15ms),
            "avg_mfe_30m": _avg(mfe_30ms),
            "good_moves": good_moves,
            "good_move_missed_count": good_moves,
            "good_move_ratio": good_move_ratio,
            "good_move_missed_ratio": (good_moves / count) if count > 0 else 0.0
        }


# -----------------------------------------------------------------------------
# Telemetry Collector (Section 76 & Section 11: 최종 일일 LIVE Telemetry)
# -----------------------------------------------------------------------------
class ProfitOpportunityTelemetry:
    """[Section 76 & Section 11] 최종 일일 LIVE 보고 텔레메트리 생성기"""
    _instance = None

    def __init__(self):
        self.reset()

    @classmethod
    def get_instance(cls):
        if cls._instance is None:
            cls._instance = cls()
        return cls._instance

    def reset(self):
        """내부 누적 지표 초기화 (테스트 격리용)"""
        self.buy_attempts: int = 0
        self.opportunity_pass: int = 0
        self.opportunity_reject: int = 0
        self.reject_counts: Dict[str, int] = {
            REJECT_LOW_EXPECTED_MOVE: 0,
            REJECT_MICRO_PROFIT: 0,
            REJECT_COST_TO_MOVE_RATIO_HIGH: 0,
            REJECT_EXPECTED_MFE_LOW: 0,
        }
        self.expected_moves: List[float] = []
        self.expected_net_rs: List[float] = []
        self.actual_mfes: List[float] = []
        self.actual_maes: List[float] = []
        self.holding_times: List[float] = []
        self.gross_moves: List[float] = []
        self.net_moves: List[float] = []
        self.costs: List[float] = []
        self.micro_trade_count: int = 0
        self.total_completed_trades: int = 0

        # Section 11 파이프라인 집계
        self.buy_approved_count: int = 0
        self.orders_sent_count: int = 0
        self.filled_count: int = 0

        # Entry Quality 지표
        self.entry_rvols: List[float] = []
        self.entry_mom_3ms: List[float] = []
        self.entry_vwap_gaps: List[float] = []
        self.entry_rebound_strengths: List[float] = []

        # Cancelled / No Trade 원인별 집계
        self.entry_quality_rejects: int = 0
        self.profit_opp_rejects: int = 0
        self.signal_expired_rejects: int = 0
        self.price_drift_rejects: int = 0
        self.insufficient_cash_rejects: int = 0

        # Reentry 지표 (상호 배타적 Disjoint Buckets)
        self.same_symbol_reentries: int = 0
        self.stop_reentry_lt_15m: int = 0
        self.stop_reentry_15_30m: int = 0
        self.stop_reentry_gte_30m: int = 0
        self.total_stop_reentries: int = 0
        self.same_symbol_3plus_entries: int = 0

        # Micro Trade 상세 지표
        self.strategy_micro_counts: Dict[str, int] = {}
        self.micro_trade_pnls: List[float] = []
        self.micro_trade_holding_times: List[float] = []
        self.micro_trade_gross_moves: List[float] = []
        self.micro_trade_net_moves: List[float] = []
        self.micro_trade_costs: List[float] = []

    @property
    def stop_reentry_lt_30m(self) -> int:
        """하위 호환성용: 30분 미만 누적 (disjoint < 15m + disjoint 15~30m)"""
        return self.stop_reentry_lt_15m + self.stop_reentry_15_30m

    @stop_reentry_lt_30m.setter
    def stop_reentry_lt_30m(self, val: int):
        self.stop_reentry_15_30m = max(0, val - self.stop_reentry_lt_15m)

    def record_attempt(self, passed: bool, reason: str, metrics: Dict[str, Any]):
        self.buy_attempts += 1
        if passed:
            self.opportunity_pass += 1
            if "expected_gross_move" in metrics:
                self.expected_moves.append(metrics["expected_gross_move"])
            if "expected_net_r" in metrics:
                self.expected_net_rs.append(metrics["expected_net_r"])
        else:
            self.opportunity_reject += 1
            self.profit_opp_rejects += 1
            for k in self.reject_counts:
                if k in reason:
                    self.reject_counts[k] += 1

    def record_buy_approved(
        self,
        rvol: float = 1.0,
        mom_3m: float = 0.0,
        vwap_gap: float = 0.0,
        rebound_strength: float = 0.50,
        count: int = 1
    ):
        self.buy_approved_count += count
        self.entry_rvols.append(rvol)
        self.entry_mom_3ms.append(mom_3m)
        self.entry_vwap_gaps.append(vwap_gap)
        self.entry_rebound_strengths.append(rebound_strength)

    def record_order_sent(self, count: int = 1):
        self.orders_sent_count += count

    def record_fill(self, count: int = 1):
        self.filled_count += count

    def record_gate_reject(self, gate: str, *args, **kwargs):
        g = str(gate).upper()
        if "ENTRY" in g or "TIMING" in g:
            self.entry_quality_rejects += 1
        elif "PROFIT" in g or "OPPORTUNITY" in g:
            self.profit_opp_rejects += 1
        elif "EXPIRED" in g or "TTL" in g:
            self.signal_expired_rejects += 1
        elif "DRIFT" in g or "SLIPPAGE" in g:
            self.price_drift_rejects += 1
        elif "CASH" in g or "SIZER" in g:
            self.insufficient_cash_rejects += 1

    def record_reentry_stats(
        self,
        same_symbol: int = 0,
        stop_lt_15m: int = 0,
        stop_15_30m: int = 0,
        stop_gte_30m: int = 0,
        same_3plus: int = 0,
        stop_lt_30m: Optional[int] = None,
        total_stop_reentries: Optional[int] = None,
        **kwargs
    ):
        """[Section 2 & 11] 재진입 통계 기록 (상호 배타적 Disjoint Buckets)"""
        self.same_symbol_reentries = same_symbol
        self.stop_reentry_lt_15m = stop_lt_15m

        # 하위 호환 처리: stop_lt_30m 인자가 전달된 경우 (< 15m 포함 누적치)
        if "stop_reentry_15_30m" in kwargs:
            self.stop_reentry_15_30m = kwargs["stop_reentry_15_30m"]
        elif stop_lt_30m is not None:
            self.stop_reentry_15_30m = max(0, stop_lt_30m - stop_lt_15m)
        else:
            self.stop_reentry_15_30m = stop_15_30m

        if "stop_reentry_gte_30m" in kwargs:
            self.stop_reentry_gte_30m = kwargs["stop_reentry_gte_30m"]
        else:
            self.stop_reentry_gte_30m = stop_gte_30m

        if "same_symbol_3plus_entries" in kwargs:
            self.same_symbol_3plus_entries = kwargs["same_symbol_3plus_entries"]
        else:
            self.same_symbol_3plus_entries = same_3plus

        self.total_stop_reentries = (
            total_stop_reentries if total_stop_reentries is not None
            else (self.stop_reentry_lt_15m + self.stop_reentry_15_30m + self.stop_reentry_gte_30m)
        )

    def record_trade_completion(
        self,
        *args,
        gross_move: float = 0.0,
        net_move: float = 0.0,
        cost: float = 0.0,
        holding_time_sec: float = 0.0,
        actual_mfe: float = 0.0,
        actual_mae: float = 0.0,
        pnl: float = 0.0,
        strategy_id: str = "DEFAULT",
        **kwargs
    ):
        if args:
            if isinstance(args[0], str):
                # Positional pattern: symbol, strategy, entry_price, exit_price, pnl, holding_sec, reason
                strategy_id = args[1] if len(args) > 1 else "DEFAULT"
                ep = float(args[2]) if len(args) > 2 else 0.0
                xp = float(args[3]) if len(args) > 3 else 0.0
                pnl = float(args[4]) if len(args) > 4 else 0.0
                holding_time_sec = float(args[5]) if len(args) > 5 else 0.0
                gross_move = abs(xp - ep) / ep if ep > 0 else 0.0
                net_move = (xp - ep) / ep if ep > 0 else 0.0
                cost = 0.0023
                actual_mfe = max(0.0, net_move)
                actual_mae = min(0.0, net_move)
            else:
                if len(args) > 0: gross_move = float(args[0])
                if len(args) > 1: net_move = float(args[1])
                if len(args) > 2: cost = float(args[2])
                if len(args) > 3: holding_time_sec = float(args[3])
                if len(args) > 4: actual_mfe = float(args[4])
                if len(args) > 5: actual_mae = float(args[5])
                if len(args) > 6: pnl = float(args[6])
                if len(args) > 7: strategy_id = str(args[7])

        if "strategy" in kwargs and strategy_id == "DEFAULT":
            strategy_id = kwargs["strategy"]
        if "holding_seconds" in kwargs and holding_time_sec == 0.0:
            holding_time_sec = float(kwargs["holding_seconds"])
        if "entry_price" in kwargs and "exit_price" in kwargs and gross_move == 0.0:
            ep = float(kwargs["entry_price"])
            xp = float(kwargs["exit_price"])
            if ep > 0:
                gross_move = abs(xp - ep) / ep
                net_move = (xp - ep) / ep
                if actual_mfe == 0.0:
                    actual_mfe = max(0.0, net_move)
                if actual_mae == 0.0:
                    actual_mae = min(0.0, net_move)
        if "pnl" in kwargs and pnl == 0.0:
            pnl = float(kwargs["pnl"])

        # Section 3 & 4: RESTART_RECOVERY 및 SCALE_OUT은 전략 완료 거래 및 Micro Trade 집계에서 원천 배제
        strat_str = str(strategy_id).upper()
        if (
            strat_str.startswith("RESTART_RECOVERY") or
            strat_str.startswith("SCALE_OUT") or
            kwargs.get("trade_classification") == "RESTART_RECOVERY"
        ):
            return

        self.total_completed_trades += 1
        self.gross_moves.append(gross_move)
        self.net_moves.append(net_move)
        self.costs.append(cost)
        self.holding_times.append(holding_time_sec)
        self.actual_mfes.append(actual_mfe)
        self.actual_maes.append(actual_mae)

        # Micro Trade Definition: gross move < 0.5% & holding time < 5m
        if gross_move < 0.0050 and holding_time_sec < 300:
            self.micro_trade_count += 1
            self.strategy_micro_counts[strategy_id] = self.strategy_micro_counts.get(strategy_id, 0) + 1
            self.micro_trade_pnls.append(pnl)
            self.micro_trade_holding_times.append(holding_time_sec)
            self.micro_trade_gross_moves.append(gross_move)
            self.micro_trade_net_moves.append(net_move)
            self.micro_trade_costs.append(cost)

    @property
    def micro_trade_ratio(self) -> float:
        """Section 6 & 11: Micro Trade Ratio = Micro Trade Count / Total Completed Trades"""
        if self.total_completed_trades <= 0:
            return 0.0
        return self.micro_trade_count / float(self.total_completed_trades)

    def validate_telemetry_consistency(self) -> Dict[str, Any]:
        """
        [Section 11 Cross-Validation] 수학적 정합성 교차 검증:
        1. 파이프라인 단조 감소성: BUY Approved >= Orders Sent >= Filled
        2. 재진입 버킷 정합성: stop_reentry_lt_15m + stop_reentry_15_30m + stop_reentry_gte_30m == total_stop_reentries
        3. Micro Trade 비율 정합성: 0.0 <= micro_trade_ratio <= 1.0 (and micro_trade_count <= total_completed_trades)
        """
        errors = []

        # 1. Pipeline Monotonicity
        if not (self.buy_approved_count >= self.orders_sent_count >= self.filled_count):
            errors.append(
                f"Pipeline order violation: Approved({self.buy_approved_count}) >= "
                f"Sent({self.orders_sent_count}) >= Filled({self.filled_count}) violated."
            )

        # 2. Reentry Disjoint Buckets Sum
        total_stop_reentry_buckets = self.stop_reentry_lt_15m + self.stop_reentry_15_30m + self.stop_reentry_gte_30m
        if self.total_stop_reentries is not None and self.total_stop_reentries > 0:
            if total_stop_reentry_buckets != self.total_stop_reentries:
                errors.append(
                    f"Reentry buckets sum mismatch: {self.stop_reentry_lt_15m} + {self.stop_reentry_15_30m} + "
                    f"{self.stop_reentry_gte_30m} = {total_stop_reentry_buckets} != {self.total_stop_reentries}"
                )

        # 3. Micro Trade Consistency
        if self.total_completed_trades > 0:
            if self.micro_trade_count > self.total_completed_trades:
                errors.append(
                    f"Micro trade count ({self.micro_trade_count}) exceeds "
                    f"total completed trades ({self.total_completed_trades})"
                )
            ratio = self.micro_trade_ratio
            if not (0.0 <= ratio <= 1.0):
                errors.append(f"Invalid micro_trade_ratio: {ratio}")

        return {
            "is_valid": len(errors) == 0,
            "errors": errors,
            "buy_approved_count": self.buy_approved_count,
            "orders_sent_count": self.orders_sent_count,
            "filled_count": self.filled_count,
            "total_completed_trades": self.total_completed_trades,
            "micro_trade_count": self.micro_trade_count,
            "micro_trade_ratio": self.micro_trade_ratio,
            "reentry_bucket_sum": total_stop_reentry_buckets
        }

    def generate_report_block(self, trading_mode: str = "LIVE") -> str:
        """Section 11 사양: 전체 7대 섹션 일일 LIVE 리포트 생성"""
        def _avg(lst):
            return sum(lst) / len(lst) if lst else 0.0
        def _p(lst, q):
            if not lst:
                return 0.0
            s = sorted(lst)
            idx = int(len(s) * q)
            return s[min(idx, len(s) - 1)]

        # 1. Entry Quality
        avg_rvol = _avg(self.entry_rvols)
        avg_3m_mom = _avg(self.entry_mom_3ms) * 100
        avg_vwap_gap = _avg(self.entry_vwap_gaps) * 100
        avg_rebound = _avg(self.entry_rebound_strengths)

        # 2. Profit Opportunity
        exp_avg = _avg(self.expected_moves) * 100
        nr_avg = _avg(self.expected_net_rs)
        cost_opp_avg = (
            _avg([c / max(0.0001, g) for c, g in zip(self.costs, self.gross_moves)]) * 100
            if self.costs and self.gross_moves else 15.0
        )

        # 5. Micro Trade
        micro_ratio = self.micro_trade_ratio
        avg_micro_pnl = _avg(self.micro_trade_pnls)
        avg_micro_hold = _avg(self.micro_trade_holding_times)

        # 6. Counterfactual
        cf_stats = CounterfactualTracker.get_instance().get_summary_stats()

        # 7. Completed Trade
        avg_mfe = _avg(self.actual_mfes) * 100
        avg_mae = _avg(self.actual_maes) * 100
        avg_hold = _avg(self.holding_times)
        avg_gross = _avg(self.gross_moves) * 100
        avg_net = _avg(self.net_moves) * 100
        avg_cost = _avg(self.costs) * 100

        mode_header = f"=== 나무퀀트 실시간 거래 텔레메트리 리포트 [{str(trading_mode).upper()}] ===\n"

        return f"""{mode_header}[ENTRY QUALITY]

BUY Approved: {self.buy_approved_count}
Orders Sent: {self.orders_sent_count}
Filled: {self.filled_count}

Average RVOL: {avg_rvol:.2f}
Average 3m Momentum: {avg_3m_mom:+.2f}%
Average VWAP Gap: {avg_vwap_gap:+.2f}%
Average Rebound Strength: {avg_rebound:.2f}

[PROFIT OPPORTUNITY]

Expected Move AVG: {exp_avg:+.2f}%
Expected Net R AVG: {nr_avg:.2f}R
Cost / Opportunity AVG: {cost_opp_avg:.1f}%

[CANCELLED / NO TRADE]

Entry Quality Reject: {self.entry_quality_rejects}
Profit Opportunity Reject: {self.profit_opp_rejects}
Signal Expired: {self.signal_expired_rejects}
Price Drift: {self.price_drift_rejects}
Insufficient Cash: {self.insufficient_cash_rejects}

[REENTRY]

Same Symbol Reentry: {self.same_symbol_reentries}
STOP → Reentry < 15m: {self.stop_reentry_lt_15m}
STOP → Reentry 15~30m: {self.stop_reentry_15_30m}
STOP → Reentry 30m+: {self.stop_reentry_gte_30m}
Same Symbol 3+ Entries: {self.same_symbol_3plus_entries}

[MICRO TRADE]

Micro Trade Count: {self.micro_trade_count}
Micro Trade Ratio: {micro_ratio * 100.0:.1f}%
Average Micro Trade PnL: {avg_micro_pnl:,.0f}원
Average Micro Trade Holding Time: {avg_micro_hold:.1f}s ({avg_micro_hold/60:.1f}m)

[COUNTERFACTUAL]

Blocked Trade Count: {cf_stats['blocked_trade_count']}
Blocked Trade Avg MFE 5m: {cf_stats['avg_mfe_5m']*100:+.2f}%
Blocked Trade Avg MFE 15m: {cf_stats['avg_mfe_15m']*100:+.2f}%
Blocked Trade Avg MFE 30m: {cf_stats['avg_mfe_30m']*100:+.2f}%
Blocked Trades Missed Good Move Ratio: {cf_stats['good_move_ratio']:.1f}% ({cf_stats['good_moves']}/{cf_stats['total_blocked']})

[COMPLETED TRADE]

Average MFE: {avg_mfe:+.2f}%
Average MAE: {avg_mae:+.2f}%
Average Holding Time: {avg_hold:.1f}s ({avg_hold/60:.1f}m)
Average Gross Move: {avg_gross:+.2f}%
Average Net Move: {avg_net:+.2f}%
Average Cost: {avg_cost:.2f}%
"""


# 모듈 레벨 편리 래퍼 함수 (Section 59)
validate_profit_opportunity = ProfitOpportunityGate.validate_profit_opportunity
