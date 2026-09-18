"""[Execution Integrity] 계좌별 단일 프로세스 실행 락 (AccountProcessLock)
- 동일 계좌(trading_mode + account_no)에 대한 중복 트레이더 프로세스 기동 원천 차단
- LIVE와 MOCK은 서로 다른 계좌번호/모드를 가지므로 독립적 동시 실행 완벽 보장
- OS 레벨 파일 락(msvcrt on Windows, fcntl on Unix) 기반으로 프로세스 비정상 종료 시 자동 해제
"""

import os
import sys
import json
import time
import logging
from datetime import datetime
from typing import Optional, List, Tuple

logger = logging.getLogger("AccountProcessLock")

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
LOCKS_DIR = os.path.join(BASE_DIR, "data", "locks")


class DuplicateAccountProcessError(RuntimeError):
    """동일 계좌에 대한 프로세스가 이미 실행 중일 때 발생하는 예외"""
    pass


class AccountProcessLock:
    """계좌 단위 OS 파일 락"""

    def __init__(self, trading_mode: str, account_no: str):
        self.trading_mode = str(trading_mode).strip().upper()
        self.account_no = str(account_no).strip()
        self.runtime_key = f"{self.trading_mode}:{self.account_no}"
        os.makedirs(LOCKS_DIR, exist_ok=True)
        # 안전한 파일명 생성
        safe_key = f"account_{self.trading_mode.lower()}_{self.account_no}.lock"
        self.lock_file_path = os.path.join(LOCKS_DIR, safe_key)
        self._file_obj = None
        self._is_locked = False

    def acquire(self) -> Tuple[bool, Optional[int]]:
        """
        락 획득 시도 (Non-blocking)
        :return: (성공여부, 기존 실행 중인 PID 또는 None)
        """
        try:
            self._file_obj = open(self.lock_file_path, "a+", encoding="utf-8")
            self._file_obj.seek(0)

            if sys.platform == "win32":
                import msvcrt
                msvcrt.locking(self._file_obj.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(self._file_obj.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)

            # 락 획득 성공 -> PID 및 획득 정보 기록
            self._is_locked = True
            self._file_obj.seek(0)
            self._file_obj.truncate()
            info = {
                "pid": os.getpid(),
                "trading_mode": self.trading_mode,
                "account_no": self.account_no,
                "acquired_at": datetime.now().isoformat(),
            }
            self._file_obj.write(json.dumps(info))
            self._file_obj.flush()
            logger.info(f"[ACCOUNT_LOCK_ACQUIRED] {self.runtime_key} (PID: {os.getpid()})")
            return True, None

        except (IOError, OSError):
            existing_pid = self._read_existing_pid()
            logger.warning(
                f"[DUPLICATE_ACCOUNT_PROCESS] Already running on {self.runtime_key} "
                f"(Existing PID: {existing_pid}, My PID: {os.getpid()}) -> Aborting."
            )
            if self._file_obj:
                try:
                    self._file_obj.close()
                except Exception:
                    pass
                self._file_obj = None
            self._is_locked = False
            return False, existing_pid

    def _read_existing_pid(self) -> Optional[int]:
        """락을 잡고 있는 기존 프로세스의 PID 조회"""
        try:
            if os.path.exists(self.lock_file_path):
                with open(self.lock_file_path, "r", encoding="utf-8") as f:
                    content = f.read().strip()
                    if content:
                        data = json.loads(content)
                        return data.get("pid")
        except Exception:
            pass
        return None

    def release(self):
        """락 해제"""
        if self._file_obj and self._is_locked:
            try:
                if sys.platform == "win32":
                    import msvcrt
                    self._file_obj.seek(0)
                    msvcrt.locking(self._file_obj.fileno(), msvcrt.LK_UNLCK, 1)
                else:
                    import fcntl
                    fcntl.flock(self._file_obj.fileno(), fcntl.LOCK_UN)
                self._file_obj.close()
            except Exception as e:
                logger.warning(f"Error releasing lock for {self.runtime_key}: {e}")
            finally:
                self._file_obj = None
                self._is_locked = False
                logger.info(f"[ACCOUNT_LOCK_RELEASED] {self.runtime_key}")

    def __enter__(self):
        acquired, existing_pid = self.acquire()
        if not acquired:
            raise DuplicateAccountProcessError(
                f"DUPLICATE_ACCOUNT_PROCESS: {self.runtime_key} is already locked by PID {existing_pid}"
            )
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        self.release()


class AccountLockManager:
    """트레이더 모드에 따른 계좌 락 일괄 관리자"""

    def __init__(self, mode: str, account_live: str, account_mock: str):
        self.mode = str(mode).lower()
        self.account_live = str(account_live).strip()
        self.account_mock = str(account_mock).strip()
        self.acquired_locks: List[AccountProcessLock] = []

    def acquire_all(self) -> Tuple[bool, Optional[str]]:
        """
        트레이더 구동 모드에 필요한 모든 계좌 락 획득
        - 'live': LIVE 계좌 락
        - 'mock': MOCK 계좌 락
        - 'dual': LIVE + MOCK 계좌 락 모두 획득 (어느 하나라도 실패 시 전체 해제)
        """
        targets = []
        if self.mode == "live":
            targets.append(("LIVE", self.account_live))
        elif self.mode == "mock":
            targets.append(("MOCK", self.account_mock))
        elif self.mode == "dual":
            targets.append(("LIVE", self.account_live))
            targets.append(("MOCK", self.account_mock))
        else:
            targets.append((self.mode.upper(), self.account_live))

        for t_mode, act_no in targets:
            lock = AccountProcessLock(trading_mode=t_mode, account_no=act_no)
            ok, pid = lock.acquire()
            if not ok:
                # 획득 실패 -> 기획득한 락 롤백
                err_msg = f"DUPLICATE_ACCOUNT_PROCESS: {t_mode}:{act_no} (Existing PID: {pid})"
                self.release_all()
                return False, err_msg
            self.acquired_locks.append(lock)

        return True, None

    def release_all(self):
        """획득된 모든 락 해제"""
        for lock in self.acquired_locks:
            try:
                lock.release()
            except Exception:
                pass
        self.acquired_locks.clear()

    def __enter__(self):
        ok, err = self.acquire_all()
        if not ok:
            raise DuplicateAccountProcessError(err)
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        self.release_all()
