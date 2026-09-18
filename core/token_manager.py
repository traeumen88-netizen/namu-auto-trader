"""나무증권 중앙 집중식 토큰 관리자 (Centralized TokenManager)
- Process-wide Singleton + Multi-process File Lock + SingleFlight
- 24시간 토큰 재사용 보장 및 불필요한 재발급/문자 알림 원천 차단
- 명시적 토큰 만료(401, IGW40043, IGW40002) 시에만 배타적 Lock 획득 후 갱신
- 감사 로그(TOKEN_ISSUED, TOKEN_REUSED, TOKEN_REFRESHED) 및 상태 텔레메트리 제공
"""

import os
import time
import json
import logging
import hashlib
import threading
from datetime import datetime
from pathlib import Path
from typing import Dict, Any, Optional

import config
import nhplug
from nhplug.errors import NhplugError

logger = logging.getLogger("TokenManager")


class CrossProcessLock:
    """크로스 프로세스 파일 락 (Windows/Linux 공통 지원, Stale Lock 방어 내장)"""
    def __init__(self, lock_file: Path, timeout: float = 10.0, stale_sec: float = 20.0):
        self.lock_file = lock_file
        self.timeout = timeout
        self.stale_sec = stale_sec
        self.fd = None

    def acquire(self) -> bool:
        start_t = time.time()
        self.lock_file.parent.mkdir(parents=True, exist_ok=True)
        while time.time() - start_t < self.timeout:
            try:
                # O_EXCL 플래그로 원자적 파일 생성 락 획득
                self.fd = os.open(str(self.lock_file), os.O_CREAT | os.O_EXCL | os.O_RDWR)
                # 락 획득 시 현재 시각 및 PID 기록
                os.write(self.fd, f"{os.getpid()}:{time.time()}".encode("utf-8"))
                return True
            except FileExistsError:
                # 이미 락 파일이 존재하는 경우: Stale Lock(프로세스 비정상 종료) 여부 점검
                try:
                    mtime = self.lock_file.stat().st_mtime
                    if time.time() - mtime > self.stale_sec:
                        logger.warning(f"[Lock] 오래된 잔여 락 감지 ({time.time() - mtime:.1f}s) - 락 강제 회수")
                        self.lock_file.unlink(missing_ok=True)
                        continue
                except Exception:
                    pass
                time.sleep(0.1)
            except Exception as e:
                logger.debug(f"[Lock 획득 대기] {e}")
                time.sleep(0.1)
        return False

    def release(self):
        if self.fd is not None:
            try:
                os.close(self.fd)
            except Exception:
                pass
            self.fd = None
        try:
            self.lock_file.unlink(missing_ok=True)
        except Exception:
            pass

    def __enter__(self):
        acquired = self.acquire()
        if not acquired:
            logger.warning("[Lock] 파일 락 획득 타임아웃 - 동시성 제어 기본 모드로 진행")
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        self.release()


class TokenManager:
    """중앙 집중식 API 토큰 관리자 (Centralized Token Manager)"""
    _instance: Optional['TokenManager'] = None
    _singleton_lock = threading.Lock()

    @classmethod
    def get_instance(cls) -> 'TokenManager':
        with cls._singleton_lock:
            if cls._instance is None:
                cls._instance = cls()
            return cls._instance

    def __init__(self):
        self.app_key = config.APP_KEY
        self.app_secret = config.APP_SECRET
        self.auth_url = "https://api.nhplug.com:8443"
        
        # 상태 필드
        self.access_token: Optional[str] = None
        self.issued_at: float = 0.0
        self.expires_at: float = 0.0
        self.token_status: str = "INITIAL"  # INITIAL, ACTIVE, EXPIRED, REFRESHING, ERROR
        self.refresh_count: int = 0
        self.last_refresh_reason: str = "SYSTEM_INIT"
        self.last_token_error: Optional[str] = None
        self.last_refresh_timestamp: float = 0.0
        
        # 동시성 제어 (In-process Thread Lock + Cross-process File Lock)
        self._thread_lock = threading.Lock()
        cache_dir = Path.home() / ".nhplug"
        cache_dir.mkdir(parents=True, exist_ok=True)
        self.lock_file = cache_dir / "token_refresh.lock"
        self.process_lock = CrossProcessLock(self.lock_file)

        # 선제적 자동 갱신 워치독 (만료 30분 전 사전 자동 발급 & 장 시작 전 점검)
        self.proactive_refresh_threshold_sec: float = 1800.0  # 만료 30분 전 자동 갱신
        self._watchdog_thread: Optional[threading.Thread] = None
        self._stop_watchdog = threading.Event()

        # 초기 디스크 캐시 로드 (네트워크 호출 전혀 없이 기존 24h 캐시만 안전 로딩)
        self._load_from_disk_cache()
        self._start_watchdog()

    def _start_watchdog(self):
        """백그라운드 토큰 수명 감시 및 선제적 자동 갱신 워치독"""
        if self._watchdog_thread is None or not self._watchdog_thread.is_alive():
            self._watchdog_thread = threading.Thread(
                target=self._watchdog_loop,
                name="TokenAutoRefreshWatchdog",
                daemon=True
            )
            self._watchdog_thread.start()
            logger.info("[TOKEN_WATCHDOG] 토큰 자동 갱신 워치독 가동 (만료 30분 전 선제 발급 + 평일 08:30 장전 점검)")

    def _watchdog_loop(self):
        """주기적(60초)으로 잔여 시간을 확인하여 시간 부족 시 자동 갱신"""
        while not self._stop_watchdog.is_set():
            try:
                now = time.time()
                # 토큰 캐시 최신 상태 동기화
                if not self.access_token or self.expires_at <= now:
                    self._load_from_disk_cache()

                if self.expires_at > 0:
                    rem_sec = self.expires_at - now
                    now_dt = datetime.fromtimestamp(now)

                    # [조건 1] 만료 30분(1800초) 이내 도래 시 자동 갱신
                    if rem_sec <= self.proactive_refresh_threshold_sec:
                        logger.info(f"[TOKEN_AUTO_REFRESH] 토큰 잔여 시간 부족 ({rem_sec/60.0:.1f}분 남음) -> 선제적 자동 갱신 수행")
                        self.get_token(force=True, reason=f"PROACTIVE_LOW_TTL_{int(rem_sec/60)}M")

                    # [조건 2] 평일 08:30~08:50 (장 시작 전) 점검:
                    # 당일 장 마감(15:30)까지 잔여 시간이 부족한 경우 (약 7시간 미만) 미리 아침에 24시간 토큰으로 갱신
                    elif now_dt.weekday() < 5 and (now_dt.hour == 8 and 30 <= now_dt.minute <= 50):
                        seconds_to_market_close = ((15 - now_dt.hour) * 3600) + ((30 - now_dt.minute) * 60)
                        if rem_sec < seconds_to_market_close + 1800:
                            logger.info(f"[TOKEN_AUTO_REFRESH] 장 시작 전 토큰 점검: 오늘 장중 만료 예정 ({rem_sec/3600.0:.1f}h 남음) -> 장전 선제 갱신")
                            self.get_token(force=True, reason="MARKET_PREP_PRE_EXPIRY")

            except Exception as e:
                logger.debug(f"[TOKEN_WATCHDOG] 감시 루프 예외: {e}")

            # 60초 간격 점검
            self._stop_watchdog.wait(60.0)

    def _get_cache_file_path(self) -> Path:
        key = hashlib.sha256(f"{self.app_key}|{self.auth_url}".encode("utf-8")).hexdigest()[:12]
        return Path.home() / ".nhplug" / f"token-{key}.json"

    def _load_from_disk_cache(self) -> bool:
        """디스크 파일 캐시에서 토큰 로드 (만료 60초 전까지 유효로 인정)"""
        cache_file = self._get_cache_file_path()
        if not cache_file.exists():
            return False
        try:
            data = json.loads(cache_file.read_text(encoding="utf-8"))
            tok = data.get("token")
            exp = float(data.get("exp", 0.0))
            now = time.time()
            if tok and exp > now + 60.0:
                self.access_token = tok
                self.expires_at = exp
                self.issued_at = exp - 86400.0  # NH 토큰 기본 수명 24시간(86,400초)
                self.token_status = "ACTIVE"
                logger.info(f"[TOKEN_REUSED] 유효한 디스크 캐시 토큰 로드 완료 (잔여 유효시간: {(exp - now)/3600.0:.1f}시간)")
                return True
        except Exception as e:
            logger.debug(f"디스크 캐시 읽기 실패: {e}")
        return False

    def is_valid(self) -> bool:
        """토큰 유효성 검사 (메모리 + 디스크 캐시 통합 점검)"""
        now = time.time()
        if self.access_token and self.expires_at > now + 60.0:
            return True
        return self._load_from_disk_cache()

    def get_token(self, force: bool = False, reason: str = "") -> str:
        """
        안전한 토큰 반환 메서드 (SingleFlight + Multi-process File Lock)
        - 유효 토큰이 있으면 0ms 즉시 반환
        - 만료 또는 force=True인 경우에만 락 획득 후 단 1회만 발급 수행
        """
        now = time.time()

        # 잔여 시간이 5분(300초) 이하로 임박한 경우 선제적으로 자동 갱신 트리거
        if not force and self.expires_at > 0 and (self.expires_at - now <= 300.0):
            force = True
            reason = reason or "PROACTIVE_EXPIRY_SOON"

        # 1. 빠른 경로: 이미 유효한 토큰이 메모리 또는 디스크에 존재하면 즉시 반환
        if not force and self.is_valid():
            return self.access_token

        # 2. 토큰 갱신 경로 (In-process Thread Lock으로 Worker 동시 요청 병합)
        with self._thread_lock:
            # 락 획득 후 재검사 (Double Checked Locking: 이전 스레드가 이미 갱신 완료했는지 확인)
            if self.is_valid():
                # 직전 5초 이내에 갱신 완료된 토큰이 있다면 force=True 요청이라도 중복 발급 방지하고 즉시 공유
                if not force or (time.time() - self.last_refresh_timestamp < 5.0):
                    return self.access_token

            # 3. 크로스 프로세스 파일 락 획득 (여러 Process/Worker 간 단 1회 갱신 보장)
            with self.process_lock:
                # 다른 프로세스가 이미 갱신했을 수 있으므로 디스크 캐시 재로드
                if self._load_from_disk_cache():
                    if not force or (time.time() - self.last_refresh_timestamp < 5.0):
                        return self.access_token

                # 4. 실제 토큰 발급 수행
                self.token_status = "REFRESHING"
                self.last_refresh_reason = reason or ("FORCED" if force else "EXPIRED")
                try:
                    logger.info(f"[TOKEN_REFRESH] 나무증권 접근 토큰 발급 요청 중... (사유: {self.last_refresh_reason})")
                    # nhplug.auth.get_token(force=True)를 통해 공식 엔드포인트 1회 발급
                    new_token = nhplug.get_token(force=True)
                    self.access_token = new_token
                    self.issued_at = time.time()
                    self.expires_at = self.issued_at + 86400.0  # 24시간
                    self.token_status = "ACTIVE"
                    self.refresh_count += 1
                    self.last_refresh_timestamp = time.time()
                    self.last_token_error = None
                    
                    logger.info(f"[TOKEN_ISSUED] 신규 접근 토큰 발급 성공 (누적 발급 횟수: {self.refresh_count}회, 만료: 24시간 후)")

                    # 토큰 발급 빈도 감시 및 이상 감지 경고
                    if self.refresh_count > 5:
                        logger.warning(f"[TOKEN_HEALTH_WARNING] 당일 토큰 발급 횟수가 {self.refresh_count}회에 도달했습니다. 비정상 재발급 여부를 점검하세요.")

                    return self.access_token

                except Exception as e:
                    self.token_status = "ERROR"
                    self.last_token_error = str(e)
                    logger.error(f"[TOKEN_REFRESH_FAILED] 접근 토큰 발급 실패: {e}")
                    raise e

    def mark_invalid(self, error_code: str = "", reason: str = ""):
        """실제 토큰 만료/무효화 응답(401, IGW40043 등) 수신 시 상태 변경"""
        logger.warning(f"[TOKEN_INVALID] 접근 토큰 무효화 플래그 수신 (코드: {error_code}, 사유: {reason})")
        self.token_status = "EXPIRED"
        self.last_token_error = f"{error_code}: {reason}"
        self.access_token = None
        self.expires_at = 0.0

    def get_telemetry(self) -> Dict[str, Any]:
        """대시보드 및 시스템 모니터링용 토큰 텔레메트리 반환"""
        now = time.time()
        rem_sec = max(0.0, self.expires_at - now)
        rem_h = int(rem_sec // 3600)
        rem_m = int((rem_sec % 3600) // 60)
        
        age_sec = max(0.0, now - self.issued_at) if self.issued_at > 0 else 0.0
        age_h = int(age_sec // 3600)
        age_m = int((age_sec % 3600) // 60)

        return {
            "status": self.token_status if self.is_valid() else "EXPIRED",
            "is_valid": self.is_valid(),
            "age_text": f"{age_h}h {age_m}m",
            "expires_at": time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(self.expires_at)) if self.expires_at > 0 else "--",
            "remaining_ttl_sec": round(rem_sec, 1),
            "remaining_ttl_text": f"{rem_h}h {rem_m}m",
            "refresh_count": self.refresh_count,
            "last_refresh_reason": self.last_refresh_reason,
            "last_token_error": self.last_token_error or "NONE",
            "last_refresh_time": time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(self.last_refresh_timestamp)) if self.last_refresh_timestamp > 0 else "--",
            "auto_refresh_enabled": True,
            "auto_refresh_policy": "만료 30분 전 선제 발급 + 평일 08:30 장전 사전 점검",
        }
