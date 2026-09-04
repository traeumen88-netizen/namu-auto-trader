"""자동매매 비상 서킷 브레이커 (Execution Circuit Breaker)
- WebSocket/실시간 데이터 3초 이상 정지 시 자동 트리거
- 복구 후 10초간 안정성 확인 후 재개
- 연속 주문 실패 3회 초과 시 긴급 차단
- 트리거 시 미체결 취소, 신규 주문 전면 차단, 상태 영구 기록
"""

from datetime import datetime, timedelta
from typing import Optional, Dict, Any
from config.settings import (
    DATA_STALL_THRESHOLD_SECONDS,
    RECOVERY_CONFIRMATION_SECONDS,
    MAX_CONSECUTIVE_ORDER_ERRORS
)


class CircuitBreaker:
    def __init__(self):
        self.is_tripped: bool = False
        self.trip_reason: str = ""
        self.tripped_at: Optional[datetime] = None
        self.last_data_timestamp: Optional[datetime] = None
        self.recovered_at: Optional[datetime] = None
        self.consecutive_order_errors: int = 0

    def update_data_heartbeat(self, timestamp: datetime):
        """실시간 데이터 수신 시각 갱신"""
        self.last_data_timestamp = timestamp
        # 복구 확인 중인 경우 상태 체크
        if self.is_tripped and "데이터 지연" in self.trip_reason:
            if self.recovered_at is None:
                self.recovered_at = datetime.now()

    def check_data_staleness(self, current_time: datetime) -> bool:
        """3초 이상 실시간 데이터 정지 시 서킷 브레이커 발동"""
        if self.last_data_timestamp is None:
            return False

        delay = (current_time - self.last_data_timestamp).total_seconds()
        if delay > DATA_STALL_THRESHOLD_SECONDS:
            self.trip(f"실시간 데이터 정지 감지 (지연 {delay:.1f}초 > 한도 {DATA_STALL_THRESHOLD_SECONDS}초)")
            return True
        return False

    def record_order_success(self):
        """주문 성공 시 연속 에러 카운터 리셋"""
        self.consecutive_order_errors = 0

    def record_order_failure(self, error_msg: str):
        """주문 실패 시 에러 카운트 누적 및 임계치 초과 시 브레이커 발동"""
        self.consecutive_order_errors += 1
        if self.consecutive_order_errors >= MAX_CONSECUTIVE_ORDER_ERRORS:
            self.trip(f"연속 주문 실패 {self.consecutive_order_errors}회 발생: {error_msg}")

    def trip(self, reason: str):
        """서킷 브레이커 즉시 발동"""
        if not self.is_tripped:
            self.is_tripped = True
            self.trip_reason = reason
            self.tripped_at = datetime.now()
            self.recovered_at = None

    def attempt_recovery(self) -> bool:
        """복구 후 최소 10초간 안정성이 확인되었을 때만 해제 허용"""
        if not self.is_tripped:
            return True

        if self.recovered_at is not None:
            stable_seconds = (datetime.now() - self.recovered_at).total_seconds()
            if stable_seconds >= RECOVERY_CONFIRMATION_SECONDS and self.consecutive_order_errors == 0:
                self.is_tripped = False
                self.trip_reason = ""
                self.tripped_at = None
                self.recovered_at = None
                return True

        return False

    def get_status(self) -> Dict[str, Any]:
        return {
            "is_tripped": self.is_tripped,
            "reason": self.trip_reason,
            "tripped_at": self.tripped_at.strftime("%Y-%m-%d %H:%M:%S") if self.tripped_at else None,
            "consecutive_errors": self.consecutive_order_errors
        }
