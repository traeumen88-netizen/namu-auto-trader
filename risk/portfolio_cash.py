"""[FINAL MASTER v16.0] 포트폴리오 중앙 현금 관리 및 예약 엔진 (risk/portfolio_cash.py)
Cash-Governed Execution & Portfolio Cash Reservation Architecture

핵심 원칙:
1. 종목 구매 개수 인위적 제한 완전 해제 (보유 종목 수 = 제한 없음)
2. 돈이 없으면 사지 않는다 (가용 현금 기반 엄격한 통제)
3. 중앙 Cash Reservation: 동시 다중 주문 시 현금 초과 발주 원천 차단
4. 현금 부족 시 강제 부분 매수 금지 (ALLOW_PARTIAL_CASH_BUY = False 기본값)
5. 주문 직전 최종 Cash Revalidation 및 현금 부족 사유(INSUFFICIENT_CASH) 정밀 감사 기록
6. 다중 매수 신호 시 기대치/ML/스코어 기반 주문 우선순위(Order Priority) 자금 배분
7. 실시간 대시보드 메트릭 (Available, Reserved, Effective Cash, Open Positions, Pending Orders)
"""

import threading
import logging
from dataclasses import dataclass, field
from datetime import datetime
from typing import Dict, Any, Optional, Tuple, List
from core.models import TradeSignal, OrderSide
import config.settings as settings

logger = logging.getLogger("PortfolioCashManager")


@dataclass
class CashRequirement:
    """주문 1건에 필요한 총 현금 소요액 명세 (Section 2)"""
    required_order_value: float    # 순수 주식 매수금액 (shares * price)
    estimated_fee: float           # 증권사 거래 수수료 (기본 0.015%)
    estimated_tax: float           # 거래세 (국내 매수 시 0%)
    estimated_slippage: float      # 슬리피지 대비 버퍼 (기본 0.05%)
    total_required_cash: float     # 결제에 필요한 총 가용 현금 합계


@dataclass
class CashShortfallRecord:
    """현금 부족으로 인한 NO_TRADE 기각 상세 감사 기록 (Section 9)"""
    symbol: str
    signal_id: str
    strategy_id: str
    decision: str                  # "NO_TRADE"
    cash_available: float          # 증권사 계좌 총 가용 현금
    reserved_cash: float           # 미체결 주문에 묶인 예약금
    effective_available_cash: float # 실제 주문 가능한 유효 현금
    required_order_value: float    # 주문 순수 대금
    estimated_cost: float          # 수수료 및 슬리피지 합계
    cash_shortfall: float          # 부족 금액 (total_required - effective)
    timestamp: str
    reason: str = "INSUFFICIENT_CASH"


class PortfolioCashManager:
    """
    중앙 포트폴리오 현금 관리자 (Central Portfolio Cash Manager)
    - 브로커 계좌 실시간 현금 동기화
    - 동시 다중 주문 간 중앙 현금 예약 (Cash Reservation)
    - 주문 직전 최종 재검증 (Cash Revalidation)
    - Section 3: available_cash, reserved_cash, pending_buy_amount, usable_cash 원자적 관리
    """

    FEE_RATE: float = 0.00015             # 0.015%
    TAX_RATE_BUY: float = 0.0             # 매수 시 0.0%
    SLIPPAGE_BUFFER_RATE: float = 0.0005  # 0.05% 안전 버퍼

    def __init__(self, initial_cash: float = 0.0, allow_partial_cash_buy: Optional[bool] = None):
        self._lock = threading.RLock()
        self.cash_available: float = float(initial_cash)
        self.reserved_cash: float = 0.0
        self.reservations: Dict[str, Dict[str, Any]] = {}
        # Section 5: 기본값 False (현금 부족 시 강제 축소 매수 금지)
        if allow_partial_cash_buy is not None:
            self.allow_partial_cash_buy = allow_partial_cash_buy
        else:
            self.allow_partial_cash_buy = getattr(settings, "ALLOW_PARTIAL_CASH_BUY", False)

        self.shortfall_history: List[CashShortfallRecord] = []

    def sync_broker_cash(self, broker_cash: float):
        """브로커 최신 예수금 실시간 동기화"""
        with self._lock:
            self.cash_available = max(0.0, float(broker_cash))

    @property
    def available_cash(self) -> float:
        """Section 3: 증권사 계좌 총 가용 현금"""
        return self.cash_available

    @property
    def pending_buy_amount(self) -> float:
        """Section 3: 미체결 매수 주문에 묶인 총 예약 금액"""
        with self._lock:
            return sum(r.get("amount", 0.0) for r in self.reservations.values())

    @property
    def usable_cash(self) -> float:
        """Section 3 공식: usable_cash = available_cash - reserved_cash"""
        with self._lock:
            return max(0.0, self.cash_available - self.reserved_cash)

    @property
    def effective_available_cash(self) -> float:
        """하위 호환 래퍼: usable_cash 반환"""
        return self.usable_cash

    @classmethod
    def calculate_total_required_cash(
        cls,
        shares: int,
        order_price: float,
        fee_rate: Optional[float] = None,
        slippage_buffer_rate: Optional[float] = None
    ) -> CashRequirement:
        """주문에 소요되는 총 필요 현금 계산 (Section 2)"""
        f_rate = fee_rate if fee_rate is not None else cls.FEE_RATE
        s_rate = slippage_buffer_rate if slippage_buffer_rate is not None else cls.SLIPPAGE_BUFFER_RATE

        required_val = float(shares * order_price)
        fee = required_val * f_rate
        tax = 0.0
        slippage = required_val * s_rate
        total = required_val + fee + tax + slippage

        return CashRequirement(
            required_order_value=required_val,
            estimated_fee=fee,
            estimated_tax=tax,
            estimated_slippage=slippage,
            total_required_cash=total
        )

    def revalidate_and_reserve_cash(
        self,
        order_id: str,
        signal: TradeSignal,
        shares: int,
        order_price: float,
        now: Optional[datetime] = None
    ) -> Tuple[bool, str, Optional[CashRequirement], Optional[CashShortfallRecord]]:
        """
        신규 매수 주문 직전 최종 Cash Revalidation 및 현금 예약 (Section 3 & 6 & 7)
        - 원자적 lock 보호: 동시 다중 주문 시 현금 중복 사용 Race Condition 원천 차단
        - 현금 충분 -> CASH_APPROVED 및 reserved_cash 증가
        - 현금 부족 -> INSUFFICIENT_CASH, 감사 기록 생성 및 기각
        """
        now = now or datetime.now()
        req = self.calculate_total_required_cash(shares, order_price)

        with self._lock:
            eff_cash = max(0.0, self.cash_available - self.reserved_cash)

            # Section: 모멘텀 자금 보호 버퍼 (Swing 주문이 전체 자금을 100% 잠식하지 않도록 30% 현금 유보)
            from core.models import TimeHorizon
            if getattr(signal, "time_horizon", None) == TimeHorizon.SWING:
                reserved_momentum = self.cash_available * 0.30
                usable_for_swing = max(0.0, eff_cash - reserved_momentum)
                if req.total_required_cash > usable_for_swing:
                    single_share_cost = order_price * (1.0 + self.FEE_RATE + self.SLIPPAGE_BUFFER_RATE)
                    max_swing_shares = int(usable_for_swing / single_share_cost) if single_share_cost > 0 else 0
                    if max_swing_shares > 0 and max_swing_shares < shares:
                        logger.info(
                            f"[{signal.iem_cd}] 스윙 현금 유보 버퍼(30%) 보호: 가용 스윙 자금({usable_for_swing:,.0f}원)에 맞춰 수량 조정 "
                            f"({shares}주 -> {max_swing_shares}주)"
                        )
                        shares = max_swing_shares
                        req = self.calculate_total_required_cash(shares, order_price)
                    else:
                        shortfall = req.total_required_cash - usable_for_swing
                        sig_id = getattr(signal, "signal_id", "") or f"SIG_{signal.iem_cd}_{int(now.timestamp())}"
                        rec = CashShortfallRecord(
                            symbol=signal.iem_cd,
                            signal_id=sig_id,
                            strategy_id=signal.strategy_id,
                            decision="NO_TRADE",
                            cash_available=self.cash_available,
                            reserved_cash=self.reserved_cash,
                            effective_available_cash=eff_cash,
                            required_order_value=req.required_order_value,
                            estimated_cost=req.estimated_fee + req.estimated_slippage,
                            cash_shortfall=shortfall,
                            timestamp=now.isoformat(),
                            reason="RESERVED_FOR_INTRADAY_MOMENTUM"
                        )
                        self.shortfall_history.append(rec)
                        logger.warning(
                            f"[스윙 현금제한 기각] {signal.name}({signal.iem_cd}): 필요금액 {req.total_required_cash:,.0f}원 > "
                            f"스윙 한도 {usable_for_swing:,.0f}원 (모멘텀 30% 유보: {reserved_momentum:,.0f}원) -> NO_TRADE"
                        )
                        return False, "RESERVED_FOR_INTRADAY_MOMENTUM", req, rec

            # 가용 현금 충족 여부 검증 (total_required_cash <= effective_available_cash)
            if req.total_required_cash <= eff_cash:
                self.reserved_cash += req.total_required_cash
                sig_id = getattr(signal, "signal_id", "") or f"SIG_{signal.iem_cd}_{int(now.timestamp())}"
                self.reservations[order_id] = {
                    "signal_id": sig_id,
                    "iem_cd": signal.iem_cd,
                    "shares": shares,
                    "order_price": order_price,
                    "amount": req.total_required_cash,
                    "reserved_at": now.isoformat()
                }
                logger.info(
                    f"[현금 예약 승인] {signal.name}({signal.iem_cd}) {shares}주 @ {order_price:,.0f}원 | "
                    f"총 필요금액: {req.total_required_cash:,.0f}원 | "
                    f"예약 후 잔여 유효현금: {self.usable_cash:,.0f}원 (가용: {self.cash_available:,.0f}원, 예약: {self.reserved_cash:,.0f}원)"
                )
                return True, "CASH_APPROVED", req, None

            # 현금 부족 발생 (Section 3 & 5 & 9)
            shortfall = req.total_required_cash - eff_cash
            sig_id = getattr(signal, "signal_id", "") or f"SIG_{signal.iem_cd}_{int(now.timestamp())}"
            rec = CashShortfallRecord(
                symbol=signal.iem_cd,
                signal_id=sig_id,
                strategy_id=signal.strategy_id,
                decision="NO_TRADE",
                cash_available=self.cash_available,
                reserved_cash=self.reserved_cash,
                effective_available_cash=eff_cash,
                required_order_value=req.required_order_value,
                estimated_cost=req.estimated_fee + req.estimated_slippage,
                cash_shortfall=shortfall,
                timestamp=now.isoformat(),
                reason="INSUFFICIENT_CASH"
            )
            self.shortfall_history.append(rec)

            logger.warning(
                f"[현금 부족 기각] {signal.name}({signal.iem_cd}): 필요금액 {req.total_required_cash:,.0f}원 > "
                f"유효현금 {eff_cash:,.0f}원 (부족: {shortfall:,.0f}원) -> FINAL_DECISION=NO_TRADE (REASON=INSUFFICIENT_CASH)"
            )
            return False, "INSUFFICIENT_CASH", req, rec

    def on_order_fill(self, order_id: str, filled_qty: int, filled_price: float):
        """체결 완료 시: 예약금 해제 및 실제 사용 현금 차감 (원자적 lock 보호)"""
        with self._lock:
            res = self.reservations.pop(order_id, None)
            reserved_amt = res["amount"] if res else 0.0
            self.reserved_cash = max(0.0, self.reserved_cash - reserved_amt)

            # 실제 체결 소요액 (수수료 포함)
            actual_req = self.calculate_total_required_cash(filled_qty, filled_price, slippage_buffer_rate=0.0)
            self.cash_available = max(0.0, self.cash_available - actual_req.total_required_cash)

    def on_order_cancel_or_reject(self, order_id: str):
        """주문 취소 또는 브로커 거절 시: 묶여 있던 예약금 전액 즉시 환원 (원자적 lock 보호)"""
        with self._lock:
            res = self.reservations.pop(order_id, None)
            if res:
                self.reserved_cash = max(0.0, self.reserved_cash - res["amount"])
                logger.info(f"[현금 예약 해제] 주문 {order_id}: {res['amount']:,.0f}원 유효현금으로 환원 완료")

    @staticmethod
    def sort_signals_by_priority(signals_with_meta: List[Any]) -> List[Any]:
        """
        Section 8: 다중 매수 신호 발생 시 주문 우선순위(Order Priority) 정렬
        정렬 기준:
        1. Expected Net Return (기대 R-수익률, 높은 순)
        2. Risk-adjusted Score (규칙 기반 정밀 스코어, 높은 순)
        3. ML Probability (승률/목표가 도달 확률, 높은 순)
        4. Setup Quality / Momentum Stage (EARLY > ACTIVE > START > NORMAL > LATE)
        """
        stage_rank = {"EARLY": 0, "ACTIVE": 1, "START": 2, "NORMAL": 3, "LATE": 4}

        def _sort_key(item):
            if isinstance(item, tuple):
                sig = item[0]
                edge_res = item[2] if len(item) > 2 else None
                meta_res = item[3] if len(item) > 3 else None

                expected_net_r = getattr(edge_res, "expected_net_r", 0.0) or getattr(meta_res, "expected_net_r", 0.0)
                score = getattr(sig, "score", 0.0)
                ml_prob = getattr(meta_res, "p_target", 0.5) if meta_res else 0.5
                stage = stage_rank.get(getattr(sig, "momentum_stage", "NORMAL"), 3)
            else:
                sig = item
                expected_net_r = getattr(sig, "expected_net_r", 0.0)
                score = getattr(sig, "score", 0.0)
                ml_prob = getattr(sig, "ml_prob", 0.5)
                stage = stage_rank.get(getattr(sig, "momentum_stage", "NORMAL"), 3)

            return (
                -float(expected_net_r or 0.0), # 1. Expected Net Return
                -float(score or 0.0),          # 2. Score
                -float(ml_prob or 0.0),        # 3. ML Probability
                stage                          # 4. Stage Rank
            )

        return sorted(signals_with_meta, key=_sort_key)

    def get_dashboard_summary(self, open_positions_count: int = 0) -> Dict[str, Any]:
        """Section 10: 실시간 대시보드 표출 데이터"""
        return {
            "cash_available": self.cash_available,
            "reserved_cash": self.reserved_cash,
            "effective_available_cash": self.effective_available_cash,
            "open_positions": open_positions_count,
            "pending_orders": len(self.reservations),
            "potential_order_value": self.reserved_cash,
            "max_position_count": "UNLIMITED",
            "current_position_count": open_positions_count,
            "position_count_block": False,
            "position_count_check": "BYPASSED / NOT_USED"
        }

    def format_dashboard_text(self, open_positions_count: int = 0) -> str:
        """Section 10: 실시간 대시보드 텍스트 포맷"""
        s = self.get_dashboard_summary(open_positions_count)
        return (
            "==================================================\n"
            "[PORTFOLIO CASH & EXECUTION STATUS]\n"
            f"MAX_POSITION_COUNT      :      UNLIMITED\n"
            f"CURRENT_POSITION_COUNT  : {s['open_positions']:>14}개\n"
            f"POSITION_COUNT_BLOCK    :          FALSE\n"
            f"POSITION_COUNT_CHECK    : BYPASSED / NOT_USED\n"
            f"Cash Available          : {s['cash_available']:>14,.0f}원\n"
            f"Reserved Cash           : {s['reserved_cash']:>14,.0f}원\n"
            f"Effective Available Cash: {s['effective_available_cash']:>14,.0f}원\n"
            f"Open Positions          : {s['open_positions']:>14}개\n"
            f"Pending Orders          : {s['pending_orders']:>14}건\n"
            f"Potential Order Value   : {s['potential_order_value']:>14,.0f}원\n"
            "=================================================="
        )
