"""정밀 시간 동기화 및 지연시간(Latency) 추적 모듈 (Time Sync v9.3)
- Execution-Grade Specification v9.3
- NTP 오프셋 동기화 및 타임아웃/오프라인 시 시스템 시간 Fallback
- perf_counter 기반 마이크로초/밀리초 정밀 단계별 지연시간 측정
"""

import time
import socket
import struct
import logging
from datetime import datetime, timedelta
from typing import Optional, Dict

logger = logging.getLogger("TimeSync")


class TimeSync:
    """NTP 정밀 시간 동기화기"""
    _offset_ms: float = 0.0
    _last_synced: Optional[datetime] = None
    _is_synced: bool = False

    @classmethod
    def sync_ntp(cls, host: str = "time.google.com", timeout: float = 1.0) -> bool:
        """
        NTP 서버(기본값 time.google.com)와 UDP 통신하여 시간 오프셋(ms) 산출
        방화벽 차단 또는 오프라인 시 예외를 발생시키지 않고 0.0ms 유지
        """
        NTP_PACKET_FORMAT = "!12I"
        NTP_DELTA = 2208988800  # 1970-01-01 to 1900-01-01 in seconds
        client = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        client.settimeout(timeout)

        data = b"\x1b" + 47 * b"\0"
        t0 = time.time()
        try:
            client.sendto(data, (host, 123))
            data, _ = client.recvfrom(1024)
            t3 = time.time()
            if data:
                unpacked = struct.unpack(NTP_PACKET_FORMAT, data[0:struct.calcsize(NTP_PACKET_FORMAT)])
                # t1: Server receive time, t2: Server transmit time
                t1 = unpacked[8] + float(unpacked[9]) / 2**32 - NTP_DELTA
                t2 = unpacked[10] + float(unpacked[11]) / 2**32 - NTP_DELTA
                # NTP Offset formula: ((t1 - t0) + (t2 - t3)) / 2
                offset_sec = ((t1 - t0) + (t2 - t3)) / 2.0
                cls._offset_ms = round(offset_sec * 1000.0, 2)
                cls._last_synced = datetime.now()
                cls._is_synced = True
                logger.info(f"NTP Time Synced with {host}: offset = {cls._offset_ms}ms")
                return True
        except Exception as e:
            logger.debug(f"NTP sync failed ({e}), defaulting to system clock with 0.0ms offset.")
            cls._offset_ms = 0.0
            cls._is_synced = False
        finally:
            client.close()

        return False

    @classmethod
    def get_offset_ms(cls) -> float:
        return cls._offset_ms

    @classmethod
    def is_synced(cls) -> bool:
        return cls._is_synced

    @classmethod
    def get_precise_time(cls) -> datetime:
        """NTP 오프셋이 적용된 정밀 datetime 반환"""
        base = datetime.now()
        if cls._offset_ms != 0.0:
            return base + timedelta(milliseconds=cls._offset_ms)
        return base


class StageTimer:
    """단일 파이프라인 단계 실행 시간(ms) 측정 컨텍스트 매니저"""
    def __init__(self):
        self._start_perf = 0.0
        self.elapsed_ms = 0.0

    def __enter__(self):
        self._start_perf = time.perf_counter()
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        self.elapsed_ms = round((time.perf_counter() - self._start_perf) * 1000.0, 2)
