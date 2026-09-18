"""
v16.2 Process Watchdog & Auto-Restart Supervisor (execution/auto_restart_supervisor.py)
--------------------------------------------------------------------------------------
실시간 퀀트 트레이더(live_quant_trader.py)의 무중단 지속 가동을 보장하는 감시 데몬.
- 프로세스 강제 종료, 메모리 부족, 예외 발생 등으로 종료 시 즉시 감지
- 3초 이내 자동 재시작 및 누적 재시작 횟수 기록
- data/trader_process_status.json 파일에 프로세스 생존 상태(PID, 하트비트, 가동시간) 실시간 기록
- 웹 대시보드(web_dashboard.py)와 실시간 연동되어 꺼짐 여부 즉시 확인 가능
"""

import os
import sys
import time
import json
import signal
import subprocess
from datetime import datetime
from typing import Optional

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if BASE_DIR not in sys.path:
    sys.path.insert(0, BASE_DIR)

# UTF-8 콘솔 출력 보정
if sys.platform == "win32":
    try:
        sys.stdout.reconfigure(encoding="utf-8")
        sys.stderr.reconfigure(encoding="utf-8")
    except Exception:
        pass

STATUS_FILE = os.path.join(BASE_DIR, "data", "trader_process_status.json")
LOG_DIR = os.path.join(BASE_DIR, "logs")
os.makedirs(LOG_DIR, exist_ok=True)
os.makedirs(os.path.dirname(STATUS_FILE), exist_ok=True)

# 파이썬 실행 경로 (Windows 기본 가상/로컬 환경 대응)
PYTHON_EXE = sys.executable or os.path.join(os.environ.get("LOCALAPPDATA", ""), "Python", "bin", "python.exe")
TRADER_SCRIPT = os.path.join(BASE_DIR, "execution", "live_quant_trader.py")

class ProcessSupervisor:
    def __init__(self, mode: str = "live", extra_args: list = None):
        self.mode = mode.lower()
        self.extra_args = extra_args or []
        self.restart_count = 0
        self.rapid_failure_count = 0
        self.child_proc: Optional[subprocess.Popen] = None
        self.is_terminating = False
        self.started_at: Optional[datetime] = None
        from config import settings
        self.accounts_to_supervise = []
        if self.mode == "live":
            self.accounts_to_supervise.append(("LIVE", getattr(settings, "ACCOUNT_LIVE", "20201549311")))
        elif self.mode == "mock":
            self.accounts_to_supervise.append(("MOCK", getattr(settings, "ACCOUNT_MOCK", "50001003032")))
        elif self.mode == "dual":
            self.accounts_to_supervise.append(("LIVE", getattr(settings, "ACCOUNT_LIVE", "20201549311")))
            self.accounts_to_supervise.append(("MOCK", getattr(settings, "ACCOUNT_MOCK", "50001003032")))
        else:
            self.accounts_to_supervise.append((self.mode.upper(), getattr(settings, "ACCOUNT_LIVE", "20201549311")))

        self._lock_files = []

        # 종료 시그널 핸들러 등록
        try:
            signal.signal(signal.SIGINT, self._handle_sigint)
            signal.signal(signal.SIGTERM, self._handle_sigterm)
        except Exception:
            pass

    def _acquire_supervisor_lock(self) -> bool:
        """단일 Supervisor 인스턴스 보장을 위한 OS 파일 락 (계좌 단위 격리)"""
        os.makedirs(os.path.join(BASE_DIR, "data", "locks"), exist_ok=True)
        for acct_mode, acct_no in self.accounts_to_supervise:
            lock_path = os.path.join(BASE_DIR, "data", "locks", f"supervisor_{acct_mode.lower()}_{acct_no}.lock")
            try:
                f = open(lock_path, "a+", encoding="utf-8")
                f.seek(0)
                if sys.platform == "win32":
                    import msvcrt
                    msvcrt.locking(f.fileno(), msvcrt.LK_NBLCK, 1)
                else:
                    import fcntl
                    fcntl.flock(f.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                self._lock_files.append(f)
            except (IOError, OSError):
                self._release_supervisor_lock()
                return False
        return True

    def _release_supervisor_lock(self):
        """Supervisor OS 파일 락 해제"""
        for f in self._lock_files:
            try:
                if sys.platform == "win32":
                    import msvcrt
                    f.seek(0)
                    msvcrt.locking(f.fileno(), msvcrt.LK_UNLCK, 1)
                else:
                    import fcntl
                    fcntl.flock(f.fileno(), fcntl.LOCK_UN)
                f.close()
            except Exception:
                pass
        self._lock_files.clear()

    @staticmethod
    def _is_pid_alive(pid: int) -> bool:
        """PID 프로세스 생존 여부 확인 (Windows / POSIX 호환)"""
        if pid <= 0:
            return False
        if sys.platform == "win32":
            try:
                import ctypes
                PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
                SYNCHRONIZE = 0x00100000
                handle = ctypes.windll.kernel32.OpenProcess(
                    PROCESS_QUERY_LIMITED_INFORMATION | SYNCHRONIZE, False, pid
                )
                if not handle:
                    return False
                exit_code = ctypes.c_ulong()
                ctypes.windll.kernel32.GetExitCodeProcess(handle, ctypes.byref(exit_code))
                ctypes.windll.kernel32.CloseHandle(handle)
                return exit_code.value == 259  # STILL_ACTIVE
            except Exception:
                return False
        else:
            try:
                os.kill(pid, 0)
                return True
            except OSError:
                return False

    @staticmethod
    def _wait_for_pid_exit(pid: int, timeout: float = 10.0) -> bool:
        """지정된 PID가 OS 프로세스 목록에서 완전히 소멸될 때까지 대기 (Requirement 6)"""
        deadline = time.time() + timeout
        while time.time() < deadline:
            if not ProcessSupervisor._is_pid_alive(pid):
                return True
            time.sleep(0.5)
        return not ProcessSupervisor._is_pid_alive(pid)

    def _update_status_file(self, status: str, pid: Optional[int] = None, exit_code: Optional[int] = None, error_msg: str = "", backoff_sec: int = 0):
        now_dt = datetime.now()
        uptime_sec = int((now_dt - self.started_at).total_seconds()) if self.started_at else 0
        payload = {
            "status": status,  # "RUNNING", "RESTARTING", "DEGRADED", "STOPPED"
            "mode": self.mode.upper(),
            "pid": pid,
            "supervisor_pid": os.getpid(),
            "restart_count": self.restart_count,
            "rapid_failure_count": self.rapid_failure_count,
            "backoff_sec": backoff_sec,
            "started_at": self.started_at.strftime("%Y-%m-%d %H:%M:%S") if self.started_at else None,
            "uptime_sec": uptime_sec,
            "last_heartbeat": time.time(),
            "last_exit_code": exit_code,
            "last_error": error_msg,
            "updated_at": now_dt.strftime("%Y-%m-%d %H:%M:%S")
        }
        try:
            temp_file = STATUS_FILE + ".tmp"
            with open(temp_file, "w", encoding="utf-8") as f:
                json.dump(payload, f, ensure_ascii=False, indent=2)
            if os.path.exists(STATUS_FILE):
                os.remove(STATUS_FILE)
            os.rename(temp_file, STATUS_FILE)
        except Exception as e:
            pass

    def _handle_sigint(self, signum, frame):
        print("\n[감시기] 사용자의 Ctrl+C 요청을 감지했습니다. 시스템을 안전하게 종료합니다...")
        self.is_terminating = True
        self._terminate_child()
        self._update_status_file("STOPPED", None, 0, "USER_INTERRUPT")
        self._release_supervisor_lock()
        sys.exit(0)

    def _handle_sigterm(self, signum, frame):
        print("\n[감시기] SIGTERM 수신. 안전 종료합니다...")
        self.is_terminating = True
        self._terminate_child()
        self._update_status_file("STOPPED", None, 0, "TERMINATED")
        self._release_supervisor_lock()
        sys.exit(0)

    def _terminate_child(self):
        if self.child_proc and self.child_proc.poll() is None:
            child_pid = self.child_proc.pid
            try:
                self.child_proc.terminate()
                self.child_proc.wait(timeout=5.0)
            except Exception:
                try:
                    self.child_proc.kill()
                    self.child_proc.wait(timeout=3.0)
                except Exception:
                    pass
            if child_pid:
                self._wait_for_pid_exit(child_pid, timeout=5.0)

    def run(self):
        if not self._acquire_supervisor_lock():
            print("=" * 76)
            print(f"🚨 [차단] 이미 동일한 모드({self.mode.upper()})의 Supervisor가 실행 중입니다.")
            print(f"   중복 실행 방지(Singleton Guard)에 의해 현재 프로세스는 즉시 종료됩니다.")
            print("=" * 76)
            sys.exit(0)

        print("=" * 76)
        print(f"🛡️  [무중단 감시기] AI QUANT 자동매매 프로세스 감시 데몬 가동 ({self.mode.upper()} 모드)")
        print(f"   · 감시 대상: {TRADER_SCRIPT}")
        print(f"   · 비정상 종료 감지 시: 3초 후 즉시 자동 재시작")
        print(f"   · 상태 파일: {STATUS_FILE}")
        print("=" * 76)

        while not self.is_terminating:
            cmd = [PYTHON_EXE, TRADER_SCRIPT, f"--{self.mode}"] + self.extra_args
            self.started_at = datetime.now()
            print(f"\n[🔄 프로세스 실행] [{self.started_at.strftime('%H:%M:%S')}] 명령어: {' '.join(cmd)}")
            
            try:
                # 자식 프로세스를 콘솔 입출력 그대로 전달하여 실행
                self.child_proc = subprocess.Popen(cmd)
                child_pid = self.child_proc.pid
                print(f"✅ [프로세스 가동] PID: {child_pid} (누적 재시작: {self.restart_count}회)")
                self._update_status_file("RUNNING", child_pid)

                # 주기적 하트비트 감시 루프
                while True:
                    ret_code = self.child_proc.poll()
                    if ret_code is not None:
                        # 자식 프로세스가 종료됨!
                        break
                    # 살아있는 동안 2초마다 하트비트 갱신
                    self._update_status_file("RUNNING", child_pid)
                    time.sleep(2.0)

                # 루프를 빠져나왔다면 프로세스가 꺼진 것임
                exit_time = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
                print(f"\n" + "!" * 76)
                print(f"🚨 [경고] 트레이더 프로세스(PID: {child_pid})가 종료되었습니다! (시각: {exit_time}, Exit Code: {ret_code})")
                print("!" * 76)

                # 이전 프로세스 완전 종료 대기 및 OS PID 소멸 검증 (Requirement 6)
                try:
                    self.child_proc.wait(timeout=5.0)
                except Exception:
                    try:
                        self.child_proc.kill()
                        self.child_proc.wait(timeout=3.0)
                    except Exception:
                        pass
                self._wait_for_pid_exit(child_pid, timeout=10.0)

                if self.is_terminating:
                    break

                self.restart_count += 1
                runtime_sec = (datetime.now() - self.started_at).total_seconds() if self.started_at else 0.0
                if runtime_sec >= 60.0:
                    # 60초 이상 안정적으로 동작한 경우 급격 재시작 카운터 리셋
                    self.rapid_failure_count = 0
                else:
                    self.rapid_failure_count += 1

                # 지수 백오프 쿨다운 산정 (1~2회: 3초, 3~5회: 5초, 6~10회: 10초, 10회 초과: 30초)
                if self.rapid_failure_count <= 2:
                    cooldown_sec = 3
                elif self.rapid_failure_count <= 5:
                    cooldown_sec = 5
                elif self.rapid_failure_count <= 10:
                    cooldown_sec = 10
                else:
                    cooldown_sec = 30

                status_str = "DEGRADED" if self.rapid_failure_count >= 6 else "RESTARTING"
                err_msg = f"Unexpected process exit (code: {ret_code}, runtime: {runtime_sec:.1f}s, rapid_failures: {self.rapid_failure_count})"
                self._update_status_file(status_str, None, ret_code, err_msg, backoff_sec=cooldown_sec)

                print(f"🔄 [자동 복구] {cooldown_sec}초 후 프로세스를 자동으로 재시작합니다... (누적: {self.restart_count}회, 연속급격종료: {self.rapid_failure_count}회, 백오프: {cooldown_sec}s, 상태: {status_str})")
                for s in range(cooldown_sec, 0, -1):
                    time.sleep(1.0)

            except Exception as e:
                print(f"[감시기 오류] 자식 프로세스 실행 실패: {e}")
                self.rapid_failure_count += 1
                cooldown_sec = min(30, 3 * (2 ** min(max(0, self.rapid_failure_count - 1), 4)))
                self._update_status_file("DEGRADED" if self.rapid_failure_count >= 6 else "RESTARTING", None, -1, str(e), backoff_sec=cooldown_sec)
                time.sleep(cooldown_sec)

def main():
    import argparse
    parser = argparse.ArgumentParser(description="AI Quant 자동 재시작 프로세스 감시기")
    parser.add_argument("--live", action="store_true", help="실전투자 모드 감시")
    parser.add_argument("--mock", action="store_true", help="모의투자 모드 감시")
    parser.add_argument("--dual", action="store_true", help="듀얼 모드 감시")
    parser.add_argument("--test-signal", action="store_true", help="테스트 시그널 모드")
    args = parser.parse_args()

    if args.live:
        mode = "live"
    elif args.mock:
        mode = "mock"
    else:
        mode = "dual"

    extra = []
    if args.test_signal:
        extra.append("--test-signal")

    supervisor = ProcessSupervisor(mode=mode, extra_args=extra)
    supervisor.run()

if __name__ == "__main__":
    main()
