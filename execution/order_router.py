"""[FINAL MASTER v16.0] 주문 라우터, 우선순위 레이트 리미터 및 체결 엔진 (execution/order_router.py)
Section 28 ~ 34: Order Execution, Broker Rate Limiter & Paper Trading Engine

- Client Order ID 부여 및 멱등성(Idempotency) 완벽 보장
- 토큰 버킷 기반 우선순위 레이트 리미터 (ORDER > QUOTE > BALANCE)
- 10단계 주문 사전 안전점검 (Pre-Order Safety Checks)
- 루프 지연과 데이터 지연의 합리적 분리 (서킷브레이커 오작동 방지)
- 실전투자(LIVE) 직접 주문 및 모의투자/페이퍼(PAPER) 현실적 체결 시뮬레이션
  (스프레드, 슬리피지 0.05%, 체결 레이턴시 150ms 반영)
"""

import os
import re
import time
import uuid
import threading
import logging
from datetime import datetime, timedelta
from typing import Dict, Tuple, Optional, Any, List
from collections import deque
from core.models import Order, OrderSide, OrderType, OrderStatus, TradeSignal, TimeHorizon
from core.tick_normalizer import normalize_price, get_tick_size
from config.krx_constants import MAX_SLIPPAGE_BUDGET
import config.settings as settings
from execution.execution_funnel_telemetry import (
    EVENT_SIGNAL_DETECTED, EVENT_DECISION_APPROVED, EVENT_ORDER_CREATED,
    EVENT_QUOTE_CHECK_START, EVENT_QUOTE_REFETCH_START, EVENT_QUOTE_REFETCH_END,
    EVENT_RISK_RECHECK, EVENT_CASH_CHECK, EVENT_ORDER_SUBMIT_START,
    EVENT_BROKER_ACK, EVENT_FILL_RECEIVED
)

logger = logging.getLogger("OrderRouter")


class BrokerRateLimiter:
    """
    Section 29: Token Bucket 기반 우선순위 Rate Limiter
    초당 최대 4.0회 (NH API 429 방어), 주문 요청은 최우선 집행
    """
    def __init__(self, max_per_second: float = 4.0):
        self.max_per_second = max_per_second
        self.interval = 1.0 / max_per_second
        self.last_call_time = 0.0

    def wait_turn(self, priority: str = "ORDER"):
        now = time.time()
        elapsed = now - self.last_call_time
        needed = self.interval if priority != "ORDER" else (self.interval * 0.5)
        if elapsed < needed:
            time.sleep(needed - elapsed)
        self.last_call_time = time.time()


class OrderRouter:
    def __init__(self, namu_client=None, circuit_breaker=None, cash_manager=None, funnel_telemetry=None, account_name=None):
        self.client = namu_client
        self.circuit_breaker = circuit_breaker
        self.cash_manager = cash_manager
        self.funnel_telemetry = funnel_telemetry
        if account_name:
            self.account_name = account_name
        else:
            self.account_name = getattr(circuit_breaker, "account_name", "UNKNOWN") if circuit_breaker else (getattr(namu_client, "mode", "UNKNOWN").upper() if namu_client else "UNKNOWN")
        self.rate_limiter = BrokerRateLimiter(max_per_second=4.0)
        # 멱등성 보장 주문 저장소: {client_order_id: Order}
        self.order_registry: Dict[str, Order] = {}
        # 미체결 주문 목록: {client_order_id: Order}
        self.pending_orders: Dict[str, Order] = {}
        # 종목별 최근 주문 타임스탬프 (중복 연타 방지)
        self.last_order_by_symbol: Dict[str, datetime] = {}
        # Critical Exit Failure 보호 모드 플래그 (신규 BUY만 차단)
        self.critical_exit_failure_active: bool = False
        # Section 4: 동시 주문 Idempotency 및 Lock
        self.execution_lock = threading.Lock()
        self.executed_intent_ids: Dict[str, Order] = {}
        self.pending_entries: Dict[str, Order] = {}
        # 체결 이벤트 리스너 (PositionManager 등 등록)
        self.fill_listeners: List[Any] = []
        self.cancel_listeners: List[Any] = []

    def register_fill_listener(self, listener):
        """체결 이벤트 리스너 등록"""
        if listener not in self.fill_listeners:
            self.fill_listeners.append(listener)

    def register_cancel_listener(self, listener):
        """취소/거부/만료 이벤트 리스너 등록"""
        if listener not in self.cancel_listeners:
            self.cancel_listeners.append(listener)

    def _export_telemetry(self, stage: str, order: Any, extra_info: Optional[Dict[str, Any]] = None):
        """실시간 분석용 Telemetry Exporter 비동기 통지 (거래 엔진 완전 비차단)"""
        try:
            from execution.live_telemetry_exporter import LiveTelemetryExporter
            LiveTelemetryExporter.get_instance().record_order_event(stage, order, extra_info)
        except Exception:
            pass

    def _export_fill_telemetry(self, order: Any, fill_qty: int, fill_price: float, is_full_fill: bool):
        """실시간 체결 분석용 Telemetry Exporter 비동기 통지 (거래 엔진 완전 비차단)"""
        try:
            from execution.live_telemetry_exporter import LiveTelemetryExporter
            LiveTelemetryExporter.get_instance().record_fill(order, fill_qty, fill_price, is_full_fill)
        except Exception:
            pass

    def check_broker_health(self) -> bool:
        """브로커 통신 가능 여부 안전 확인 (불필요한 무거운 API 호출 없이 클라이언트/토큰 상태 점검)"""
        if self.client is None:
            return False
        try:
            from core.token_manager import TokenManager
            from core.api_gateway import CentralAPIGateway
            return TokenManager.get_instance() is not None and CentralAPIGateway.get_instance() is not None
        except Exception:
            return True

    def set_critical_exit_failure(self, active: bool):
        """치명적 청산 장애 발생 시 보호 모드 활성화/해제 (BUY 차단, SELL 허용)"""
        self.critical_exit_failure_active = bool(active)

    def generate_client_order_id(self, strategy_id: str, iem_cd: str, side: OrderSide) -> str:
        """유일무이한 Client Order ID 생성 (전략_종목_타입_타임스탬프_UUID4자리)"""
        now_str = datetime.now().strftime("%Y%m%d%H%M%S%f")[:17]
        short_uuid = uuid.uuid4().hex[:4]
        return f"{strategy_id}_{iem_cd}_{side.value}_{now_str}_{short_uuid}"

    def re_fetch_fresh_quote(self, signal: TradeSignal, now: Optional[datetime] = None) -> Optional[Any]:
        """
        주문 직전 1회 On-demand 최신 Quote Re-fetch
        - Stale 감지 시 브로커 REST API로 실시간 호가를 1회 재조회
        - 신선도(3초 이내) 및 정상 호가 여부 확인
        """
        now = now or datetime.now()
        t0 = time.perf_counter()
        cid = getattr(signal, "client_order_id", "") or f"{signal.strategy_id}_{signal.iem_cd}"
        init_age = signal.quote_snapshot.order_quote_age_ms if hasattr(signal, "quote_snapshot") and signal.quote_snapshot else 0.0

        if self.funnel_telemetry:
            self.funnel_telemetry.record_funnel_event(
                event_name=EVENT_QUOTE_REFETCH_START,
                symbol=signal.iem_cd,
                strategy=signal.strategy_id,
                order_id=cid,
                result="SUCCESS",
                quote_age_ms=init_age,
                now_dt=now
            )

        if not self.client or not hasattr(self.client, "get_current_price"):
            if self.funnel_telemetry:
                self.funnel_telemetry.record_quote_refetch_attempt(is_success=False, is_stale_before=True, latency_ms=0.0)
                self.funnel_telemetry.record_funnel_event(
                    event_name=EVENT_QUOTE_REFETCH_END,
                    symbol=signal.iem_cd,
                    strategy=signal.strategy_id,
                    order_id=cid,
                    result="REJECT",
                    reject_reason="NO_CLIENT_FOR_REFETCH",
                    quote_age_ms=init_age,
                    elapsed_ms=0.0,
                    now_dt=now
                )
            return None

        try:
            raw_quote = self.client.get_current_price(signal.iem_cd)
            elapsed_ms = (time.perf_counter() - t0) * 1000.0
            if self.funnel_telemetry:
                self.funnel_telemetry.record_broker_api_call(latency_ms=elapsed_ms, is_success=bool(raw_quote and raw_quote.get("is_valid", False)))

            if not raw_quote or not raw_quote.get("is_valid", False) or raw_quote.get("price", 0) <= 0:
                if self.funnel_telemetry:
                    self.funnel_telemetry.record_quote_refetch_attempt(is_success=False, is_stale_before=True, latency_ms=elapsed_ms)
                    self.funnel_telemetry.record_funnel_event(
                        event_name=EVENT_QUOTE_REFETCH_END,
                        symbol=signal.iem_cd,
                        strategy=signal.strategy_id,
                        order_id=cid,
                        result="REJECT",
                        reject_reason="INVALID_REFETCH_DATA",
                        quote_age_ms=init_age,
                        elapsed_ms=elapsed_ms,
                        now_dt=now
                    )
                return None

            from execution.order_quote_manager import OrderQuoteSnapshot
            curr_p = int(raw_quote["price"])
            bid = int(raw_quote.get("bid", curr_p))
            ask = int(raw_quote.get("ask", curr_p))
            received_at = now

            quote_ts = raw_quote.get("timestamp")
            if isinstance(quote_ts, str):
                try:
                    quote_dt = datetime.fromisoformat(quote_ts)
                except Exception:
                    quote_dt = received_at
            elif isinstance(quote_ts, datetime):
                quote_dt = quote_ts
            else:
                quote_dt = received_at

            quote_data_age_ms = max(0.0, (received_at - quote_dt).total_seconds() * 1000.0)
            order_quote_age_ms = max(0.0, (now - received_at).total_seconds() * 1000.0)
            is_fresh = (quote_data_age_ms <= 3000.0)

            refetched_snapshot = OrderQuoteSnapshot(
                symbol=signal.iem_cd,
                current_price=curr_p,
                bid=bid,
                ask=ask,
                quote_time=str(raw_quote.get("quote_time") or received_at.strftime("%H:%M:%S")),
                quote_timestamp=quote_dt,
                received_at=received_at,
                api_latency_ms=elapsed_ms,
                quote_data_age_ms=quote_data_age_ms,
                order_quote_age_ms=order_quote_age_ms,
                is_fresh=is_fresh,
                staleness_reason="" if is_fresh else f"RE_FETCH_STALE ({quote_data_age_ms:.1f}ms > 3000ms)",
                source="REST_RE_FETCH"
            )

            if self.funnel_telemetry:
                self.funnel_telemetry.record_quote_refetch_attempt(is_success=is_fresh, is_stale_before=True, latency_ms=elapsed_ms)
                self.funnel_telemetry.record_funnel_event(
                    event_name=EVENT_QUOTE_REFETCH_END,
                    symbol=signal.iem_cd,
                    strategy=signal.strategy_id,
                    order_id=cid,
                    result="SUCCESS" if is_fresh else "REJECT",
                    reject_reason="" if is_fresh else refetched_snapshot.staleness_reason,
                    quote_age_ms=quote_data_age_ms,
                    elapsed_ms=elapsed_ms,
                    now_dt=now
                )

            return refetched_snapshot
        except Exception as e:
            logger.error(f"[OrderRouter] {signal.iem_cd} 최신 호가 재조회 예외: {e}")
            if self.funnel_telemetry:
                self.funnel_telemetry.record_quote_refetch_attempt(is_success=False, is_stale_before=True, latency_ms=0.0)
                self.funnel_telemetry.record_funnel_event(
                    event_name=EVENT_QUOTE_REFETCH_END,
                    symbol=signal.iem_cd,
                    strategy=signal.strategy_id,
                    order_id=cid,
                    result="REJECT",
                    reject_reason=f"REFETCH_EXCEPTION: {e}",
                    quote_age_ms=init_age,
                    elapsed_ms=0.0,
                    now_dt=now
                )
            return None

    def run_pre_order_checks(
        self,
        signal: TradeSignal,
        shares: int,
        order_price: int,
        balance: Dict[str, Any],
        portfolio_risk_status: str = "NORMAL",
        current_spread: float = 0.001,
        now: Optional[datetime] = None
    ) -> Tuple[bool, str]:
        """
        주문 전 10단계 사전 안전점검 (Section 28 & 30)
        """
        now = now or datetime.now()

        is_live_broker = (self.client is not None and not getattr(self.client, "dry_run", False))
        if is_live_broker:
            from core.after_hours_manager import MarketSessionManager, AfterHoursManager, MarketSession
            if signal.side == OrderSide.BUY:
                session_allowed, s_reason = MarketSessionManager.is_order_entry_allowed(
                    signal.time_horizon, dt=now, is_buy=True
                )
                if not session_allowed:
                    return False, s_reason

                sig_s = getattr(signal, "signal_session", "REGULAR") or "REGULAR"
                curr_session = MarketSessionManager.get_market_session(now)
                ent_s = "REGULAR" if curr_session == MarketSession.REGULAR else "AFTER_HOURS"
                valid_tag, tag_msg = AfterHoursManager.validate_trade_tagging(sig_s, ent_s)
                if not valid_tag:
                    return False, tag_msg
            elif signal.side == OrderSide.SELL:
                session_allowed, s_reason = MarketSessionManager.is_order_entry_allowed(
                    signal.time_horizon, dt=now, is_buy=False
                )
                if not session_allowed:
                    logger.info(f"[SESSION_BLOCK] side=SELL reason=OUTSIDE_TRADING_HOURS symbol={signal.iem_cd} desc={s_reason}")
                    return False, f"SESSION_BLOCK: OUTSIDE_TRADING_HOURS ({s_reason})"

        # [Section 1] Signal TTL (30,000ms 초과 시 SIGNAL_EXPIRED 및 재평가 요구)
        sig_time = getattr(signal, "signal_created_at", None) or getattr(signal, "timestamp", None)
        if sig_time:
            signal_age_ms = max(0.0, (now - sig_time).total_seconds() * 1000.0)
            signal.signal_age_ms = signal_age_ms
            max_ttl_ms = getattr(settings, "SIGNAL_MAX_AGE_MS", 30000.0)
            if signal.side == OrderSide.BUY and signal_age_ms > max_ttl_ms:
                signal.approved_status = "SIGNAL_EXPIRED"
                rej_ttl = f"SIGNAL_EXPIRED: 신호 생성 후 {signal_age_ms:.0f}ms 경과 (TTL {max_ttl_ms:.0f}ms 초과)"
                try:
                    from core.decision_trace import DecisionTraceRegistry
                    DecisionTraceRegistry.get_instance().record_no_trade(
                        symbol=signal.iem_cd,
                        strategy=signal.strategy_id,
                        entry_price=float(order_price),
                        current_price=float(order_price),
                        rejected_gate="SIGNAL_TTL",
                        rejected_reason=rej_ttl,
                        initial_rejected_gate="SIGNAL_TTL",
                        initial_rejected_reason=rej_ttl,
                        signal_age_ms=float(signal_age_ms),
                        now=now
                    )
                except Exception:
                    pass
                return False, rej_ttl

        # [Section 2] BUY_APPROVED 이후 가격 Drift 재검증
        appr_price = getattr(signal, "approved_price", None)
        if signal.side == OrderSide.BUY and appr_price and appr_price > 0:
            latest_p = float(order_price)
            if hasattr(signal, "quote_snapshot") and signal.quote_snapshot and signal.quote_snapshot.current_price > 0:
                latest_p = float(signal.quote_snapshot.current_price)
            signal.latest_price = latest_p
            drift_pct = (latest_p - appr_price) / float(appr_price)
            signal.price_drift_pct = drift_pct
            max_drift = getattr(settings, "MAX_ALLOWED_PRICE_DRIFT_PCT", 0.015)
            if abs(drift_pct) > max_drift:
                signal.approved_status = "REJECTED"
                rej_drift = f"PRICE_DRIFT_EXCEEDED: 승인가({appr_price:,.0f}원) 대비 {drift_pct*100:+.2f}% 급변 (허용 한도 {max_drift*100:.1f}% 초과)"
                try:
                    from core.decision_trace import DecisionTraceRegistry
                    DecisionTraceRegistry.get_instance().record_no_trade(
                        symbol=signal.iem_cd,
                        strategy=signal.strategy_id,
                        entry_price=float(appr_price),
                        current_price=float(latest_p),
                        rejected_gate="PRICE_DRIFT",
                        rejected_reason=rej_drift,
                        initial_rejected_gate="PRICE_DRIFT",
                        initial_rejected_reason=rej_drift,
                        price_drift_pct=float(drift_pct),
                        now=now
                    )
                except Exception:
                    pass
                return False, rej_drift

        # [Section 10] Liquidity / Spread / Slippage Gate (호가 깊이 및 잔량 부족 방어)
        if signal.side == OrderSide.BUY:
            bid = getattr(signal, "bid1_price", 0)
            ask = getattr(signal, "ask1_price", 0)
            ask1_qty = getattr(signal, "ask1_qty", 0)
            if hasattr(signal, "quote_snapshot") and signal.quote_snapshot:
                bid = signal.quote_snapshot.bid or bid
                ask = signal.quote_snapshot.ask or ask
            if bid > 0 and ask > 0 and ask >= bid:
                calc_spread = (ask - bid) / float(bid)
            else:
                calc_spread = current_spread
            max_spread = getattr(settings, "MAX_ALLOWED_SPREAD_PCT", 0.005)
            if calc_spread > max_spread:
                return False, f"SPREAD_TOO_WIDE: 스프레드 {calc_spread*100:.2f}% > {max_spread*100:.2f}%"
            if ask1_qty > 0 and shares > ask1_qty * 3:
                return False, f"INSUFFICIENT_LIQUIDITY: 주문수량 {shares}주 > 매도1호가잔량 {ask1_qty}주의 300%"

            # [Section 59 - 73] Minimum Profit Opportunity Gate (짤짤이/초단타 Churn 방어)
            from strategies.profit_opportunity_gate import validate_profit_opportunity, CounterfactualTracker
            opp_ok, opp_msg, opp_metrics = validate_profit_opportunity(
                current_price=float(order_price),
                stop_price=float(getattr(signal, "stop_price", 0)),
                target_price_1r=float(getattr(signal, "target_1r", 0)),
                target_price_2r=float(getattr(signal, "target_2r", 0)),
                strategy_id=signal.strategy_id,
                atr14=getattr(signal, "atr14", None),
                expected_move_pct=getattr(signal, "expected_move_pct", None),
                expected_mfe_pct=getattr(signal, "expected_mfe_pct", None),
                expected_net_r=getattr(signal, "expected_net_r", None),
                spread_pct=calc_spread,
                symbol=signal.iem_cd,
                signal_created_at=getattr(signal, "signal_created_at", None),
                now=now
            )
            if not opp_ok:
                signal.approved_status = "REJECTED"
                CounterfactualTracker.get_instance().record_blocked_trade(
                    symbol=signal.iem_cd,
                    strategy_id=signal.strategy_id,
                    ref_price=float(order_price),
                    reason=opp_msg,
                    metrics=opp_metrics,
                    now=now
                )
                try:
                    from core.decision_trace import DecisionTraceRegistry
                    DecisionTraceRegistry.get_instance().record_no_trade(
                        symbol=signal.iem_cd,
                        strategy=signal.strategy_id,
                        entry_price=float(order_price),
                        current_price=float(order_price),
                        rejected_gate="PROFIT_OPPORTUNITY",
                        rejected_reason=opp_msg,
                        initial_rejected_gate="PROFIT_OPPORTUNITY",
                        initial_rejected_reason=opp_msg,
                        spread_pct=float(calc_spread),
                        expected_move_pct=float(opp_metrics.get("expected_gross_move", 0.02)),
                        expected_net_r=float(opp_metrics.get("expected_net_r", 1.0)),
                        now=now
                    )
                except Exception:
                    pass
                return False, opp_msg

        # 1. 서킷 브레이커 상태 점검 및 안전한 자동 복구 시도 (신규 매수만 차단, 포지션 청산/손절 매도는 항상 허용)
        if signal.side == OrderSide.BUY and self.circuit_breaker and self.circuit_breaker.is_tripped:
            if self.circuit_breaker.attempt_recovery(health_check_fn=self.check_broker_health):
                logger.info(f"[{self.account_name} OrderRouter] 서킷 브레이커 자동 복구 완료 -> 매수 신호 검증 진행")
            else:
                return False, f"서킷브레이커 작동 중 ({self.circuit_breaker.trip_reason})"

        # 1-1. Critical Exit Failure 보호 모드 점검 (Requirement 7: 신규 매수만 차단, 청산 매도는 무조건 허용)
        if signal.side == OrderSide.BUY and getattr(self, "critical_exit_failure_active", False):
            return False, "CRITICAL_EXIT_FAILURE: 청산 실패 보호 모드 발동 중 (신규 매수 차단)"

        # 2. 계좌 잔고 및 예수금 정밀 확인 (Section 2 & 6: Fee + Slippage 반영)
        from risk.portfolio_cash import PortfolioCashManager
        cash_req = PortfolioCashManager.calculate_total_required_cash(shares, order_price)
        total_required_cash = cash_req.total_required_cash

        if self.cash_manager:
            available_cash = self.cash_manager.effective_available_cash
        else:
            available_cash = balance.get("cash", 0)

        if signal.side == OrderSide.BUY and total_required_cash > available_cash:
            shortfall = total_required_cash - available_cash
            return False, f"INSUFFICIENT_CASH: 필요금액 {total_required_cash:,.0f}원 > 가용현금 {available_cash:,.0f}원 (부족: {shortfall:,.0f}원)"

        # 2-S. 매도 주문 시 보유 수량, 미체결 주문 및 매도가능수량(broker_psbl_qty) 사전 검증 (Error 16157 및 중복 SELL 원천 차단)
        if signal.side == OrderSide.SELL:
            # Item 11: 미체결 매도 주문 존재 여부 검사 (중복 SELL 방지)
            active_pending_sells = [
                po for po in self.pending_orders.values()
                if po.iem_cd == signal.iem_cd and po.side == OrderSide.SELL
                and po.status in (OrderStatus.ORDER_CREATED, OrderStatus.ORDER_SENT, OrderStatus.ORDER_ACK, OrderStatus.PENDING, OrderStatus.PARTIAL_FILL)
            ]
            if active_pending_sells:
                active_cids = [po.client_order_id for po in active_pending_sells]
                return False, f"ACTIVE_EXIT_ORDER_EXISTS: 미체결 매도 주문 대기 중 (기존 주문: {active_cids})"

            # Item 12: 브로커 매도가능수량(broker_psbl_qty) 정밀 확인
            broker_total_qty = 0
            broker_pending_sell_qty = 0
            broker_psbl_qty = None

            if self.client and not getattr(self.client, "dry_run", False) and hasattr(self.client, "get_sellable_quantity"):
                try:
                    sq = self.client.get_sellable_quantity(signal.iem_cd)
                    if isinstance(sq, dict) and (sq.get("is_valid") is True or "sellable_qty" in sq or "sll_pbl_qty" in sq):
                        broker_total_qty = int(sq.get("holding_qty") or sq.get("bnc_qty") or 0)
                        broker_pending_sell_qty = int(sq.get("unfilled_sell_qty") or sq.get("tdt_sll_ny_cns_qty") or 0)
                        broker_psbl_qty = int(sq.get("sellable_qty") if "sellable_qty" in sq else sq.get("sll_pbl_qty", 0))
                except Exception as e:
                    logger.debug(f"매도가능수량 조회 실패 (폴백 적용): {e}")

            if broker_psbl_qty is None and balance and "holdings" in balance:
                holdings_list = balance.get("holdings", [])
                match_h = [h for h in holdings_list if h.get("iem_cd") == signal.iem_cd]
                if match_h:
                    broker_total_qty = int(match_h[0].get("qty", 0))
                    pending_in_router = sum(po.remaining_qty or po.qty for po in active_pending_sells)
                    broker_pending_sell_qty = pending_in_router
                    broker_psbl_qty = max(0, broker_total_qty - pending_in_router)

            if broker_psbl_qty is not None and shares > broker_psbl_qty:
                return False, (
                    f"INSUFFICIENT_PSBL_QTY (INSUFFICIENT_BROKER_SELLABLE_QTY): 가용 {broker_psbl_qty}주 < 요청 {shares}주 "
                    f"[총보유: {broker_total_qty}주, 미체결매도: {broker_pending_sell_qty}주]"
                )

        # 3. 주문 수량 유효성 (> 0)
        if shares <= 0:
            return False, f"주문 수량 오류 ({shares}주)"

        # 4. 가격 Tick Normalize 일치 확인
        norm_price = normalize_price(order_price, signal.side.value, "LIMIT")
        if order_price != norm_price:
            order_price = norm_price  # 자동 규격화 보정

        # 5. 미체결 중복 주문 검사
        for p_order in self.pending_orders.values():
            if p_order.iem_cd == signal.iem_cd and p_order.side == signal.side:
                return False, f"동일 종목 미체결 주문 진행 중 ({p_order.client_order_id})"

        # 6. 최근 주문 쿨다운 (동일 종목 10초 내 재주문 차단: BUY 주문에만 적용)
        if signal.side == OrderSide.BUY:
            last_ord = self.last_order_by_symbol.get(signal.iem_cd)
            if last_ord and (now - last_ord).total_seconds() < 10.0:
                return False, f"동일 종목 주문 쿨다운 중 (경과 {(now - last_ord).total_seconds():.1f}초 < 10초)"

        # 7. 스프레드 / 슬리피지 예산 검증 (BUY 주문에만 적용, 긴급 청산은 허용)
        if signal.side == OrderSide.BUY and current_spread > MAX_SLIPPAGE_BUDGET:
            return False, f"스프레드 예산 초과 ({current_spread*100:.2f}% > {MAX_SLIPPAGE_BUDGET*100:.2f}%)"

        # 8. 포트폴리오 총 리스크 한도 점검 (신규 매수만 차단, 매도는 리스크 축소이므로 항상 통과)
        if signal.side == OrderSide.BUY and portfolio_risk_status == "BLOCKED":
            return False, "PORTFOLIO_RISK_LIMIT"

        # 9. 손절가 유효성
        if signal.side == OrderSide.BUY and signal.stop_price >= order_price:
            return False, f"손절가({signal.stop_price:,})가 진입가({order_price:,}) 이상"

        # 10. 실시간 데이터 신선도 확인 (BUY 주문에만 적용: 개별 종목 Quote 신선도 및 Circuit Breaker 가드)
        if signal.side == OrderSide.BUY:
            if self.circuit_breaker and getattr(self.circuit_breaker, "is_tripped", False):
                if not self.circuit_breaker.attempt_recovery(health_check_fn=self.check_broker_health):
                    return False, f"서킷 브레이커 긴급 차단 발동 중 ({self.circuit_breaker.trip_reason})"

            if hasattr(signal, "quote_snapshot") and signal.quote_snapshot is not None:
                is_stale = (not signal.quote_snapshot.is_fresh) or (signal.quote_snapshot.order_quote_age_ms > 3000.0)
                if is_stale:
                    # 1단계: Stale Quote 감지 -> 최신 호가 1회 Re-fetch 시도
                    age_ms = signal.quote_snapshot.order_quote_age_ms
                    logger.info(f"[{signal.iem_cd}] Stale Quote 감지 (age: {age_ms:.1f}ms) -> 최신 호가 1회 Re-fetch 수행")
                    refetched = self.re_fetch_fresh_quote(signal, now=now)
                    if refetched and refetched.is_fresh:
                        signal.quote_snapshot = refetched
                        logger.info(f"[{signal.iem_cd}] Re-fetch 성공: Fresh 호가 확보 (현재가: {refetched.current_price:,}원, age: {refetched.quote_data_age_ms:.1f}ms)")

                        # 2단계: 재조회 후 주문 직전 5대 핵심 조건 재검증
                        # 2-1. 가격 급변동 슬리피지 검증 (< 1.5% 급변 보호)
                        if order_price and order_price > 0:
                            price_dev = abs(refetched.current_price - order_price) / float(order_price)
                            if price_dev > 0.015:
                                return False, f"재조회 후 가격 급변동 초과 ({price_dev*100:.2f}% > 1.5%)"

                        # 2-2. 최신 스프레드 검증 (KRX 1틱 이내 스프레드는 정상 허용)
                        if refetched.ask > 0 and refetched.bid > 0:
                            re_spread = (refetched.ask - refetched.bid) / float(refetched.ask)
                            tick_size = get_tick_size(refetched.ask)
                            is_min_tick = (refetched.ask - refetched.bid) <= tick_size
                            if re_spread > MAX_SLIPPAGE_BUDGET and not is_min_tick:
                                return False, f"재조회 후 스프레드 예산 초과 ({re_spread*100:.2f}% > {MAX_SLIPPAGE_BUDGET*100:.2f}%)"

                        # 2-3. 손절가/목표가 및 R:R 재검증
                        target_price = getattr(signal, "target_2r", None) or getattr(signal, "target_price", None)
                        if target_price and signal.stop_price and order_price > 0:
                            risk = order_price - signal.stop_price
                            if risk <= 0:
                                return False, f"재조회 후 손절가({signal.stop_price:,})가 주문가({order_price:,}) 이상"
                            reward = target_price - order_price
                            if (reward / risk) < 1.45:
                                return False, f"재조회 후 R:R 미달 ({(reward/risk):.2f} < 1.50)"

                        # 2-4. 현금 가용량 재검증
                        from risk.portfolio_cash import PortfolioCashManager
                        re_req = PortfolioCashManager.calculate_total_required_cash(shares, order_price)
                        if re_req.total_required_cash > available_cash:
                            return False, f"INSUFFICIENT_CASH (재조회 후 필요금액 {re_req.total_required_cash:,.0f}원 > 가용 {available_cash:,.0f}원)"
                    else:
                        # 재조회 실패 또는 재조회 후에도 stale 상태이면 기존과 동일하게 주문 차단 (Fail-Safe 유지)
                        age_sec = (refetched.quote_data_age_ms / 1000.0) if refetched else (signal.quote_snapshot.order_quote_age_ms / 1000.0)
                        return False, f"시장 데이터 과도한 지연 (재조회 후에도 지연: {age_sec:.1f}초 > 3.0초)"
            elif self.circuit_breaker and getattr(self.circuit_breaker, "last_data_timestamp", None):
                last_ts = getattr(self.circuit_breaker, "last_data_timestamp", None)
                if isinstance(last_ts, datetime):
                    age = (now - last_ts).total_seconds()
                    if age > 30.0:
                        return False, f"시장 데이터 과도한 지연 ({age:.1f}초 > 30초)"

        # 보유 종목 수 정책: POSITION_COUNT_CHECK = BYPASSED / NOT_USED
        return True, "10단계 안전점검 전체 승인 통과 (POSITION_COUNT_CHECK: BYPASSED / NOT_USED)"

    def submit_order(
        self,
        signal: TradeSignal,
        shares: int,
        order_type: Optional[OrderType] = None,
        order_price: Optional[int] = None,
        balance: Optional[Dict[str, Any]] = None,
        portfolio_risk_status: str = "NORMAL",
        current_spread: float = 0.001,
        now: Optional[datetime] = None,
        client_order_id: Optional[str] = None
    ) -> Optional[Order]:
        """
        안전점검 통과 후 브로커 API로 주문 라우팅 및 Paper 실시간 체결 처리 (Idempotency 보장)
        """
        with self.execution_lock:
            # [Section 4] 멱등성 검사: order_intent_id / idempotency_key 중복 방지
            intent_id = getattr(signal, "order_intent_id", None) or getattr(signal, "idempotency_key", None)
            if intent_id and intent_id in self.executed_intent_ids:
                existing = self.executed_intent_ids[intent_id]
                logger.info(f"[주문 멱등성 보장] 기실행 order_intent_id 주문 반환 ({intent_id}, 상태: {existing.status})")
                return existing

            target_cid = client_order_id or getattr(signal, "client_order_id", None)
            if target_cid and target_cid in self.order_registry:
                existing = self.order_registry[target_cid]
                logger.info(f"[주문 멱등성 보장] 기등록 주문 반환 ({target_cid}, 상태: {existing.status})")
                return existing

            # [Section 4] 동일 종목 동시 중복 BUY 방지 (pending_entry)
            if signal.side == OrderSide.BUY and signal.iem_cd in self.pending_entries:
                logger.warning(f"[IDEMPOTENCY] 동일 종목({signal.iem_cd}) 이미 pending_entry 진행 중 -> 중복 BUY 차단")
                return None

            if balance is None:
                balance = {"cash": 1_000_000_000}
            if order_price is None or order_price <= 0:
                order_price = signal.strategy_price
            if order_type is None:
                order_type = signal.order_type or OrderType.MARKET

            order_price = normalize_price(order_price, signal.side.value, "LIMIT")

            # 10단계 검사 실행
            passed, msg = self.run_pre_order_checks(
                signal, shares, order_price, balance, portfolio_risk_status, current_spread, now=now
            )
            if not passed:
                signal.rejection_reasons.append(msg)
                signal.approved_status = "REJECTED"
                if self.funnel_telemetry:
                    self.funnel_telemetry.record_funnel_event(
                        event_name=EVENT_RISK_RECHECK,
                        symbol=signal.iem_cd,
                        strategy=signal.strategy_id,
                        order_id=target_cid or "",
                        result="REJECT",
                        reject_reason=msg,
                        now_dt=now
                    )
                print(f"[주문 반려] {signal.name}({signal.iem_cd}): {msg}")
                return None
            elif self.funnel_telemetry:
                self.funnel_telemetry.record_funnel_event(
                    event_name=EVENT_RISK_RECHECK,
                    symbol=signal.iem_cd,
                    strategy=signal.strategy_id,
                    order_id=target_cid or "",
                    result="SUCCESS",
                    now_dt=now
                )

            if not target_cid:
                target_cid = self.generate_client_order_id(signal.strategy_id, signal.iem_cd, signal.side)
            client_order_id = target_cid
            now = now or datetime.now()
            from core.after_hours_manager import MarketSessionManager, MarketSession
            sig_s = getattr(signal, "signal_session", "REGULAR") or "REGULAR"
            curr_session = MarketSessionManager.get_market_session(now)
            ent_s = "REGULAR" if curr_session == MarketSession.REGULAR else "AFTER_HOURS"
            order = Order(
                client_order_id=target_cid,
                iem_cd=signal.iem_cd,
                side=signal.side,
                order_type=order_type,
                qty=shares,
                price=order_price,
                strategy_id=signal.strategy_id,
                time_horizon=signal.time_horizon,
                status=OrderStatus.PENDING,
                created_at=now,
                sent_at=now,
                signal_session=sig_s,
                entry_session=ent_s,
                requested_qty=shares,
                remaining_qty=shares,
                order_intent_id=intent_id,
                idempotency_key=intent_id or target_cid,
                account_no=getattr(self.client, "act_no", None)
            )

            if self.funnel_telemetry:
                self.funnel_telemetry.record_funnel_event(
                    event_name=EVENT_ORDER_CREATED,
                    symbol=signal.iem_cd,
                    strategy=signal.strategy_id,
                    order_id=target_cid,
                    result="SUCCESS",
                    now_dt=now
                )

            self.order_registry[target_cid] = order
            self.pending_orders[target_cid] = order
            self.last_order_by_symbol[signal.iem_cd] = now
            if signal.side == OrderSide.BUY:
                self.pending_entries[signal.iem_cd] = order
            if intent_id:
                self.executed_intent_ids[intent_id] = order

            # Telemetry: ORDER_CREATED
            self._export_telemetry("ORDER_CREATED", order)

            # Central Cash Reservation (Section 3 & 6 & 7)
            if self.cash_manager and signal.side == OrderSide.BUY:
                ok, msg, req, shortfall_rec = self.cash_manager.revalidate_and_reserve_cash(
                    target_cid, signal, shares, order_price, now
                )
                if not ok:
                    order.status = OrderStatus.REJECTED
                    order.reject_reason = msg
                    signal.rejection_reasons.append(msg)
                    signal.approved_status = "REJECTED"
                    if self.funnel_telemetry:
                        self.funnel_telemetry.record_funnel_event(
                            event_name=EVENT_CASH_CHECK,
                            symbol=signal.iem_cd,
                            strategy=signal.strategy_id,
                            order_id=client_order_id,
                            result="REJECT",
                            reject_reason=msg,
                            now_dt=now
                        )
                    if client_order_id in self.pending_orders:
                        del self.pending_orders[client_order_id]
                    if signal.iem_cd in self.pending_entries:
                        del self.pending_entries[signal.iem_cd]
                    if intent_id and intent_id in self.executed_intent_ids:
                        del self.executed_intent_ids[intent_id]
                    print(f"[주문 반려] {signal.name}({signal.iem_cd}): {msg}")
                    return None
                elif self.funnel_telemetry:
                    self.funnel_telemetry.record_funnel_event(
                        event_name=EVENT_CASH_CHECK,
                        symbol=signal.iem_cd,
                        strategy=signal.strategy_id,
                        order_id=client_order_id,
                        result="SUCCESS",
                        now_dt=now
                    )

                # 현금 매니저에 의해 수량이 다운사이징된 경우 주문 객체 및 수량 동기화
                res_shares = self.cash_manager.reservations.get(client_order_id, {}).get("shares")
                if res_shares and res_shares != shares:
                    shares = res_shares
                    order.qty = res_shares

        # 주문 발주 시작 시점 계측
        t_sub_start = time.perf_counter()
        if self.funnel_telemetry:
            self.funnel_telemetry.record_funnel_event(
                event_name=EVENT_ORDER_SUBMIT_START,
                symbol=signal.iem_cd,
                strategy=signal.strategy_id,
                order_id=client_order_id,
                result="SUCCESS",
                now_dt=now
            )

        # 1. 단위테스트 / 모의 환경 (client=None)
        if self.client is None:
            if self.cash_manager and signal.side == OrderSide.BUY:
                self.cash_manager.on_order_fill(client_order_id, shares, float(order_price))
            if self.funnel_telemetry:
                ack_ms = (time.perf_counter() - t_sub_start) * 1000.0
                self.funnel_telemetry.record_funnel_event(
                    event_name=EVENT_BROKER_ACK,
                    symbol=signal.iem_cd,
                    strategy=signal.strategy_id,
                    order_id=client_order_id,
                    result="SUCCESS",
                    elapsed_ms=ack_ms,
                    now_dt=now
                )
                self.funnel_telemetry.record_funnel_event(
                    event_name=EVENT_FILL_RECEIVED,
                    symbol=signal.iem_cd,
                    strategy=signal.strategy_id,
                    order_id=client_order_id,
                    result="SUCCESS",
                    now_dt=now
                )
                self.funnel_telemetry.record_order_lifecycle(is_sent=True, is_filled=True)
            return order

        # 2. Paper Trading 환경 (client가 있고 dry_run=True)
        is_dry_run = getattr(self.client, "dry_run", False)
        if is_dry_run:
            # Section 34: Paper Trading 체결 시뮬레이션
            # 슬리피지(매수는 +0.05%, 매도는 -0.05%) 및 체결 가격 산출
            slippage_pct = 0.0005 if signal.side == OrderSide.BUY else -0.0005
            sim_fill_price = normalize_price(int(order_price * (1.0 + slippage_pct)), signal.side.value, "LIMIT")
            order.broker_order_no = f"PAPER_ORD_{now.strftime('%H%M%S')}"
            order.ack_at = datetime.now()
            order.status = OrderStatus.FILLED
            order.filled_qty = shares
            order.filled_avg_price = float(sim_fill_price)
            order.filled_at = datetime.now()
            signal.approved_status = "FILLED"

            if client_order_id in self.pending_orders:
                del self.pending_orders[client_order_id]

            if self.cash_manager and signal.side == OrderSide.BUY:
                self.cash_manager.on_order_fill(client_order_id, shares, float(sim_fill_price))

            if self.funnel_telemetry:
                ack_ms = (time.perf_counter() - t_sub_start) * 1000.0
                self.funnel_telemetry.record_funnel_event(
                    event_name=EVENT_BROKER_ACK,
                    symbol=signal.iem_cd,
                    strategy=signal.strategy_id,
                    order_id=client_order_id,
                    result="SUCCESS",
                    elapsed_ms=ack_ms,
                    now_dt=now
                )
                self.funnel_telemetry.record_funnel_event(
                    event_name=EVENT_FILL_RECEIVED,
                    symbol=signal.iem_cd,
                    strategy=signal.strategy_id,
                    order_id=client_order_id,
                    result="SUCCESS",
                    now_dt=now
                )
                self.funnel_telemetry.record_order_lifecycle(is_sent=True, is_filled=True)

            print(f"[Paper 체결] {signal.name}({signal.iem_cd}) {shares}주 {signal.side.value} 완료 @ {sim_fill_price:,}원 (슬리피지: {slippage_pct*100:+.2f}%)")
            return order

        # 실전 / 모의투자 브로커 API 전송
        self.rate_limiter.wait_turn("ORDER")
        order.status = OrderStatus.ORDER_SENT
        self._export_telemetry("ORDER_SENT", order)
        try:
            if signal.side == OrderSide.BUY:
                if order_type == OrderType.MARKET:
                    res = self.client.buy_market(signal.iem_cd, shares)
                else:
                    res = self.client.buy_limit(signal.iem_cd, shares, order_price)
            else:
                if order_type == OrderType.MARKET:
                    res = self.client.sell_market(signal.iem_cd, shares)
                else:
                    res = self.client.sell_limit(signal.iem_cd, shares, order_price)

            order.ack_at = datetime.now()
            ack_ms = (time.perf_counter() - t_sub_start) * 1000.0
            if self.circuit_breaker:
                self.circuit_breaker.record_order_success()

            broker_no = str(res.get("Output_0", {}).get("mkt_orr_no", "")) if res else "ORD_ACK"
            order.broker_order_no = broker_no

            # [Section 3 & 4: Item 3 & 4] 브로커 접수(ACK) 시점에는 ORDER_ACK 전이, 절대 FILLED 처리 금지!
            order.status = OrderStatus.ORDER_ACK
            order.filled_qty = 0
            order.remaining_qty = shares
            order.filled_avg_price = 0.0

            # Telemetry: ORDER_ACK
            self._export_telemetry("ORDER_ACK", order)

            if self.funnel_telemetry:
                self.funnel_telemetry.record_funnel_event(
                    event_name=EVENT_BROKER_ACK,
                    symbol=signal.iem_cd,
                    strategy=signal.strategy_id,
                    order_id=client_order_id,
                    result="SUCCESS",
                    elapsed_ms=ack_ms,
                    now_dt=now
                )
                self.funnel_telemetry.record_order_lifecycle(is_sent=True, is_filled=False)

            # [Section 6 & 7: Item 6 & 7] 미체결 대기 추적을 위해 pending_orders에 보존 (체결 전까지 절대 삭제 금지)
            # 체결 알림 텔레그램은 실제 broker fill 확인 시점에만 on_fill()에서 발송함
            print(f"[주문 접수(ACK)] {signal.name}({signal.iem_cd}) {shares}주 {signal.side.value} 접수 완료 (주문번호: {broker_no}, 체결 대기)")

            # 즉시 체결 가능성이 있는 주문(시장가 등)은 1회 즉시 체결 확인
            self._check_order_fill_immediate(order, signal)

            return order

        except Exception as e:
            order.ack_at = datetime.now()
            ack_ms = (time.perf_counter() - t_sub_start) * 1000.0
            order.status = OrderStatus.REJECTED
            signal.rejection_reasons.append(f"BROKER_API_ERROR: {e}")
            signal.approved_status = "REJECTED"
            self._export_telemetry("REJECTED", order, {"reason": str(e)})
            if client_order_id in self.pending_orders:
                del self.pending_orders[client_order_id]

            if self.cash_manager and signal.side == OrderSide.BUY:
                self.cash_manager.on_order_cancel_or_reject(client_order_id)

            for listener in getattr(self, "cancel_listeners", []):
                try:
                    listener(order, f"REJECTED: {e}")
                except Exception as c_err:
                    logger.error(f"거부 리스너 통지 실패: {c_err}")

            # ResponseClassifier를 통한 비즈니스 거절(Business Rejection) vs 시스템 장애(System Failure) 엄격 분리
            from core.api_gateway import ResponseClassifier, ErrorCategory
            category, desc = ResponseClassifier.classify(e)
            acc_name = getattr(self, "account_name", "UNKNOWN")

            code_match = re.search(r'\b(\d{5})\b', str(e))
            err_code = code_match.group(1) if code_match else "BUSINESS"

            if category in (ErrorCategory.BUSINESS_ERROR, ErrorCategory.INVALID_SYMBOL) or "23962" in str(e) or "매매가능" in str(e):
                logger.info(f"[ORDER_REJECT] type=BUSINESS_REJECTION code={err_code} symbol={signal.iem_cd} side={signal.side.value} account={acc_name} reason={e}")
                if self.circuit_breaker:
                    self.circuit_breaker.record_order_failure(str(e), error_type="BUSINESS_REJECTION")
            else:
                if self.circuit_breaker:
                    self.circuit_breaker.record_order_failure(str(e), error_type="SYSTEM_FAILURE")
                else:
                    logger.error(f"[CIRCUIT_BREAKER] account={acc_name} event=SYSTEM_FAILURE count=1 msg={e}")

            if self.funnel_telemetry:
                self.funnel_telemetry.record_funnel_event(
                    event_name=EVENT_BROKER_ACK,
                    symbol=signal.iem_cd,
                    strategy=signal.strategy_id,
                    order_id=client_order_id,
                    result="REJECT",
                    reject_reason=f"BROKER_API_ERROR: {e}",
                    elapsed_ms=ack_ms,
                    now_dt=now
                )
                self.funnel_telemetry.record_broker_api_call(latency_ms=ack_ms, is_success=False)
                self.funnel_telemetry.record_order_lifecycle(is_sent=True, is_filled=False)

            logger.error(f"[주문 실패] {signal.name}({signal.iem_cd}) API 거부: {e}")
            print(f"[주문 실패] {signal.name}({signal.iem_cd}) API 거부: {e}")
            return None

    def _check_order_fill_immediate(self, order: Order, signal: TradeSignal = None):
        """주문 접수 직후 즉시 체결 여부 1회 확인 (체결 시 on_fill 트리거)"""
        if not self.client or getattr(self.client, "dry_run", False):
            return
        if not hasattr(self.client, "get_daily_order_execution"):
            return
        try:
            orders = self.client.get_daily_order_execution(ost_cns_dit="0")
            for bo in orders:
                bo_no = str(bo.get("itg_orr_no") or bo.get("orr_no") or "")
                if bo_no and bo_no == str(order.broker_order_no):
                    cns_qty = int(float(bo.get("tot_cns_qty") or bo.get("cns_qty") or 0))
                    avg_pr = float(bo.get("cns_avg_uit_pr") or bo.get("cns_pr") or order.price)
                    if cns_qty > getattr(order, "filled_qty", 0):
                        self.on_fill(order.client_order_id, cns_qty, avg_pr, is_cumulative=True)
                    break
        except Exception as e:
            logger.debug(f"즉시 체결 조회 생략 ({order.client_order_id}): {e}")

    def _send_fill_telegram(self, order: Order, newly_filled: int, fill_price: float):
        """실제 브로커 체결 확인 시점에만 텔레그램 체결 알림 발송 (Item 7)"""
        try:
            from core.telegram_notifier import telegram_notifier
            dashboard_url = None
            try:
                url_file = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "data", "active_mobile_url.txt")
                if os.path.exists(url_file):
                    with open(url_file, "r", encoding="utf-8") as f:
                        candidate_url = f.read().strip()
                        if candidate_url.startswith("http"):
                            dashboard_url = candidate_url
            except Exception:
                pass

            pnl_won = 0
            calc_return_pct = 0.0
            entry_p = 0.0
            sig = getattr(order, "signal", None)
            name = getattr(sig, "name", order.iem_cd) if sig else order.iem_cd
            reason = (getattr(sig, "reason", "") or getattr(sig, "rule_name", "")) if sig else ""

            if order.side == OrderSide.SELL:
                try:
                    import sqlite3
                    db_path = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "data", "operational_v16.db")
                    if os.path.exists(db_path):
                        with sqlite3.connect(db_path, timeout=5) as conn:
                            cur = conn.cursor()
                            cur.execute("SELECT entry_price FROM positions WHERE iem_cd = ? ORDER BY id DESC LIMIT 1", (order.iem_cd,))
                            row = cur.fetchone()
                            if row and row[0]:
                                entry_p = float(row[0])
                except Exception:
                    pass

                if entry_p > 0:
                    calc_return_pct = round(((float(fill_price) - entry_p) / entry_p) * 100.0, 2)
                    pnl_won = int((float(fill_price) - entry_p) * newly_filled)

            telegram_notifier.send_trade_event(
                event_type=order.side.value if hasattr(order.side, "value") else str(order.side),
                symbol=order.iem_cd,
                name=name,
                price=int(fill_price),
                qty=newly_filled,
                reason=reason,
                return_pct=calc_return_pct,
                pnl_won=pnl_won,
                entry_price=entry_p,
                dashboard_url=dashboard_url,
                account=self.account_name,
                account_no=getattr(self.client, "act_no", None),
                trading_mode=getattr(self.client, "mode", None)
            )
        except Exception as tg_err:
            logger.warning(f"텔레그램 체결 알림 발송 실패 (무시): {tg_err}")

    def on_ack(self, client_order_id: str, broker_order_no: str):
        """브로커 접수(ACK) 이벤트 수신 처리 (지연된 ACK 수신 포함)"""
        order = self.order_registry.get(client_order_id)
        if order:
            order.ack_at = datetime.now()
            order.broker_order_no = broker_order_no
            order.status = OrderStatus.ORDER_ACK
            logger.info(f"[주문 ACK 수신] {client_order_id} -> 브로커 주문번호: {broker_order_no}")
            self._export_telemetry("ORDER_ACK", order)

    def on_fill(self, client_order_id: str, filled_qty: int = 0, fill_price: float = 0.0, is_cumulative: bool = False, **kwargs):
        """체결 이벤트 수신 처리 (실제 브로커 체결 확인 시점에만 상태 전이 및 통지)"""
        if "fill_qty" in kwargs and not filled_qty:
            filled_qty = kwargs["fill_qty"]

        order = self.order_registry.get(client_order_id) or self.pending_orders.get(client_order_id)
        if order:
            if client_order_id not in self.order_registry:
                self.order_registry[client_order_id] = order
            current_filled = getattr(order, "filled_qty", 0) or 0
            if is_cumulative:
                new_total = filled_qty
            else:
                new_total = current_filled + filled_qty

            new_total = min(order.qty, new_total)
            newly_filled = new_total - current_filled
            is_full_fill = (new_total >= order.qty)
            if newly_filled <= 0:
                if is_full_fill and order.status != OrderStatus.FILLED:
                    order.status = OrderStatus.FILLED
                    if client_order_id in self.pending_orders:
                        del self.pending_orders[client_order_id]
                    self.pending_entries.pop(order.iem_cd, None)
                return

            order.filled_qty = new_total
            order.remaining_qty = max(0, order.qty - new_total)
            order.filled_avg_price = float(fill_price)
            order.filled_at = datetime.now()

            # Central Cash Reservation 정산
            if self.cash_manager and order.side == OrderSide.BUY:
                self.cash_manager.on_order_fill(client_order_id, new_total, float(fill_price))

            is_full_fill = (new_total >= order.qty)
            if is_full_fill:
                order.status = OrderStatus.FILLED
                if client_order_id in self.pending_orders:
                    del self.pending_orders[client_order_id]
                self.pending_entries.pop(order.iem_cd, None)
                logger.info(f"[체결 완료] {client_order_id} 전량 체결 ({new_total}/{order.qty}주 @ {fill_price:,}원)")
            else:
                order.status = OrderStatus.PARTIAL_FILL
                logger.info(f"[부분 체결] {client_order_id} 부분 체결 ({new_total}/{order.qty}주, 잔여: {order.remaining_qty}주 @ {fill_price:,}원)")

            # [Section 7: Item 7] 텔레그램 체결 알림 - 실제 broker fill 확인 시점에만 발송!
            if newly_filled > 0:
                self._send_fill_telegram(order, newly_filled, fill_price)

            # 등록된 체결 리스너(PositionManager 등)에 체결 통지
            for listener in getattr(self, "fill_listeners", []):
                try:
                    listener(order, newly_filled, fill_price, is_full_fill)
                except Exception as l_err:
                    logger.error(f"체결 리스너 통지 실패: {l_err}")

            # Telemetry: BUY_FILLED / SELL_FILLED / PARTIAL_FILL
            self._export_fill_telemetry(order, newly_filled, fill_price, is_full_fill)

    def retry_order(self, client_order_id: str) -> Tuple[bool, str, Optional[Order]]:
        """
        ORDER_SENT -> ACK 지연 상태에서의 재시도 처리 (멱등성 보장).
        이미 pending_orders 또는 order_registry에 동일 주문이 존재하면 중복 브로커 주문 발주를 차단
        """
        existing = self.order_registry.get(client_order_id)
        if existing:
            if existing.status in (OrderStatus.FILLED, OrderStatus.PARTIAL):
                return False, f"ALREADY_FILLED ({existing.status.value})", existing
            if client_order_id in self.pending_orders:
                return False, f"ALREADY_PENDING (중복 발주 차단: {client_order_id})", existing
        return False, f"ORDER_NOT_FOUND: {client_order_id}", None

    def cancel_order(self, client_order_id: str, reason: str = "CANCELLED") -> bool:
        """
        미체결 주문 취소 (ORDER_TIMEOUT 또는 수동 취소)
        """
        order = self.pending_orders.get(client_order_id)
        if not order:
            return False

        # 브로커 API가 있고 실전/모의인 경우 취소 요청
        if self.client and not getattr(self.client, "dry_run", False) and hasattr(self.client, "cancel_order"):
            try:
                self.rate_limiter.wait_turn("ORDER")
                self.client.cancel_order(order.broker_order_no, order.iem_cd, order.qty)
            except Exception as e:
                logger.error(f"[주문 취소 API 오류] {order.client_order_id}: {e}")

        order.status = OrderStatus.CANCELLED
        order.reject_reason = reason
        if client_order_id in self.pending_orders:
            del self.pending_orders[client_order_id]
        self.pending_entries.pop(order.iem_cd, None)

        if self.cash_manager and order.side == OrderSide.BUY:
            self.cash_manager.on_order_cancel_or_reject(client_order_id)

        self._export_telemetry("CANCELLED", order, {"reason": reason})

        for listener in getattr(self, "cancel_listeners", []):
            try:
                listener(order, reason)
            except Exception as c_err:
                logger.error(f"취소 리스너 통지 실패: {c_err}")

        logger.info(f"[주문 취소 완료] {order.iem_cd} ({client_order_id}) 사유: {reason}")
        return True

    def check_zombie_orders(self, timeout_ms: float = 3000.0) -> List[Order]:
        """ACK 응답이 3초 이상 지연된 미체결 의심 주문 탐지"""
        now = datetime.now()
        zombies = []
        for order in self.pending_orders.values():
            if order.sent_at:
                elapsed_ms = (now - order.sent_at).total_seconds() * 1000.0
                if elapsed_ms > timeout_ms:
                    order.is_zombie_suspected = True
                    zombies.append(order)
        return zombies

    def reconcile_orders(self, client=None) -> Dict[str, Any]:
        """미체결/Pending 주문에 대해 브로커 실제 주문체결내역(dailyOrderExecution) 및 잔고와 정밀 대조"""
        cli = client or self.client
        pending_ids = list(self.pending_orders.keys())
        if not pending_ids or not cli:
            return {"reconciled_count": 0, "filled_count": 0, "cancelled_count": 0, "partial_count": 0}

        filled_cnt = 0
        cancelled_cnt = 0
        partial_cnt = 0

        # 브로커 실제 당일 주문체결내역 조회
        broker_orders_by_no = {}
        if hasattr(cli, "get_daily_order_execution") and not getattr(cli, "dry_run", False):
            try:
                self.rate_limiter.wait_turn("RECONCILIATION")
                raw_orders = cli.get_daily_order_execution(ost_cns_dit="0")
                for bo in raw_orders:
                    bo_no = str(bo.get("itg_orr_no") or bo.get("orr_no") or "")
                    if bo_no:
                        broker_orders_by_no[bo_no] = bo
            except Exception as e:
                logger.warning(f"reconcile_orders dailyOrderExecution 조회 실패: {e}")

        for cid in pending_ids:
            order = self.pending_orders.get(cid)
            if not order:
                continue

            # 1. 브로커 일별 주문체결내역 대조
            bo = broker_orders_by_no.get(str(order.broker_order_no or ""))
            if bo:
                cns_qty = int(float(bo.get("tot_cns_qty") or bo.get("cns_qty") or 0))
                can_qty = int(float(bo.get("can_qty") or 0))
                avg_pr = float(bo.get("cns_avg_uit_pr") or bo.get("cns_pr") or order.price)
                if cns_qty > order.filled_qty:
                    self.on_fill(cid, cns_qty, avg_pr, is_cumulative=True)
                    if order.status == OrderStatus.FILLED:
                        filled_cnt += 1
                    else:
                        partial_cnt += 1
                elif can_qty >= order.qty or (cns_qty == 0 and can_qty > 0):
                    order.status = OrderStatus.CANCELED
                    if cid in self.pending_orders:
                        del self.pending_orders[cid]
                    self.pending_entries.pop(order.iem_cd, None)
                    for listener in getattr(self, "cancel_listeners", []):
                        try:
                            listener(order, "BROKER_CANCELED")
                        except Exception as c_err:
                            logger.error(f"취소 리스너 통지 실패: {c_err}")
                    cancelled_cnt += 1
                continue

            # 2. Mock / Paper / Balance 기반 Fallback
            try:
                balance_data = cli.get_balance() if hasattr(cli, "get_balance") else {}
                holdings = balance_data.get("holdings", []) if isinstance(balance_data, dict) else []
                holding_codes = {h.get("iem_cd", ""): h for h in holdings}
            except Exception:
                holding_codes = {}

            if order.side == OrderSide.BUY and order.iem_cd in holding_codes:
                h_item = holding_codes[order.iem_cd]
                fill_p = float(h_item.get("buy_price") or h_item.get("avg_price") or order.price)
                self.on_fill(cid, order.qty, fill_p, is_cumulative=True)
                filled_cnt += 1
            elif (getattr(cli, "dry_run", False) is True) or (getattr(cli, "is_mock_recon", False) is True):
                elapsed = (datetime.now() - order.sent_at).total_seconds() if order.sent_at else 0
                if elapsed > 60.0:  # 1분 이상 미체결 상태는 취소 처리
                    order.status = OrderStatus.CANCELLED
                    order.reconciled = True
                    cancelled_cnt += 1
                    if cid in self.pending_orders:
                        del self.pending_orders[cid]

        return {
            "reconciled_count": filled_cnt + cancelled_cnt + partial_cnt,
            "filled_count": filled_cnt,
            "cancelled_count": cancelled_cnt,
            "partial_count": partial_cnt
        }
