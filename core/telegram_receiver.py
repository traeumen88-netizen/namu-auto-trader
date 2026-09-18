"""
core/telegram_receiver.py
Telegram Bot API getUpdates Long-Polling 수신기
데몬 스레드로 백그라운드에서 동작하며 메시지 수신, 인증, ping/pong 및 피드백 파이프라인 연동 수행
단일 RX Guardian, HTTP 409 Exponential Backoff, Offset 전진 영속화 및 텔레메트리 분리 지원
"""

import os
import sys
import time
import json
import uuid
import hashlib
import logging
import sqlite3
import threading
import requests
from datetime import datetime
from typing import Optional, Dict, Any, List

logger = logging.getLogger("TelegramReceiver")
logger.propagate = False
if not logger.handlers:
    handler = logging.StreamHandler()
    handler.setFormatter(logging.Formatter("[%(asctime)s] %(levelname)s: [TELEGRAM RX] %(message)s"))
    logger.addHandler(handler)
    logger.setLevel(logging.INFO)


def mask_token(token: Optional[str]) -> str:
    """토큰 문자열 마스킹 (보안 유지)"""
    if not token:
        return "NONE"
    if len(token) <= 10:
        return "***"
    return f"{token[:6]}...{token[-4:]}"


def compute_token_hash(token: Optional[str]) -> str:
    """토큰 SHA-256 해시 생성 (토큰 원문 노출 없이 동일성 비교)"""
    if not token:
        return "NONE"
    return hashlib.sha256(token.encode("utf-8")).hexdigest()[:12]


class TelegramReceiver:
    """Telegram Bot getUpdates Long-Polling 데몬 수신기 (Single Consumer Guardian & 409 Backoff)"""

    _active_instance: Optional["TelegramReceiver"] = None
    _class_lock = threading.Lock()

    def __init__(
        self,
        notifier=None,
        token: Optional[str] = None,
        allowed_chat_id: Optional[str] = None,
        db_path: str = "data/operational_v16.db",
        poll_timeout: int = 2
    ):
        self.notifier = notifier
        self.token = token
        self.allowed_chat_id = str(allowed_chat_id) if allowed_chat_id is not None else None
        self.db_path = db_path
        self.poll_timeout = poll_timeout
        self.poll_interval = 0.5

        self.instance_id = f"RX-{os.getpid()}-{uuid.uuid4().hex[:4]}"
        self.last_update_id = 0
        self.running = False
        self.thread: Optional[threading.Thread] = None
        self._lock = threading.Lock()
        self._poll_lock = threading.Lock()
        self._lock_file = None
        self.lock_file_path = None
        self.owner_file_path = None
        self.last_telemetry: Dict[str, Any] = {}

        # 메트릭 카운터 (Requirement 5)
        self.telegram_getupdates_requests_total: int = 0
        self.telegram_getupdates_409_total: int = 0
        self.consecutive_409_errors: int = 0
        self.backoff_seconds: float = 0.0
        self.health_state: str = "HEALTHY"

        # 건강도 추적 변수
        self.last_poll_time: Optional[datetime] = None
        self.last_poll_ok: bool = True
        self.last_received_at: Optional[datetime] = None
        self.consecutive_errors: int = 0
        self.last_error_message: Optional[str] = None

        # DB 초기화 및 영속화된 update_id 복원
        self._init_db()
        self.last_update_id = self._load_last_update_id()

    def _acquire_cross_process_lock(self) -> bool:
        """OS 파일 락으로 복수 프로세스 간 getUpdates 단일 consumer 보장"""
        lock_dir = os.path.dirname(self.db_path) if self.db_path else "data"
        if not lock_dir:
            lock_dir = "data"
        os.makedirs(lock_dir, exist_ok=True)
        self.lock_file_path = os.path.join(lock_dir, "telegram_receiver.lock")
        self.owner_file_path = os.path.join(lock_dir, "telegram_receiver_owner.json")
        try:
            self._lock_file = open(self.lock_file_path, "a+", encoding="utf-8")
            self._lock_file.seek(0)
            if sys.platform == "win32":
                import msvcrt
                msvcrt.locking(self._lock_file.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(self._lock_file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)

            # 락 획득 성공 시 소유 프로세스 메타데이터 영속화 (Requirement 4)
            owner_info = {
                "pid": os.getpid(),
                "ppid": os.getppid() if hasattr(os, "getppid") else -1,
                "instance_id": self.instance_id,
                "token_hash": compute_token_hash(self.token),
                "lock_file": self.lock_file_path,
                "acquired_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            }
            try:
                temp_owner = self.owner_file_path + f".{os.getpid()}.tmp"
                with open(temp_owner, "w", encoding="utf-8") as of:
                    json.dump(owner_info, of, indent=2, ensure_ascii=False)
                if os.path.exists(self.owner_file_path):
                    try:
                        os.remove(self.owner_file_path)
                    except Exception:
                        pass
                os.replace(temp_owner, self.owner_file_path)
            except Exception as e:
                logger.warning(f"[TELEGRAM RX] owner info write error: {e}")

            return True
        except (IOError, OSError):
            if self._lock_file:
                try:
                    self._lock_file.close()
                except Exception:
                    pass
                self._lock_file = None

            # 이미 락을 보유 중인 프로세스 PID 확인 및 보고
            owner_pid = "UNKNOWN"
            if self.owner_file_path and os.path.exists(self.owner_file_path):
                try:
                    with open(self.owner_file_path, "r", encoding="utf-8") as of:
                        owner_data = json.load(of)
                        owner_pid = owner_data.get("pid", "UNKNOWN")
                except Exception:
                    pass
            logger.warning(
                f"[TELEGRAM RX] duplicate receiver start blocked (cross-process lock held by PID: {owner_pid})"
            )
            return False

    def _release_cross_process_lock(self):
        """OS 파일 락 안전 해제 및 소유자 메타데이터 정리"""
        try:
            if hasattr(self, "owner_file_path") and self.owner_file_path and os.path.exists(self.owner_file_path):
                try:
                    with open(self.owner_file_path, "r", encoding="utf-8") as of:
                        owner_data = json.load(of)
                    if owner_data.get("pid") == os.getpid():
                        os.remove(self.owner_file_path)
                except Exception:
                    pass
        except Exception:
            pass

        if self._lock_file:
            try:
                if sys.platform == "win32":
                    import msvcrt
                    self._lock_file.seek(0)
                    msvcrt.locking(self._lock_file.fileno(), msvcrt.LK_UNLCK, 1)
                else:
                    import fcntl
                    fcntl.flock(self._lock_file.fileno(), fcntl.LOCK_UN)
                self._lock_file.close()
            except Exception:
                pass
            finally:
                self._lock_file = None

    def _get_connection(self):
        """SQLite 연결 생성"""
        dir_name = os.path.dirname(self.db_path)
        if dir_name:
            os.makedirs(dir_name, exist_ok=True)
        return sqlite3.connect(self.db_path, timeout=10.0)

    def _init_db(self):
        """수신 상태 및 메시지 중복 방지 테이블 초기화"""
        try:
            with self._lock:
                conn = self._get_connection()
                try:
                    conn.execute("""
                    CREATE TABLE IF NOT EXISTS telegram_rx_state (
                        key TEXT PRIMARY KEY,
                        value TEXT,
                        updated_at TEXT
                    )
                    """)
                    conn.execute("""
                    CREATE TABLE IF NOT EXISTS telegram_processed_messages (
                        message_id INTEGER PRIMARY KEY,
                        chat_id TEXT,
                        received_at TEXT
                    )
                    """)
                    conn.commit()
                finally:
                    conn.close()
        except Exception as e:
            logger.error(f"Telegram 수신 상태 DB 초기화 실패: {e}")

    def _load_last_update_id(self) -> int:
        """영속 저장소에서 last_update_id 로드"""
        try:
            with self._lock:
                conn = self._get_connection()
                try:
                    cur = conn.cursor()
                    cur.execute("SELECT value FROM telegram_rx_state WHERE key = 'last_update_id'")
                    row = cur.fetchone()
                    if row and row[0]:
                        return int(row[0])
                finally:
                    conn.close()
        except Exception as e:
            logger.warning(f"last_update_id 로드 실패 (0으로 초기화): {e}")
        return 0

    def _save_last_update_id(self, update_id: int):
        """영속 저장소에 last_update_id 저장"""
        self.last_update_id = update_id
        try:
            now_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            with self._lock:
                conn = self._get_connection()
                try:
                    conn.execute(
                        "INSERT OR REPLACE INTO telegram_rx_state (key, value, updated_at) VALUES (?, ?, ?)",
                        ("last_update_id", str(update_id), now_str)
                    )
                    conn.commit()
                finally:
                    conn.close()
        except Exception as e:
            logger.error(f"last_update_id 저장 실패 ({update_id}): {e}")

    def _is_message_processed(self, message_id: int) -> bool:
        """메시지 중복 처리 여부 검사"""
        if not message_id:
            return False
        try:
            with self._lock:
                conn = self._get_connection()
                try:
                    cur = conn.cursor()
                    cur.execute("SELECT 1 FROM telegram_processed_messages WHERE message_id = ?", (message_id,))
                    return cur.fetchone() is not None
                finally:
                    conn.close()
        except Exception as e:
            logger.warning(f"메시지 중복 확인 오류: {e}")
            return False

    def _mark_message_processed(self, message_id: int, chat_id: str):
        """메시지 처리 완료 기록 (중복 방지)"""
        if not message_id:
            return
        try:
            now_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            with self._lock:
                conn = self._get_connection()
                try:
                    conn.execute(
                        "INSERT OR IGNORE INTO telegram_processed_messages (message_id, chat_id, received_at) VALUES (?, ?, ?)",
                        (message_id, str(chat_id), now_str)
                    )
                    conn.commit()
                finally:
                    conn.close()
        except Exception as e:
            logger.warning(f"메시지 처리 기록 저장 실패: {e}")

    def start(self) -> bool:
        """백그라운드 수신 데몬 스레드 시작 (단일 RX Guardian 적용)"""
        with TelegramReceiver._class_lock:
            # 1. 프로세스 내 단일 인스턴스 검증 (Requirement 2 & 6)
            if TelegramReceiver._active_instance is not None and TelegramReceiver._active_instance is not self:
                if TelegramReceiver._active_instance.running and TelegramReceiver._active_instance.thread and TelegramReceiver._active_instance.thread.is_alive():
                    logger.warning("[TELEGRAM RX] duplicate receiver start blocked")
                    print("[TELEGRAM RX] duplicate receiver start blocked")
                    return False

            if self.running and self.thread and self.thread.is_alive():
                logger.warning("[TELEGRAM RX] duplicate receiver start blocked")
                print("[TELEGRAM RX] duplicate receiver start blocked")
                return False

            # 2. 프로세스 간(Cross-Process) 단일 인스턴스 파일 락 검증
            if not self._acquire_cross_process_lock():
                logger.warning("[TELEGRAM RX] duplicate receiver start blocked (another process holds lock)")
                print("[TELEGRAM RX] duplicate receiver start blocked")
                return False

            TelegramReceiver._active_instance = self

        self.running = True
        logger.info(f"[TELEGRAM RX][instance={self.instance_id}] polling started (last_update_id={self.last_update_id})")
        print(f"[TELEGRAM RX][instance={self.instance_id}] polling started")

        self.thread = threading.Thread(target=self._poll_loop, name=f"TelegramReceiverThread-{self.instance_id}", daemon=True)
        self.thread.start()
        self._sync_live_telemetry()
        return True

    def stop(self, timeout: float = 2.0):
        """수신 스레드 안전 종료 및 가디언 락 해제"""
        self.running = False
        if self.thread and self.thread.is_alive():
            self.thread.join(timeout=timeout)
            logger.info(f"[TELEGRAM RX][instance={self.instance_id}] polling stopped")

        with TelegramReceiver._class_lock:
            if TelegramReceiver._active_instance is self:
                TelegramReceiver._active_instance = None
        self._release_cross_process_lock()
        self._sync_live_telemetry()

    def _poll_loop(self):
        """Long Polling 메인 루프 (Exponential Backoff on HTTP 409)"""
        while self.running:
            try:
                self._poll_once()
            except Exception as e:
                self.last_poll_ok = False
                self.consecutive_errors += 1
                self.last_error_message = str(e)
                logger.error(f"[TELEGRAM RX][instance={self.instance_id}] Telegram getUpdates 폴링 루프 예외: {e}")

            sleep_duration = self.backoff_seconds if self.backoff_seconds > 0 else self.poll_interval
            time.sleep(sleep_duration)

    def _poll_once(self) -> List[Dict[str, Any]]:
        """1회 getUpdates 호출 및 수신 메시지 처리 (409 감지, Telemetry & Backoff 제어)"""
        if not self.token:
            return []

        # 1. 단일 프로세스 내 동시 진입 차단 (IN_PROCESS_DUPLICATE 방지)
        if not self._poll_lock.acquire(blocking=False):
            logger.warning(
                f"[TELEGRAM RX][instance={self.instance_id}][409_CAUSE=IN_PROCESS_DUPLICATE] "
                f"Concurrent getUpdates call blocked within process (PID: {os.getpid()})"
            )
            return []

        pid = os.getpid()
        ppid = os.getppid() if hasattr(os, "getppid") else -1
        thread_id = threading.get_ident()
        token_hash = compute_token_hash(self.token)
        t_start = datetime.now()
        t_start_iso = t_start.strftime("%Y-%m-%d %H:%M:%S.%f")[:-3]

        self.telegram_getupdates_requests_total += 1
        url = f"https://api.telegram.org/bot{self.token}/getUpdates"
        params = {
            "offset": self.last_update_id + 1,
            "timeout": self.poll_timeout,
            "limit": 10
        }

        try:
            logger.debug(
                f"[TELEGRAM RX TELEMETRY] START | pid={pid}, ppid={ppid}, tid={thread_id}, "
                f"instance={self.instance_id}, token_hash={token_hash}, start={t_start_iso}"
            )
            resp = requests.get(url, params=params, timeout=self.poll_timeout + 5)
            t_end = datetime.now()
            t_end_iso = t_end.strftime("%Y-%m-%d %H:%M:%S.%f")[:-3]
            duration_ms = int((t_end - t_start).total_seconds() * 1000)
            self.last_poll_time = t_end

            # Telemetry 기록 갱신 (Requirement 3)
            self.last_telemetry = {
                "process_id": pid,
                "parent_process_id": ppid,
                "thread_id": thread_id,
                "receiver_instance_id": self.instance_id,
                "bot_token_hash": token_hash,
                "start_time": t_start_iso,
                "end_time": t_end_iso,
                "duration_ms": duration_ms,
                "status_code": resp.status_code,
                "409_cause": None
            }

            if resp.status_code == 200:
                self.last_poll_ok = True
                self.consecutive_errors = 0
                self.consecutive_409_errors = 0
                self.backoff_seconds = 0.0
                self.health_state = "HEALTHY"
                data = resp.json()
                updates = data.get("result", [])
                for update in updates:
                    self._handle_update(update)
                self._sync_live_telemetry()
                return updates

            elif resp.status_code == 409:
                self.last_poll_ok = False
                self.telegram_getupdates_409_total += 1
                self.consecutive_409_errors += 1
                self.consecutive_errors += 1

                # 409 원인 정밀 분류 (Requirement 5)
                cause = "UNKNOWN_409"
                desc = ""
                try:
                    data = resp.json()
                    desc = data.get("description", "")
                    if "webhook" in desc.lower():
                        cause = "WEBHOOK_CONFLICT"
                    elif "terminated" in desc.lower() or "other getupdates" in desc.lower() or "conflict" in desc.lower():
                        cause = "DUPLICATE_POLLING"
                except Exception:
                    pass

                self.last_telemetry["409_cause"] = cause

                # Exponential backoff: 2s -> 4s -> 8s -> 16s -> 32s -> max 60s (Requirement 7)
                self.backoff_seconds = min(60.0, 2.0 * (2 ** (self.consecutive_409_errors - 1)))
                if self.consecutive_409_errors >= 3:
                    self.health_state = "DEGRADED"
                self.last_error_message = f"HTTP 409 Conflict ({cause}): {desc}"

                logger.warning(
                    f"[TELEGRAM RX][instance={self.instance_id}][409_CAUSE={cause}] "
                    f"HTTP 409 Conflict: {desc}. "
                    f"pid={pid}, ppid={ppid}, tid={thread_id}, token_hash={token_hash}, "
                    f"duration={duration_ms}ms, backoff={self.backoff_seconds:.1f}s, state={self.health_state}"
                )
                self._sync_live_telemetry()
                return []

            else:
                self.last_poll_ok = False
                self.consecutive_errors += 1
                self.last_error_message = f"HTTP {resp.status_code}"
                logger.warning(f"[TELEGRAM RX][instance={self.instance_id}] Telegram getUpdates 오류 (HTTP {resp.status_code})")
                self._sync_live_telemetry()
                return []

        except requests.exceptions.Timeout:
            # 롱폴링 타임아웃은 정상적인 대기 완료
            t_end = datetime.now()
            t_end_iso = t_end.strftime("%Y-%m-%d %H:%M:%S.%f")[:-3]
            self.last_poll_time = t_end
            self.last_poll_ok = True
            self.consecutive_errors = 0
            self.backoff_seconds = 0.0
            self.last_telemetry = {
                "process_id": pid,
                "parent_process_id": ppid,
                "thread_id": thread_id,
                "receiver_instance_id": self.instance_id,
                "bot_token_hash": token_hash,
                "start_time": t_start_iso,
                "end_time": t_end_iso,
                "duration_ms": int((t_end - t_start).total_seconds() * 1000),
                "status_code": 200,
                "409_cause": None
            }
            return []
        except Exception as e:
            t_end = datetime.now()
            t_end_iso = t_end.strftime("%Y-%m-%d %H:%M:%S.%f")[:-3]
            self.last_poll_ok = False
            self.consecutive_errors += 1
            self.last_error_message = str(e)
            self.last_telemetry = {
                "process_id": pid,
                "parent_process_id": ppid,
                "thread_id": thread_id,
                "receiver_instance_id": self.instance_id,
                "bot_token_hash": token_hash,
                "start_time": t_start_iso,
                "end_time": t_end_iso,
                "duration_ms": int((t_end - t_start).total_seconds() * 1000),
                "status_code": -1,
                "409_cause": None,
                "error": str(e)
            }
            logger.warning(f"[TELEGRAM RX][instance={self.instance_id}] Telegram getUpdates 통신 실패: {e}")
            self._sync_live_telemetry()
            return []
        finally:
            self._poll_lock.release()

    def _handle_update(self, update: Dict[str, Any]):
        """단일 Telegram Update 처리"""
        update_id = update.get("update_id", 0)
        if update_id > self.last_update_id:
            self._save_last_update_id(update_id)

        msg = update.get("message") or update.get("edited_message")
        if not msg:
            return

        chat = msg.get("chat", {})
        chat_id = str(chat.get("id", ""))
        message_id = msg.get("message_id")
        text = msg.get("text", "").strip()
        date_ts = msg.get("date")
        received_at = datetime.fromtimestamp(date_ts).strftime("%Y-%m-%d %H:%M:%S") if date_ts else datetime.now().strftime("%Y-%m-%d %H:%M:%S")

        from_user = msg.get("from", {})
        if from_user.get("is_bot", False):
            logger.info(f"[TELEGRAM RX] 봇 발송 메시지 수신 무시: message_id={message_id}")
            return

        # 1. chat_id / user_id 인가(Authorization) 검증 (Requirement 5)
        if self.allowed_chat_id and chat_id != self.allowed_chat_id:
            logger.warning(f"🛑 [TELEGRAM RX] UNAUTHORIZED_CHAT: chat_id={chat_id}, message_id={message_id}")
            print(f"🛑 [TELEGRAM RX] UNAUTHORIZED_CHAT: chat_id={chat_id}, message_id={message_id}")
            return

        # 2. 메시지 중복 처리 방어 (Requirement 6)
        if message_id and self._is_message_processed(message_id):
            logger.info(f"[TELEGRAM RX] 중복 메시지 수신 무시: message_id={message_id}")
            return

        # 3. 실시간 터미널 수신 로그 출력 (Requirement 4 & 9)
        try:
            print("\n[TELEGRAM RX]")
            print(f"chat_id={chat_id}")
            print(f"message_id={message_id}")
            print(f"received_at={received_at}")
            print(f'text="{text}"\n')
        except UnicodeEncodeError:
            try:
                enc = sys.stdout.encoding or 'cp949'
                clean_text = text.encode(enc, errors='replace').decode(enc, errors='replace')
                print(f'text="{clean_text}"\n')
            except Exception:
                pass

        self.last_received_at = datetime.now()
        if message_id:
            self._mark_message_processed(message_id, chat_id)

        # 4. 실시간 ping / pong 점검 (Requirement 4 & 9)
        if text.lower() in ("ping", "/ping"):
            print("[TELEGRAM RX] ping received -> sending pong")
            logger.info("[TELEGRAM RX] ping received -> sending pong")
            if self.notifier:
                pong_key = f"PONG_{chat_id}_{message_id}"
                self.notifier.send_message("pong", idempotency_key=pong_key)
            return

        # 5. 메시지 분류 및 수신 라우팅 (Requirement 1, 2, 4, 5, 6)
        from core.telegram_feedback_manager import MessageClassifier, IncomingMessageType
        classification = MessageClassifier.classify(text)
        logger.info(f"[TELEGRAM RX] 메시지 분류: {classification.value} (text='{text[:40]}')")

        if classification in (IncomingMessageType.TRADE_EVENT, IncomingMessageType.SYSTEM_EVENT):
            # 거래 이벤트 / 시스템 알림 -> 피드백 파이프라인에서 원천 배제 (No feedback_id, No Confirmation)
            logger.info(f"[TELEGRAM RX] 거래/시스템 이벤트 감지 -> 피드백 저장 및 응답 원천 차단: {text[:40]}")
            return

        elif classification == IncomingMessageType.QUERY:
            # 자연어 질문 -> 피드백으로 저장하지 않고 질의 응답만 회신 (No feedback_id)
            logger.info(f"[TELEGRAM RX] 자연어 질문(QUERY) 감지 -> Query Responder 호출: {text[:40]}")
            if self.notifier and hasattr(self.notifier, "handle_query"):
                self.notifier.handle_query(text, chat_id=chat_id, message_id=message_id)
            return

        elif classification == IncomingMessageType.COMMAND:
            # 슬래시 명령어 처리
            logger.info(f"[TELEGRAM RX] 명령어(COMMAND) 감지: {text[:40]}")
            if self.notifier and hasattr(self.notifier, "handle_command"):
                self.notifier.handle_command(text, chat_id=chat_id, message_id=message_id)
            return

        elif classification == IncomingMessageType.USER_FEEDBACK:
            # 사용자 자연어 피드백인 경우에만 Feedback Store 저장 및 Confirmation 회신
            if self.notifier:
                logger.info(f"[TELEGRAM RX] 사용자 피드백 파이프라인 전달: {text[:40]}")
                self.notifier.receive_and_reply_feedback(text, chat_id=chat_id, now=self.last_received_at)
            return

        else:
            # UNKNOWN / 불확실한 텍스트 -> 임의로 피드백으로 저장하지 않음 (Requirement 6)
            logger.info(f"[TELEGRAM RX] 불확실한 텍스트 수신 (피드백 저장 제외): {text[:40]}")
            return

    def get_webhook_info(self) -> Dict[str, Any]:
        """Telegram Bot API getWebhookInfo 조회 (Requirement 3)"""
        if not self.token:
            return {
                "status": "ERROR",
                "webhook_url": "",
                "pending_update_count": 0,
                "last_error_date": None,
                "last_error_message": "Token missing"
            }
        try:
            r = requests.get(f"https://api.telegram.org/bot{self.token}/getWebhookInfo", timeout=5)
            if r.status_code == 200:
                res = r.json().get("result", {})
                url = res.get("url", "")
                return {
                    "status": "BLOCKED_BY_WEBHOOK" if url else "NOT_SET",
                    "webhook_url": url,
                    "pending_update_count": res.get("pending_update_count", 0),
                    "last_error_date": res.get("last_error_date"),
                    "last_error_message": res.get("last_error_message")
                }
            return {
                "status": "ERROR",
                "webhook_url": "",
                "pending_update_count": 0,
                "last_error_date": None,
                "last_error_message": f"HTTP {r.status_code}"
            }
        except Exception as e:
            return {
                "status": "ERROR",
                "webhook_url": "",
                "pending_update_count": 0,
                "last_error_date": None,
                "last_error_message": str(e)
            }

    def check_api_connectivity(self) -> Dict[str, Any]:
        """Telegram API 통신 점검 (getMe & getUpdates)"""
        result = {
            "telegram_api": "FAIL",
            "bot_identity": "FAIL",
            "polling": "FAIL",
            "update_reception": "FAIL",
            "details": {}
        }
        if not self.token:
            result["details"]["error"] = "Bot token is missing"
            return result

        masked = mask_token(self.token)

        # 1. getMe 점검
        try:
            r_me = requests.get(f"https://api.telegram.org/bot{self.token}/getMe", timeout=5)
            if r_me.status_code == 200:
                me_data = r_me.json()
                if me_data.get("ok"):
                    res = me_data.get("result", {})
                    result["telegram_api"] = "OK"
                    result["bot_identity"] = f"OK (@{res.get('username')}, id={res.get('id')})"
                    result["details"]["bot_id"] = res.get("id")
                    result["details"]["bot_username"] = res.get("username")
                    result["details"]["bot_first_name"] = res.get("first_name")
            else:
                result["details"]["getMe_status"] = r_me.status_code
        except Exception as e:
            result["details"]["getMe_error"] = str(e)

        # 2. getUpdates 점검 (수신 스레드 가동 중일 때는 충돌 방지를 위해 스레드 상태 스냅샷 활용)
        if self.running:
            result["polling"] = "OK (receiver thread active)"
            result["update_reception"] = "OK" if self.last_poll_ok else "DEGRADED"
            result["details"]["last_update_id"] = self.last_update_id
            result["details"]["consecutive_409"] = self.consecutive_409_errors
        else:
            try:
                r_up = requests.get(
                    f"https://api.telegram.org/bot{self.token}/getUpdates",
                    params={"limit": 5, "offset": self.last_update_id + 1, "timeout": 2},
                    timeout=5
                )
                if r_up.status_code == 200:
                    up_data = r_up.json()
                    if up_data.get("ok"):
                        result["polling"] = "OK"
                        updates = up_data.get("result", [])
                        result["update_reception"] = f"OK ({len(updates)} pending)" if updates else "OK (0 pending)"
                        result["details"]["pending_count"] = len(updates)
                else:
                    result["details"]["getUpdates_status"] = r_up.status_code
            except Exception as e:
                result["details"]["getUpdates_error"] = str(e)

        result["details"]["token_masked"] = masked
        result["details"]["token_hash"] = compute_token_hash(self.token)
        result["details"]["last_update_id"] = self.last_update_id
        return result

    def is_healthy(self) -> bool:
        """수신 루프 건강도 판별 (consecutive_409_errors 포함)"""
        if not self.running:
            return False
        if self.thread and not self.thread.is_alive():
            return False
        if self.consecutive_errors >= 5 or self.consecutive_409_errors >= 3:
            return False
        return self.last_poll_ok

    def get_health(self) -> Dict[str, Any]:
        """발신 및 수신 건강도 분리 스냅샷 (Requirement 7 & 11)"""
        healthy = self.is_healthy()
        if not healthy:
            rx_status = "DEGRADED" if self.health_state == "DEGRADED" else "FAIL"
        elif self.last_received_at is not None:
            rx_status = "OK"
        else:
            rx_status = "NO_MESSAGE"

        send_status = "OK"
        last_sent_at = None
        if self.notifier:
            send_status = getattr(self.notifier, "send_status", "OK")
            last_sent_at = getattr(self.notifier, "last_sent_at", None)

        return {
            "send_status": send_status,
            "receive_status": rx_status,
            "receiver_instance_count": 1 if self.running else 0,
            "instance_id": self.instance_id,
            "lock_held": self._lock_file is not None,
            "owner_pid": os.getpid() if self._lock_file else None,
            "lock_file": self.lock_file_path,
            "telemetry": getattr(self, "last_telemetry", {}),
            "getupdates_requests": self.telegram_getupdates_requests_total,
            "getupdates_409": self.telegram_getupdates_409_total,
            "consecutive_409_errors": self.consecutive_409_errors,
            "backoff_seconds": self.backoff_seconds,
            "health_state": self.health_state,
            "last_sent_at": last_sent_at,
            "last_received_at": self.last_received_at.strftime("%Y-%m-%d %H:%M:%S") if self.last_received_at else None,
            "last_update_id": self.last_update_id
        }

    def _sync_live_telemetry(self):
        """data/live_telemetry.json에 telegram_health 섹션 실시간 영속화 (Requirement 11)"""
        telemetry_path = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "data", "live_telemetry.json")
        try:
            data = {}
            if os.path.exists(telemetry_path):
                try:
                    with open(telemetry_path, "r", encoding="utf-8") as f:
                        data = json.load(f)
                except Exception:
                    data = {}
            data["telegram_health"] = self.get_health()
            os.makedirs(os.path.dirname(telemetry_path), exist_ok=True)
            with open(telemetry_path, "w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False, indent=2)
        except Exception:
            pass
