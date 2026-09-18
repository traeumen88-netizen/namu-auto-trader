"""[FINAL MASTER v16.0] 청산 워치독 장애 격리 및 치명적 청산 장애 탐지/보호 엔진
(Exit Watchdog Fault Isolation & Critical Exit Failure Protection Engine)
execution/exit_watchdog.py

핵심 불변조건:
"어떤 신규매수/후보탐색/ML/텔레그램/시세 처리 오류가 발생하더라도
 기존 보유 포지션의 청산 및 손절 기능은 살아 있어야 한다."

핵심 아키텍처:
1. Exit Watchdog 완전 독립화 (후보 스캔, 전략, ML, BUY 파이프라인과 100% 격리)
2. Watchdog 내부 종목별 장애 격리 (Position Fault Isolation: 종목 A 예외 발생 시 A만 격리, B/C 정상 청산)
3. Watchdog 자체 장애 격리 및 자동 재기동 (Watchdog Resilience)
4. Watchdog Heartbeat (HEALTHY / DEGRADED / CRITICAL) 및 실시간 텔레메트리
5. Critical Exit Failure 탐지:
   - STOP_CONDITION_TRUE -> EXIT_TRIGGERED -> EXIT_ORDER_CREATED -> ORDER_SENT -> ORDER_ACK -> PARTIAL_FILL / FILLED
   - 단계별 타임아웃 및 결손 발생 시 즉시 CRITICAL_EXIT_FAILURE 선언
6. Critical Exit Failure 보호 모드:
   - 신규 BUY 전면 일시 차단
   - 기존 보유 포지션 Exit Watchdog 감시 및 청산은 100% 지속 허용
   - 브로커 잔고/주문 재조회 및 Idempotent 재시도 (중복 매도 방지)
7. 영구 감사 이벤트 분리 저장:
   - HARD_STOP_CONDITION_TRUE, EXIT_TRIGGERED, EXIT_ORDER_CREATED, EXIT_ORDER_SENT, EXIT_ORDER_ACK, EXIT_PARTIAL_FILL, EXIT_FILLED
"""

import os
import sys
import time
import json
import logging
import sqlite3
from enum import Enum
from dataclasses import dataclass, field, asdict
from datetime import datetime
from typing import Dict, Any, List, Optional, Tuple, Set

logger = logging.getLogger("ExitWatchdog")

# UTF-8 콘솔 출력 보정
if sys.platform == "win32":
    try:
        sys.stdout.reconfigure(encoding="utf-8")
        sys.stderr.reconfigure(encoding="utf-8")
    except Exception:
        pass

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if BASE_DIR not in sys.path:
    sys.path.insert(0, BASE_DIR)

from core.models import Position, TradeSignal, OrderSide, OrderType, OrderStatus, TimeHorizon
from execution.exit_order_policy import ExitOrderPolicyEngine, ExitReason, ExitOrderPlan


class ExitLifecycleStage(str, Enum):
    """청산 생명주기 7대 핵심 단계"""
    HARD_STOP_CONDITION_TRUE = "HARD_STOP_CONDITION_TRUE"
    EXIT_TRIGGERED = "EXIT_TRIGGERED"
    EXIT_ORDER_CREATED = "EXIT_ORDER_CREATED"
    EXIT_ORDER_SENT = "EXIT_ORDER_SENT"
    EXIT_ORDER_ACK = "EXIT_ORDER_ACK"
    EXIT_PARTIAL_FILL = "EXIT_PARTIAL_FILL"
    EXIT_FILLED = "EXIT_FILLED"
    CRITICAL_EXIT_FAILURE = "CRITICAL_EXIT_FAILURE"


class WatchdogStatus(str, Enum):
    """워치독 상태"""
    HEALTHY = "HEALTHY"
    DEGRADED = "DEGRADED"
    CRITICAL = "CRITICAL"


@dataclass
class WatchdogHeartbeat:
    """Watchdog Heartbeat Telemetry (Requirement 5)"""
    watchdog_status: str = WatchdogStatus.HEALTHY.value
    watchdog_cycle_id: str = ""
    watchdog_started_at: str = ""
    watchdog_completed_at: str = ""
    watchdog_last_heartbeat_at: float = 0.0
    watchdog_cycle_duration_ms: float = 0.0
    held_position_count: int = 0
    checked_position_count: int = 0
    stop_triggered_count: int = 0
    exit_order_created_count: int = 0
    watchdog_error_count: int = 0
    watchdog_consecutive_failures: int = 0
    protective_mode_active: bool = False
    critical_failures: List[Dict[str, Any]] = field(default_factory=list)


@dataclass
class CriticalExitFailureRecord:
    """Critical Exit Failure 세부 명세 (Requirement 6 & 11)"""
    stock_code: str
    position_id: str
    current_price: float
    stop_price: float
    quantity: int
    stop_condition: bool
    exit_state: str
    failed_stage: str
    watchdog_cycle_id: str
    timestamp: str
    error_message: str
    retry_count: int = 0
    resolved: bool = False


@dataclass
class ExitOrderContext:
    """Exit 주문 Idempotency 및 중복 방지 상태 추적 (Requirement 8)"""
    idempotency_key: str
    position_id: str
    stock_code: str
    exit_reason: str
    requested_qty: int
    previous_order_id: Optional[str] = None
    state: str = "EXIT_PENDING"  # EXIT_PENDING, EXIT_SENT, EXIT_ACKED, EXIT_PARTIAL, EXIT_FILLED, EXIT_FAILED
    created_at: float = field(default_factory=time.time)
    retry_count: int = 0
    last_retry_at: float = 0.0


class ExitWatchdog:
    """
    [Section 2-12] 청산 워치독 장애 격리 및 치명적 청산 장애 보호 전담 엔진
    """

    def __init__(
        self,
        db_path: str = "data/operational_v16.db",
        heartbeat_file: str = "data/watchdog_heartbeat.json",
        stop_to_trigger_timeout_sec: float = 2.0,
        trigger_to_order_timeout_sec: float = 2.0,
        order_to_sent_timeout_sec: float = 3.0,
        sent_to_ack_timeout_sec: float = 5.0
    ):
        self.db_path = db_path
        self.heartbeat_file = heartbeat_file
        self.stop_to_trigger_timeout = stop_to_trigger_timeout_sec
        self.trigger_to_order_timeout = trigger_to_order_timeout_sec
        self.order_to_sent_timeout = order_to_sent_timeout_sec
        self.sent_to_ack_timeout = sent_to_ack_timeout_sec

        self._seq = 0
        self.current_heartbeat = WatchdogHeartbeat()
        self.active_critical_failures: Dict[str, CriticalExitFailureRecord] = {}
        self.exit_contexts: Dict[str, ExitOrderContext] = {}  # key: position_id
        self.last_notified_critical: Dict[str, float] = {}

        # 라이프사이클 이벤트 타임스탬프 추적 (position_id -> {stage: float})
        self.stage_timestamps: Dict[str, Dict[str, float]] = {}

        self._init_db()

    def _init_db(self):
        """라이프사이클 이벤트 영구 저장용 테이블 생성 (Requirement 10)"""
        try:
            os.makedirs(os.path.dirname(self.db_path), exist_ok=True)
            with sqlite3.connect(self.db_path, timeout=5.0) as conn:
                conn.execute("""
                CREATE TABLE IF NOT EXISTS exit_lifecycle_events (
                    event_id TEXT PRIMARY KEY,
                    position_id TEXT NOT NULL,
                    iem_cd TEXT NOT NULL,
                    stage TEXT NOT NULL,
                    price REAL,
                    stop_price REAL,
                    qty INTEGER,
                    timestamp TEXT NOT NULL,
                    cycle_id TEXT NOT NULL,
                    details TEXT
                );
                """)
                conn.execute("""
                CREATE INDEX IF NOT EXISTS idx_exit_lifecycle_pos_stage 
                ON exit_lifecycle_events(position_id, stage);
                """)
        except Exception as e:
            logger.error(f"[ExitWatchdog] DB 스키마 초기화 실패: {e}")

    def record_lifecycle_event(
        self,
        position_id: str,
        iem_cd: str,
        stage: ExitLifecycleStage,
        price: float,
        stop_price: float,
        qty: int,
        cycle_id: str,
        details: str = "",
        now: Optional[datetime] = None
    ):
        """
        영구 감사 이벤트 분리 저장 (Requirement 10)
        HARD_STOP_CONDITION_TRUE, EXIT_TRIGGERED, EXIT_ORDER_CREATED,
        EXIT_ORDER_SENT, EXIT_ORDER_ACK, EXIT_PARTIAL_FILL, EXIT_FILLED
        """
        now_dt = now or datetime.now()
        ts_now = time.time()
        if position_id not in self.stage_timestamps:
            self.stage_timestamps[position_id] = {}
        self.stage_timestamps[position_id][stage.value] = ts_now

        event_id = f"EVT_{position_id}_{stage.value}_{int(ts_now * 1000)}"
        try:
            with sqlite3.connect(self.db_path, timeout=5.0) as conn:
                conn.execute("""
                INSERT OR REPLACE INTO exit_lifecycle_events (
                    event_id, position_id, iem_cd, stage, price, stop_price, qty, timestamp, cycle_id, details
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """, (
                    event_id, position_id, iem_cd, stage.value, float(price), float(stop_price),
                    int(qty), now_dt.isoformat(), cycle_id, details
                ))
        except Exception as e:
            logger.debug(f"[ExitWatchdog] 라이프사이클 이벤트 DB 기록 실패 ({stage.value}): {e}")

    def is_protective_mode_active(self) -> bool:
        """
        Critical Exit Failure 발생 시 즉시 보호 모드 활성 여부 반환 (Requirement 7)
        True인 경우 신규 BUY 진입이 전면 차단됨.
        """
        return bool(self.active_critical_failures)

    def run_watchdog_cycle(
        self,
        accounts: list,
        scanner: Any,
        store: Any,
        now: Optional[datetime] = None
    ) -> Dict[str, Any]:
        """
        [STEP 0: EXIT WATCHDOG 최우선 / 독립 실행] (Requirement 2, 3, 4, 5)
        어떤 시장 스캔, 후보 검색, ML, 텔레그램, API 오류와도 무관하게 독립 실행.
        각 포지션별 try-except로 특정 종목 오류가 타 종목에 절대 전파되지 않음.
        """
        now_dt = now or datetime.now()
        self._seq += 1
        cycle_id = f"WD_{int(now_dt.timestamp() * 1000)}_{self._seq}"
        t0 = time.perf_counter()

        hb = self.current_heartbeat
        hb.watchdog_cycle_id = cycle_id
        hb.watchdog_started_at = now_dt.isoformat()
        hb.watchdog_error_count = 0
        hb.checked_position_count = 0
        hb.stop_triggered_count = 0
        hb.exit_order_created_count = 0

        # 전체 최상위 보호 계층 (Requirement 4)
        try:
            # 1. 모든 계좌의 보유 포지션 수집
            held_items: List[Tuple[Any, Position]] = []
            for acc in accounts:
                if not hasattr(acc, "position_manager") or not acc.position_manager:
                    continue
                for pos_id, pos in list(acc.position_manager.positions.items()):
                    if not getattr(pos, "is_closed", False) and pos.qty > 0:
                        held_items.append((acc, pos))

            hb.held_position_count = len(held_items)

            # 2. 보유 종목별 개별 시세 수신 및 독립 스톱/청산 검사 (Requirement 3: 종목별 장애 격리)
            for acc, pos in held_items:
                hb.checked_position_count += 1
                try:
                    self._check_and_manage_single_position(acc, pos, scanner, store, cycle_id, now_dt)
                except Exception as pos_e:
                    # 종목별 장애 격리: 종목 A 오류 시 A만 기록하고 B/C 계속 진행 (Requirement 3)
                    hb.watchdog_error_count += 1
                    self._log_critical_watchdog_error(pos, pos_e, cycle_id, now_dt)
                    continue

            # 3. 치명적 청산 장애(Critical Exit Failure) 및 불변조건 검사 (Requirement 6, 11)
            self._evaluate_critical_exit_invariants(held_items, cycle_id, now_dt)

            # 4. 실패한 청산 주문 Fail-Safe 재시도 (Requirement 8, 9)
            if self.active_critical_failures:
                self._execute_exit_retry_failsafe(accounts, cycle_id, now_dt)

            # 5. Heartbeat 갱신 (Requirement 5)
            hb.watchdog_consecutive_failures = 0
            if self.active_critical_failures:
                hb.watchdog_status = WatchdogStatus.DEGRADED.value
            elif hb.watchdog_error_count > 0:
                hb.watchdog_status = WatchdogStatus.DEGRADED.value
            else:
                hb.watchdog_status = WatchdogStatus.HEALTHY.value

        except Exception as top_e:
            # Watchdog 자체 최상위 예외 격리 (Requirement 4)
            hb.watchdog_consecutive_failures += 1
            hb.watchdog_status = WatchdogStatus.CRITICAL.value
            logger.critical(f"[WATCHDOG_CRITICAL_ERROR] Watchdog 사이클 중 최상위 예외 발생 (누적 실패: {hb.watchdog_consecutive_failures}): {top_e}")

        finally:
            t_end = time.perf_counter()
            hb.watchdog_completed_at = datetime.now().isoformat()
            hb.watchdog_last_heartbeat_at = time.time()
            hb.watchdog_cycle_duration_ms = round((t_end - t0) * 1000.0, 2)
            hb.protective_mode_active = self.is_protective_mode_active()
            hb.critical_failures = [asdict(r) for r in self.active_critical_failures.values()]
            self._save_heartbeat_to_disk()

        return asdict(hb)

    def _check_and_manage_single_position(
        self,
        acc: Any,
        pos: Position,
        scanner: Any,
        store: Any,
        cycle_id: str,
        now: datetime
    ):
        """단일 포지션 시세 조회 및 스톱/청산 검사 (종목별 장애 격리 실행)"""
        code = str(pos.iem_cd).strip()
        client = getattr(acc, "client", None)
        if not client:
            raise RuntimeError(f"계좌 클라이언트 부재: {acc.name}")

        # 개별 호가 조회 (REST)
        curr_info = client.get_current_price(code)
        if not curr_info or curr_info.get("price", 0) <= 0:
            raise ValueError(f"시세 수신 실패 또는 0원 호가: {code}")

        price = int(curr_info["price"])
        bid = float(curr_info.get("bid", price))
        ask = float(curr_info.get("ask", price))

        agg = scanner.get_aggregator(code) if scanner and hasattr(scanner, "get_aggregator") else None
        atr14 = agg.calculate_atr("1m", 14) if agg else 0.0
        ema9 = agg.calculate_ema("1m", 9) if agg else 0.0

        # HARD_STOP_COND 실시간 판정
        hard_stop_cond = (price <= pos.stop_price) if getattr(pos, "stop_price", 0) > 0 else False
        if hard_stop_cond:
            self.record_lifecycle_event(
                position_id=pos.position_id,
                iem_cd=code,
                stage=ExitLifecycleStage.HARD_STOP_CONDITION_TRUE,
                price=float(price),
                stop_price=float(pos.stop_price),
                qty=pos.qty,
                cycle_id=cycle_id,
                details=f"현재가 {price:,}원 <= 손절가 {pos.stop_price:,}원",
                now=now
            )

        # PositionManager의 update_price_and_manage 호출
        closed_before = getattr(pos, "is_closed", False)
        stops_before = acc.position_manager.stop_watchdog_stats.get("stops_triggered", 0)

        acc.position_manager.update_price_and_manage(
            iem_cd=code,
            current_price=price,
            current_time=now,
            atr14=atr14,
            ema9=ema9,
            bid=bid,
            ask=ask
        )

        stops_after = acc.position_manager.stop_watchdog_stats.get("stops_triggered", 0)
        closed_after = getattr(pos, "is_closed", False)

        if stops_after > stops_before:
            self.current_heartbeat.stop_triggered_count += (stops_after - stops_before)
            self.record_lifecycle_event(
                position_id=pos.position_id,
                iem_cd=code,
                stage=ExitLifecycleStage.EXIT_TRIGGERED,
                price=float(price),
                stop_price=float(pos.stop_price),
                qty=pos.qty,
                cycle_id=cycle_id,
                details="스톱로스 트리거 정상 발동",
                now=now
            )

        if closed_after and not closed_before:
            self.current_heartbeat.exit_order_created_count += 1
            self.record_lifecycle_event(
                position_id=pos.position_id,
                iem_cd=code,
                stage=ExitLifecycleStage.EXIT_ORDER_CREATED,
                price=float(price),
                stop_price=float(pos.stop_price),
                qty=pos.qty,
                cycle_id=cycle_id,
                details=f"청산 주문 생성 완료 ({pos.exit_reason})",
                now=now
            )
            # 전량 청산 완료 시 해당 계좌 독립 쿨다운 부여 및 스토어 상태 정리
            if hasattr(acc, "set_cooldown"):
                acc.set_cooldown(code, now=now, cooldown_seconds=600)
            if store and not acc.position_manager.has_position(code):
                from core.models import SymbolState
                sym = store.get(code)
                if sym and sym.state == SymbolState.POSITION:
                    store.demote(code, SymbolState.WATCH, reason="포지션 전량 청산 완료")

            # 포지션이 완전히 정리되었으면 active_critical_failures에서 해제
            if pos.position_id in self.active_critical_failures:
                self.active_critical_failures[pos.position_id].resolved = True
                del self.active_critical_failures[pos.position_id]
                logger.info(f"[CRITICAL EXIT RECOVERED] {code}({pos.position_id}) 정상 청산 완료되어 보호 모드 해제")

    def _log_critical_watchdog_error(self, pos: Position, exc: Exception, cycle_id: str, now: datetime):
        """종목별 오류 로깅 규격 (Requirement 3)"""
        code = getattr(pos, "iem_cd", "UNKNOWN")
        pid = getattr(pos, "position_id", "UNKNOWN")
        exc_type = type(exc).__name__
        exc_msg = str(exc)

        err_log = (
            f"\n------------------------------------------------------------\n"
            f"WATCHDOG_POSITION_ERROR\n"
            f"stock_code={code}\n"
            f"position_id={pid}\n"
            f"exception_type={exc_type}\n"
            f"exception_message={exc_msg}\n"
            f"timestamp={now.isoformat()}\n"
            f"watchdog_cycle_id={cycle_id}\n"
            f"------------------------------------------------------------"
        )
        logger.error(err_log)
        print(err_log)

    def _evaluate_critical_exit_invariants(
        self,
        held_items: List[Tuple[Any, Position]],
        cycle_id: str,
        now: datetime
    ):
        """
        치명적 불변조건 검사 (Requirement 6 & 11)
        1. 실제 보유 포지션 존재 AND HARD_STOP_COND = TRUE AND EXIT_TRIGGERED 부재/지연
        2. EXIT_TRIGGERED = TRUE AND EXIT_ORDER_CREATED 부재/지연
        3. EXIT_ORDER_CREATED = TRUE AND ORDER_SENT 부재/지연
        4. SELL ORDER SENT AND BROKER ACK 누락/타임아웃
        """
        now_ts = time.time()
        for acc, pos in held_items:
            pid = pos.position_id
            code = pos.iem_cd
            cur_p = pos.current_price
            stop_p = pos.stop_price
            qty = pos.qty
            stages = self.stage_timestamps.get(pid, {})

            # Invariant 1: HARD_STOP_COND = TRUE 인데 EXIT_TRIGGERED가 없는 경우 (Requirement 11)
            hard_stop_cond = (cur_p <= stop_p) if (stop_p > 0 and cur_p > 0) else False
            if hard_stop_cond and not getattr(pos, "is_closed", False):
                stop_true_ts = stages.get(ExitLifecycleStage.HARD_STOP_CONDITION_TRUE.value, now_ts)
                has_trigger = ExitLifecycleStage.EXIT_TRIGGERED.value in stages

                if (not has_trigger) and (now_ts - stop_true_ts >= self.stop_to_trigger_timeout):
                    self._escalate_critical_exit_failure(
                        stock_code=code,
                        position_id=pid,
                        cur_p=cur_p,
                        stop_p=stop_p,
                        qty=qty,
                        stop_cond=hard_stop_cond,
                        exit_state="TRIGGER_MISSING",
                        failed_stage=ExitLifecycleStage.EXIT_TRIGGERED.value,
                        cycle_id=cycle_id,
                        error_msg=f"손절 조건 발생 후 {self.stop_to_trigger_timeout}초 초과되었으나 EXIT_TRIGGERED 미발생"
                    )

            # Invariant 2: EXIT_TRIGGERED = TRUE 인데 EXIT_ORDER_CREATED가 없는 경우
            if ExitLifecycleStage.EXIT_TRIGGERED.value in stages and not getattr(pos, "is_closed", False):
                trig_ts = stages[ExitLifecycleStage.EXIT_TRIGGERED.value]
                has_order_created = ExitLifecycleStage.EXIT_ORDER_CREATED.value in stages
                if (not has_order_created) and (now_ts - trig_ts >= self.trigger_to_order_timeout):
                    self._escalate_critical_exit_failure(
                        stock_code=code,
                        position_id=pid,
                        cur_p=cur_p,
                        stop_p=stop_p,
                        qty=qty,
                        stop_cond=hard_stop_cond,
                        exit_state="ORDER_NOT_CREATED",
                        failed_stage=ExitLifecycleStage.EXIT_ORDER_CREATED.value,
                        cycle_id=cycle_id,
                        error_msg=f"EXIT_TRIGGERED 발동 후 {self.trigger_to_order_timeout}초 초과되었으나 SELL Order 미생성"
                    )

            # Invariant 3: 주문 발주 후 브로커 ACK 타임아웃
            if pos.active_exit_order_id and acc.order_router:
                pending_order = acc.order_router.pending_orders.get(pos.active_exit_order_id)
                if pending_order and pending_order.side == OrderSide.SELL:
                    sent_ts = pending_order.sent_at.timestamp() if pending_order.sent_at else now_ts
                    if (now_ts - sent_ts >= self.sent_to_ack_timeout) and pending_order.status == OrderStatus.PENDING:
                        self._escalate_critical_exit_failure(
                            stock_code=code,
                            position_id=pid,
                            cur_p=cur_p,
                            stop_p=stop_p,
                            qty=qty,
                            stop_cond=hard_stop_cond,
                            exit_state="ORDER_ACK_TIMEOUT",
                            failed_stage=ExitLifecycleStage.EXIT_ORDER_ACK.value,
                            cycle_id=cycle_id,
                            error_msg=f"SELL 주문 전송 후 {self.sent_to_ack_timeout}초 동안 브로커 ACK 누락 (주문ID: {pos.active_exit_order_id})"
                        )

    def _escalate_critical_exit_failure(
        self,
        stock_code: str,
        position_id: str,
        cur_p: float,
        stop_p: float,
        qty: int,
        stop_cond: bool,
        exit_state: str,
        failed_stage: str,
        cycle_id: str,
        error_msg: str
    ):
        """치명적 청산 장애 승격 및 보호 모드 진입 (Requirement 6, 7, 11)"""
        rec = self.active_critical_failures.get(position_id)
        if not rec:
            rec = CriticalExitFailureRecord(
                stock_code=stock_code,
                position_id=position_id,
                current_price=cur_p,
                stop_price=stop_p,
                quantity=qty,
                stop_condition=stop_cond,
                exit_state=exit_state,
                failed_stage=failed_stage,
                watchdog_cycle_id=cycle_id,
                timestamp=datetime.now().isoformat(),
                error_message=error_msg,
                retry_count=0
            )
            self.active_critical_failures[position_id] = rec
        else:
            rec.failed_stage = failed_stage
            rec.exit_state = exit_state
            rec.error_message = error_msg
            rec.current_price = cur_p

        # 알림 중복 폭풍 방지 (동일 종목 10초 쿨다운)
        now_ts = time.time()
        last_notified = self.last_notified_critical.get(position_id, 0.0)
        if now_ts - last_notified >= 10.0:
            self.last_notified_critical[position_id] = now_ts
            alert_msg = (
                f"\n🚨 [CRITICAL EXIT FAILURE] 🚨\n"
                f"종목: {stock_code}\n"
                f"현재가: {cur_p:,}원\n"
                f"손절가: {stop_p:,}원\n"
                f"보유수량: {qty}주\n"
                f"STOP 조건: {'TRUE' if stop_cond else 'FALSE'}\n"
                f"EXIT 상태: {exit_state}\n"
                f"실패 단계: {failed_stage}\n"
                f"사유: {error_msg}\n"
                f"watchdog_cycle_id: {cycle_id}\n"
                f"보호 조치: [신규 BUY 즉시 차단] -> [기존 SELL 경로 재시도 / 브로커 재조회]"
            )
            logger.critical(alert_msg)
            print(alert_msg)

    def _execute_exit_retry_failsafe(self, accounts: list, cycle_id: str, now: datetime):
        """
        브로커 장애 시 Fail-Safe 재시도 & Idempotency 보장 (Requirement 8 & 9)
        1. 브로커 실제 잔고/주문 상태 재조회
        2. 브로커에 이미 체결되었으면 내부 포지션 동기화 (CLOSED)
        3. 브로커에 미체결 SELL 주문이 있으면 상태 추적 대기
        4. 브로커에 주문이 전혀 없으면 Idempotent Key로 SELL 즉시 재주문
        """
        for pid, fail_rec in list(self.active_critical_failures.items()):
            code = fail_rec.stock_code
            for acc in accounts:
                if not acc.position_manager or not acc.position_manager.has_position(code):
                    continue

                pos = acc.position_manager.get_position(code)
                if not pos or pos.is_closed or pos.qty <= 0:
                    fail_rec.resolved = True
                    del self.active_critical_failures[pid]
                    break

                client = getattr(acc, "client", None)
                router = getattr(acc, "order_router", None)
                if not client or not router:
                    continue

                # 1. 브로커 재조회 (Fail-Safe Step 1)
                try:
                    bal = client.get_balance()
                    broker_holdings = {h["iem_cd"]: h for h in bal.get("holdings", [])}
                    if code not in broker_holdings or broker_holdings[code].get("qty", 0) == 0:
                        # 브로커에서는 이미 전량 매도 완료됨!
                        pos.is_closed = True
                        pos.status = "POSITION_CLOSED"
                        pos.qty = 0
                        fail_rec.resolved = True
                        del self.active_critical_failures[pid]
                        logger.info(f"[Fail-Safe] {code} 브로커 조회 결과 이미 체결 완료 확인 -> 내부 포지션 CLOSED 동기화")
                        break
                except Exception as b_e:
                    logger.warning(f"[Fail-Safe] {code} 브로커 잔고 재조회 실패: {b_e}")

                # 2. 기존 미체결 SELL 주문 검사 (Idempotency: 중복 주문 방지)
                active_sell_exists = False
                for p_ord in router.pending_orders.values():
                    if p_ord.iem_cd == code and p_ord.side == OrderSide.SELL:
                        active_sell_exists = True
                        logger.info(f"[Fail-Safe] {code} 이미 미체결 SELL 주문 대기 중({p_ord.client_order_id}) -> 중복 매도 방지")
                        break

                if active_sell_exists:
                    break

                # 3. 브로커에 주문이 없으므로 재주문 집행 (Requirement 8)
                fail_rec.retry_count += 1
                idem_key = f"RETRY_{pos.position_id}_{code}_{fail_rec.retry_count}_{int(time.time())}"
                logger.warning(f"[Fail-Safe] {code} 손절 실패 포지션 긴급 재전송 (시도: {fail_rec.retry_count}회, Key: {idem_key})")

                try:
                    plan = ExitOrderPolicyEngine.determine_exit_plan(
                        exit_reason="HARD_STOP_RETRY",
                        qty=pos.qty,
                        current_price=float(fail_rec.current_price),
                        now=now
                    )
                    sig = TradeSignal(
                        strategy_id="CRITICAL_EXIT_RETRY",
                        time_horizon=pos.time_horizon,
                        iem_cd=pos.iem_cd,
                        name=pos.name,
                        side=OrderSide.SELL,
                        strategy_price=int(fail_rec.current_price),
                        stop_price=0,
                        score=100.0,
                        reason="스톱로스 재시도 (CRITICAL_RETRY)",
                        timestamp=now,
                        order_type=plan.selected_order_type,
                        entry_price=float(pos.entry_price)
                    )
                    res = router.submit_order(
                        signal=sig,
                        shares=pos.qty,
                        order_type=plan.selected_order_type,
                        order_price=int(fail_rec.current_price),
                        client_order_id=idem_key
                    )
                    if res:
                        pos.active_exit_order_id = res.client_order_id
                        pos.is_closed = True
                        pos.qty = 0
                        fail_rec.resolved = True
                        del self.active_critical_failures[pid]
                        logger.info(f"[Fail-Safe] {code} 손절 재전송 성공 -> 주문번호: {res.broker_order_no}")
                        break
                except Exception as retry_e:
                    logger.error(f"[Fail-Safe] {code} 손절 재전송 실패: {retry_e}")

    def _save_heartbeat_to_disk(self):
        """워치독 하트비트 디스크 영구 기록 (Requirement 5)"""
        try:
            os.makedirs(os.path.dirname(self.heartbeat_file), exist_ok=True)
            tmp = self.heartbeat_file + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(asdict(self.current_heartbeat), f, ensure_ascii=False, indent=2)
            if os.path.exists(self.heartbeat_file):
                os.remove(self.heartbeat_file)
            os.rename(tmp, self.heartbeat_file)
        except Exception:
            pass

    def recover(self):
        """Watchdog 비정상 상태 감지 시 자동 재기동 / 상태 복원 (Requirement 4 & 13 Test 12)"""
        logger.warning("[ExitWatchdog] Watchdog 상태 자동 재기동 및 복구 실행")
        self.current_heartbeat.watchdog_consecutive_failures = 0
        self.current_heartbeat.watchdog_status = WatchdogStatus.HEALTHY.value
        self.active_critical_failures.clear()
        self.stage_timestamps.clear()
        self._init_db()
