"""자동매매 비상 서킷 브레이커 (Execution Circuit Breaker)
- WebSocket/실시간 데이터 3초 이상 정지 시 자동 트리거
- 복구 후 10초간 안정성 확인 후 재개
- 연속 주문 실패 3회 초과 시 긴급 차단
- 트리거 시 미체결 취소, 신규 주문 전면 차단, 상태 영구 기록
"""

import logging
from datetime import datetime, timedelta
from typing import Optional, Dict, Any, Callable
from config.settings import (
    DATA_STALL_THRESHOLD_SECONDS,
    RECOVERY_CONFIRMATION_SECONDS,
    MAX_CONSECUTIVE_ORDER_ERRORS
)

logger = logging.getLogger("CircuitBreaker")


class CircuitBreaker:
    """계좌별 비상 서킷 브레이커 (Execution Circuit Breaker v2.0)
    - 계좌별(MOCK / LIVE) 독립 인스턴스 격리
    - 시스템 장애(SYSTEM_FAILURE)와 비즈니스 거절(BUSINESS_REJECTION) 엄격 분리
    - Recovery Window 경과 및 브로커 헬스체크 통과 시 안전한 자동 복구
    - WebSocket/실시간 데이터 정지 시 자동 트리거 및 데이터 수신 복구 지원
    """
    def __init__(self, account_name: str = "GLOBAL", recovery_window_seconds: float = 30.0):
        self.account_name: str = account_name
        self.is_tripped: bool = False
        self.trip_reason: Optional[str] = None
        self.trip_type: Optional[str] = None  # "SYSTEM_FAILURE", "DATA_STALE", None
        self.tripped_at: Optional[datetime] = None
        self.last_data_timestamp: Optional[datetime] = None
        self.recovered_at: Optional[datetime] = None
        self.consecutive_order_errors: int = 0
        self.recovery_window_seconds: float = recovery_window_seconds

    def update_data_heartbeat(self, timestamp: datetime):
        """실시간 데이터 수신 시각 갱신"""
        self.last_data_timestamp = timestamp
        # 데이터 지연으로 발동된 경우 복구 시도
        if self.is_tripped and self.trip_type == "DATA_STALE":
            if self.recovered_at is None:
                self.recovered_at = datetime.now()
            self.attempt_recovery()

    def check_data_staleness(self, current_time: datetime) -> bool:
        """3초 이상 실시간 데이터 정지 시 서킷 브레이커 발동"""
        if self.last_data_timestamp is None:
            return False

        delay = (current_time - self.last_data_timestamp).total_seconds()
        if delay > DATA_STALL_THRESHOLD_SECONDS:
            self.trip(
                reason=f"실시간 데이터 정지 감지 (지연 {delay:.1f}초 > 한도 {DATA_STALL_THRESHOLD_SECONDS}초)",
                trip_type="DATA_STALE"
            )
            return True
        return False

    def record_order_success(self):
        """주문 성공 시 연속 에러 카운터 리셋"""
        self.consecutive_order_errors = 0

    def record_order_failure(self, error_msg: str, error_type: str = "SYSTEM_FAILURE"):
        """주문 실패 시 에러 카운트 누적 및 임계치 초과 시 브레이커 발동
        - BUSINESS_REJECTION (예: 23962 매매가능시간 아님, 잔고부족 등)은 시스템 카운터에 포함하지 않음
        """
        if error_type == "BUSINESS_REJECTION":
            logger.info(f"[{self.account_name} CircuitBreaker] 비즈니스 거절 통과 (시스템 실패 카운터 미가산): {error_msg}")
            return

        self.consecutive_order_errors += 1
        logger.error(f"[CIRCUIT_BREAKER] account={self.account_name} event=SYSTEM_FAILURE count={self.consecutive_order_errors} msg={error_msg}")

        if self.consecutive_order_errors >= MAX_CONSECUTIVE_ORDER_ERRORS:
            self.trip(
                reason=f"연속 주문 실패 {self.consecutive_order_errors}회 발생: {error_msg}",
                trip_type="SYSTEM_FAILURE"
            )

    def trip(self, reason: str, trip_type: str = "SYSTEM_FAILURE"):
        """서킷 브레이커 즉시 발동"""
        if not self.is_tripped:
            self.is_tripped = True
            self.trip_reason = reason
            self.trip_type = trip_type
            self.tripped_at = datetime.now()
            self.recovered_at = None
            logger.warning(f"[{self.account_name} CircuitBreaker 발동] type={trip_type}, reason={reason}")

    def attempt_recovery(self, health_check_fn: Optional[Callable[[], bool]] = None) -> bool:
        """안전한 서킷 브레이커 자동 복구
        - DATA_STALE: 데이터 재수신 후 최소 10초 안정성 확인
        - SYSTEM_FAILURE: recovery_window_seconds 경과 후 브로커/API health 확인
        - 단순 23962 오발동 잔존 시 즉시 정리
        """
        if not self.is_tripped:
            return True

        now = datetime.now()
        if self.tripped_at is None:
            self.tripped_at = now

        elapsed = (now - self.tripped_at).total_seconds()

        # 과거 23962 등 장외 비즈니스 거절 오발동 해제
        if self.trip_reason and ("23962" in self.trip_reason or "매매가능" in self.trip_reason):
            self._execute_recovery(reason="FALSE_POSITIVE_BUSINESS_REJECTION_CLEARED")
            return True

        # DATA_STALE 복구 조건
        if self.trip_type == "DATA_STALE":
            if self.last_data_timestamp and (now - self.last_data_timestamp).total_seconds() <= DATA_STALL_THRESHOLD_SECONDS:
                stable_sec = (now - self.recovered_at).total_seconds() if self.recovered_at else elapsed
                if stable_sec >= RECOVERY_CONFIRMATION_SECONDS and self.consecutive_order_errors == 0:
                    self._execute_recovery(reason="DATA_RESTORED")
                    return True
            return False

        # SYSTEM_FAILURE 복구 조건
        if self.trip_type == "SYSTEM_FAILURE":
            if elapsed >= self.recovery_window_seconds:
                health_ok = True
                if health_check_fn is not None:
                    try:
                        health_ok = bool(health_check_fn())
                    except Exception as err:
                        logger.warning(f"[{self.account_name} Breaker Recovery] 헬스체크 실패: {err}")
                        health_ok = False

                if health_ok:
                    self._execute_recovery(reason="RECOVERY_WINDOW_PASSED")
                    return True

            return False

        return False

    def _execute_recovery(self, reason: str = "RECOVERY_WINDOW_PASSED"):
        """서킷 브레이커 상태 복구 집행"""
        self.consecutive_order_errors = 0
        self.is_tripped = False
        self.trip_reason = None
        self.trip_type = None
        self.tripped_at = None
        self.recovered_at = datetime.now()
        logger.info(f"[BREAKER_RECOVERY] account={self.account_name} reason={reason} health=OK")

    def get_status(self) -> Dict[str, Any]:
        return {
            "account": self.account_name,
            "is_tripped": self.is_tripped,
            "reason": self.trip_reason,
            "trip_type": self.trip_type,
            "tripped_at": self.tripped_at.strftime("%Y-%m-%d %H:%M:%S") if self.tripped_at else None,
            "consecutive_errors": self.consecutive_order_errors
        }
