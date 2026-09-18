"""[FINAL MASTER v16.0] Exit Reason별 Order Type 매핑 및 실행 정책 엔진
execution/exit_order_policy.py

핵심 원칙:
1. 수익 실현 계열 (TARGET_EXIT, SCALE_OUT, TARGET_2_EXIT):
   - 가격 우선: LIMIT 지정가 우선 -> 미체결 시 ORDER_TIMEOUT 후 재평가 -> AGGRESSIVE_LIMIT 전환
2. 위험 회피 계열 (HARD_STOP, TRAILING_STOP, TREND_BREAK, EMERGENCY, CIRCUIT_BREAKER):
   - 체결 우선: MARKET 시장가 우선 -> 호가 급변 또는 예외 시 AGGRESSIVE_LIMIT fallback
   - Aggressive Limit: 최우선 매수호가(Bid)에서 설정된 N Tick(기본: STOP_AGGRESSIVE_TICKS = 3) 불리하게 적용하여 즉각 체결 유도
3. TIME_STOP:
   - 기회비용 제거: LIMIT 우선 -> 일정 시간 미체결 시 AGGRESSIVE_LIMIT 전환
4. END_OF_DAY:
   - 장마감(15:10/15:20) 단타 강제 청산: MARKET 시장가 체결 우선
5. Exit 주문 실행 상태 머신:
   - EXIT_TRIGGERED -> EXIT_ORDER_CREATED -> EXIT_ORDER_SENT -> EXIT_ORDER_ACK -> PARTIAL_FILL / FILLED
   - 동일 포지션 중복 Exit 주문 방지: active_exit_order_id 중앙 관리
6. 주문 방식 결정 감사 로그 (Decision Audit Trail):
   - exit_reason, selected_order_type, fallback_order_type, reference_price, limit_price,
     aggressive_ticks, bid, ask, spread, order_quantity, timestamp 영구 기록
"""

import os
import logging
from enum import Enum
from dataclasses import dataclass, field
from datetime import datetime
from typing import Dict, Any, Optional, Tuple

from core.models import OrderType, Position
from core.tick_normalizer import get_tick_size, normalize_price
import config.settings as settings

logger = logging.getLogger("ExitOrderPolicy")


class ExitReason(str, Enum):
    # 1. 수익 실현 계열 (Profit-Taking, 가격 우선)
    TARGET_EXIT = "TARGET_EXIT"
    SCALE_OUT = "SCALE_OUT"
    TARGET_2_EXIT = "TARGET_2_EXIT"

    # 2. 위험 회피 계열 (Risk-Avoidance, 체결 우선)
    HARD_STOP = "HARD_STOP"
    TRAILING_STOP = "TRAILING_STOP"
    TREND_BREAK = "TREND_BREAK"
    EMERGENCY = "EMERGENCY"
    CIRCUIT_BREAKER = "CIRCUIT_BREAKER"

    # 3. 시간 및 장마감 청산
    TIME_STOP = "TIME_STOP"
    END_OF_DAY = "END_OF_DAY"


@dataclass
class ExitOrderPlan:
    """Exit 주문 결정 세부 명세 및 감사 기록 (Section 8)"""
    exit_reason: ExitReason
    selected_order_type: OrderType
    fallback_order_type: OrderType
    reference_price: float
    limit_price: float
    aggressive_ticks: int
    bid: float
    ask: float
    spread: float
    order_quantity: int
    timestamp: str
    description: str


class ExitOrderPolicyEngine:
    """Exit Reason별 Order Type 매핑 및 동적 호가 산출 엔진"""

    # Section 5: Order Type Mapping Table
    POLICY_MAP: Dict[ExitReason, Dict[str, OrderType]] = {
        # 수익 실현 계열 (가격 우선)
        ExitReason.TARGET_EXIT:     {"primary": OrderType.LIMIT,  "fallback": OrderType.AGGRESSIVE_LIMIT},
        ExitReason.SCALE_OUT:       {"primary": OrderType.LIMIT,  "fallback": OrderType.AGGRESSIVE_LIMIT},
        ExitReason.TARGET_2_EXIT:   {"primary": OrderType.LIMIT,  "fallback": OrderType.AGGRESSIVE_LIMIT},

        # 위험 회피 계열 (체결 우선)
        ExitReason.HARD_STOP:       {"primary": OrderType.MARKET, "fallback": OrderType.AGGRESSIVE_LIMIT},
        ExitReason.TRAILING_STOP:   {"primary": OrderType.MARKET, "fallback": OrderType.AGGRESSIVE_LIMIT},
        ExitReason.TREND_BREAK:     {"primary": OrderType.MARKET, "fallback": OrderType.AGGRESSIVE_LIMIT},
        ExitReason.EMERGENCY:       {"primary": OrderType.MARKET, "fallback": OrderType.AGGRESSIVE_LIMIT},
        ExitReason.CIRCUIT_BREAKER: {"primary": OrderType.MARKET, "fallback": OrderType.AGGRESSIVE_LIMIT},

        # 시간 및 장마감
        ExitReason.TIME_STOP:       {"primary": OrderType.LIMIT,  "fallback": OrderType.AGGRESSIVE_LIMIT},
        ExitReason.END_OF_DAY:      {"primary": OrderType.MARKET, "fallback": OrderType.AGGRESSIVE_LIMIT},
    }

    @classmethod
    def parse_exit_reason(cls, reason_str: str) -> ExitReason:
        """사유 문자열로부터 정규화된 ExitReason 파싱"""
        r_upper = reason_str.upper()
        if "HARD_STOP" in r_upper or "스톱로스" in r_upper or "손절" in r_upper or "STOP_LOSS" in r_upper:
            return ExitReason.HARD_STOP
        elif "TRAILING" in r_upper:
            return ExitReason.TRAILING_STOP
        elif "SCALE_OUT" in r_upper or "+1R" in r_upper or "1차 익절" in r_upper:
            return ExitReason.SCALE_OUT
        elif "TARGET_2" in r_upper or "+2R" in r_upper or "2차 익절" in r_upper:
            return ExitReason.TARGET_2_EXIT
        elif "TARGET_3" in r_upper or "+3R" in r_upper or "3차 익절" in r_upper or "TARGET" in r_upper or "익절" in r_upper:
            return ExitReason.TARGET_EXIT
        elif "TREND" in r_upper or "추세" in r_upper or "EMA" in r_upper or "MA20" in r_upper:
            return ExitReason.TREND_BREAK
        elif "CIRCUIT" in r_upper or "서킷" in r_upper:
            return ExitReason.CIRCUIT_BREAKER
        elif "EMERGENCY" in r_upper or "긴급" in r_upper:
            return ExitReason.EMERGENCY
        elif "TIME" in r_upper or "시간" in r_upper:
            return ExitReason.TIME_STOP
        elif "장마감" in r_upper or "15:20" in r_upper or "15:10" in r_upper or "END_OF_DAY" in r_upper or "CLOSE" in r_upper:
            return ExitReason.END_OF_DAY
        return ExitReason.HARD_STOP  # 안전 기본값: 체결 우선

    @classmethod
    def calculate_aggressive_limit_price(
        cls,
        current_price: float,
        bid: Optional[float] = None,
        aggressive_ticks: Optional[int] = None
    ) -> float:
        """
        Section 2: 최우선 매수호가(Bid) 기준 일정 Tick 불리하게 적용하여 즉시 체결 가격 산출
        """
        ticks = aggressive_ticks if aggressive_ticks is not None else getattr(settings, "STOP_AGGRESSIVE_TICKS", 3)
        ref_bid = bid if (bid is not None and bid > 0) else current_price
        tick_size = get_tick_size(ref_bid)

        # 매도 시 즉시 체결을 위해 매수호가보다 ticks 단계 아래로 던짐
        raw_price = ref_bid - (ticks * tick_size)
        return float(normalize_price(max(tick_size, raw_price), "SELL", "STOP"))

    @classmethod
    def determine_exit_plan(
        cls,
        exit_reason: Any,
        qty: int,
        current_price: float,
        bid: Optional[float] = None,
        ask: Optional[float] = None,
        is_fallback: bool = False,
        now: Optional[datetime] = None
    ) -> ExitOrderPlan:
        """
        Section 5 & 8: Exit Reason에 따른 최적 주문 유형 및 가격 산출과 결정 감사 로그 생성
        """
        now = now or datetime.now()
        e_reason = exit_reason if isinstance(exit_reason, ExitReason) else cls.parse_exit_reason(str(exit_reason))
        mapping = cls.POLICY_MAP.get(e_reason, {"primary": OrderType.MARKET, "fallback": OrderType.AGGRESSIVE_LIMIT})

        primary_type = mapping["primary"]
        fallback_type = mapping["fallback"]
        selected_type = fallback_type if is_fallback else primary_type

        ref_bid = bid if (bid is not None and bid > 0) else current_price
        ref_ask = ask if (ask is not None and ask > 0) else current_price
        spread = max(0.0, ref_ask - ref_bid)
        ticks = getattr(settings, "STOP_AGGRESSIVE_TICKS", 3)

        if selected_type == OrderType.MARKET:
            limit_price = 0.0
            desc = f"{e_reason.value}: 체결 우선 시장가(MARKET) 청산"
        elif selected_type == OrderType.AGGRESSIVE_LIMIT:
            limit_price = cls.calculate_aggressive_limit_price(current_price, bid=ref_bid, aggressive_ticks=ticks)
            desc = f"{e_reason.value}: 체결 우선 공격적 지정가(AGGRESSIVE_LIMIT, Bid-{ticks}Tick={limit_price:,.0f}원)"
        else:  # LIMIT (수익 실현 / 가격 우선)
            limit_price = float(normalize_price(current_price, "SELL", "PROFIT"))
            desc = f"{e_reason.value}: 가격 우선 지정가(LIMIT, {limit_price:,.0f}원)"

        plan = ExitOrderPlan(
            exit_reason=e_reason,
            selected_order_type=selected_type,
            fallback_order_type=fallback_type,
            reference_price=current_price,
            limit_price=limit_price,
            aggressive_ticks=ticks,
            bid=ref_bid,
            ask=ref_ask,
            spread=spread,
            order_quantity=qty,
            timestamp=now.isoformat(),
            description=desc
        )

        logger.info(
            f"[Exit 주문 방식 결정] {desc} | Qty={qty}주 | RefPrice={current_price:,.0f}원 | "
            f"Bid={ref_bid:,.0f}원, Ask={ref_ask:,.0f}원, Spread={spread:,.0f}원"
        )
        return plan
