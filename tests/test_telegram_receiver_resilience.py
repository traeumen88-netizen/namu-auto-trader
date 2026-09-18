"""
tests/test_telegram_receiver_resilience.py
Telegram getUpdates 409 회복성, 단일 RX 가디언, 지수 백오프 및 수신 파이프라인 검증 9대 필수 테스트
"""

import os
import time
import shutil
import tempfile
import unittest
from unittest.mock import patch, MagicMock
from datetime import datetime

from core.telegram_receiver import TelegramReceiver, compute_token_hash, mask_token
from core.telegram_notifier import TelegramNotifier


class TestTelegramReceiverResilience(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.mkdtemp()
        self.db_path = os.path.join(self.temp_dir, "test_operational.db")
        self.chat_id = "6824295325"
        self.token = "8704110151:AAHibUhbqXraZMEeeA8Mj9Oe_EKTtKDNOF8"
        self.notifier = TelegramNotifier(token=self.token, chat_id=self.chat_id, db_path=self.db_path)
        # 테스트 전 활성 인스턴스 초기화
        TelegramReceiver._active_instance = None

    def tearDown(self):
        if TelegramReceiver._active_instance:
            try:
                TelegramReceiver._active_instance.stop(timeout=0.5)
            except Exception:
                pass
            TelegramReceiver._active_instance = None
        shutil.rmtree(self.temp_dir, ignore_errors=True)

    # 1. test_single_receiver_instance
    def test_single_receiver_instance(self):
        """단일 receiver가 정상적으로 instance_id를 부여받고 실행되는지 검증"""
        rx = TelegramReceiver(notifier=self.notifier, token=self.token, allowed_chat_id=self.chat_id, db_path=self.db_path)
        self.assertTrue(rx.instance_id.startswith("RX-"))
        started = rx.start()
        try:
            self.assertTrue(started)
            self.assertTrue(rx.running)
            self.assertEqual(TelegramReceiver._active_instance, rx)
        finally:
            rx.stop(timeout=0.5)
            self.assertFalse(rx.running)
            self.assertIsNone(TelegramReceiver._active_instance)

    # 2. test_duplicate_receiver_start_blocked
    def test_duplicate_receiver_start_blocked(self):
        """동일 프로세스 내에서 2번째 receiver start 시도시 차단(FAIL/BLOCKED)되는지 검증"""
        rx1 = TelegramReceiver(notifier=self.notifier, token=self.token, allowed_chat_id=self.chat_id, db_path=self.db_path)
        rx2 = TelegramReceiver(notifier=self.notifier, token=self.token, allowed_chat_id=self.chat_id, db_path=self.db_path)

        started1 = rx1.start()
        self.assertTrue(started1)
        try:
            # 2번째 start는 차단되어야 함
            started2 = rx2.start()
            self.assertFalse(started2)
            self.assertFalse(rx2.running)
        finally:
            rx1.stop(timeout=0.5)

    # 3. test_webhook_conflict_detection
    @patch("requests.get")
    def test_webhook_conflict_detection(self, mock_get):
        """getWebhookInfo 응답에 webhook_url이 등록되어 있을 때 BLOCKED_BY_WEBHOOK 감지 검증"""
        rx = TelegramReceiver(notifier=self.notifier, token=self.token, allowed_chat_id=self.chat_id, db_path=self.db_path)

        # Webhook이 설정되어 있는 경우
        mock_resp_set = MagicMock()
        mock_resp_set.status_code = 200
        mock_resp_set.json.return_value = {
            "ok": True,
            "result": {
                "url": "https://example.com/webhook",
                "has_custom_certificate": False,
                "pending_update_count": 5,
                "last_error_date": None,
                "last_error_message": None
            }
        }
        mock_get.return_value = mock_resp_set

        info = rx.get_webhook_info()
        self.assertEqual(info["status"], "BLOCKED_BY_WEBHOOK")
        self.assertEqual(info["webhook_url"], "https://example.com/webhook")
        self.assertEqual(info["pending_update_count"], 5)

        # Webhook이 해제되어 있는 경우
        mock_resp_unset = MagicMock()
        mock_resp_unset.status_code = 200
        mock_resp_unset.json.return_value = {
            "ok": True,
            "result": {
                "url": "",
                "has_custom_certificate": False,
                "pending_update_count": 0
            }
        }
        mock_get.return_value = mock_resp_unset

        info2 = rx.get_webhook_info()
        self.assertEqual(info2["status"], "NOT_SET")
        self.assertEqual(info2["webhook_url"], "")

    # 4. test_getupdates_409_backoff
    @patch("requests.get")
    def test_getupdates_409_backoff(self, mock_get):
        """HTTP 409 수신 시 지수 백오프(2s -> 4s -> 8s ...) 및 DEGRADED 상태 전이 검증"""
        mock_resp_409 = MagicMock()
        mock_resp_409.status_code = 409
        mock_resp_409.json.return_value = {"ok": False, "error_code": 409, "description": "Conflict: terminated by other getUpdates"}
        mock_get.return_value = mock_resp_409

        rx = TelegramReceiver(notifier=self.notifier, token=self.token, allowed_chat_id=self.chat_id, db_path=self.db_path)
        rx.running = True

        # 1차 409: 2s 백오프
        rx._poll_once()
        self.assertEqual(rx.telegram_getupdates_409_total, 1)
        self.assertEqual(rx.consecutive_409_errors, 1)
        self.assertEqual(rx.backoff_seconds, 2.0)
        self.assertEqual(rx.health_state, "HEALTHY")

        # 2차 409: 4s 백오프
        rx._poll_once()
        self.assertEqual(rx.telegram_getupdates_409_total, 2)
        self.assertEqual(rx.consecutive_409_errors, 2)
        self.assertEqual(rx.backoff_seconds, 4.0)

        # 3차 409: 8s 백오프 및 DEGRADED 전이
        rx._poll_once()
        self.assertEqual(rx.telegram_getupdates_409_total, 3)
        self.assertEqual(rx.consecutive_409_errors, 3)
        self.assertEqual(rx.backoff_seconds, 8.0)
        self.assertEqual(rx.health_state, "DEGRADED")
        self.assertFalse(rx.is_healthy())

        # 200 OK 복구 시 리셋 및 HEALTHY 복귀
        mock_resp_200 = MagicMock()
        mock_resp_200.status_code = 200
        mock_resp_200.json.return_value = {"ok": True, "result": []}
        mock_get.return_value = mock_resp_200

        rx._poll_once()
        self.assertEqual(rx.consecutive_409_errors, 0)
        self.assertEqual(rx.backoff_seconds, 0.0)
        self.assertEqual(rx.health_state, "HEALTHY")
        self.assertTrue(rx.is_healthy())

    # 5. test_getupdates_409_no_hot_loop
    @patch("requests.get")
    def test_getupdates_409_no_hot_loop(self, mock_get):
        """HTTP 409 발생 시 tight loop(0.5초)를 돌지 않고 backoff_seconds 만큼 대기 주기가 설정되는지 검증"""
        mock_resp_409 = MagicMock()
        mock_resp_409.status_code = 409
        mock_get.return_value = mock_resp_409

        rx = TelegramReceiver(notifier=self.notifier, token=self.token, allowed_chat_id=self.chat_id, db_path=self.db_path)
        rx._poll_once()

        # 409 발생 후에는 0.5초가 아닌 최소 2.0초 대기 주기가 활성화됨
        effective_interval = rx.backoff_seconds if rx.backoff_seconds > 0 else rx.poll_interval
        self.assertGreaterEqual(effective_interval, 2.0)

    # 6. test_update_offset_progression
    @patch("requests.get")
    def test_update_offset_progression(self, mock_get):
        """update_id N 수신 후 다음 getUpdates에서 offset=N+1로 호출되는지 검증"""
        rx = TelegramReceiver(notifier=self.notifier, token=self.token, allowed_chat_id=self.chat_id, db_path=self.db_path)
        self.assertEqual(rx.last_update_id, 0)

        # Update 100 수신
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = {
            "ok": True,
            "result": [
                {"update_id": 100, "message": {"message_id": 1, "chat": {"id": 6824295325}, "text": "안녕"}}
            ]
        }
        mock_get.return_value = mock_resp

        rx._poll_once()
        self.assertEqual(rx.last_update_id, 100)

        # 다음 호출 시 offset=101 확인
        mock_resp.json.return_value = {"ok": True, "result": []}
        rx._poll_once()
        called_params = mock_get.call_args[1]["params"]
        self.assertEqual(called_params["offset"], 101)

    # 7. test_duplicate_update_prevention
    def test_duplicate_update_prevention(self):
        """동일 message_id가 2회 유입될 때 2회차 처리가 중복 방어되는지 검증"""
        rx = TelegramReceiver(notifier=self.notifier, token=self.token, allowed_chat_id=self.chat_id, db_path=self.db_path)
        update_data = {
            "update_id": 200,
            "message": {"message_id": 8888, "chat": {"id": 6824295325}, "text": "테스트"}
        }

        # 1회차 처리
        rx._handle_update(update_data)
        self.assertTrue(rx._is_message_processed(8888))

        # 2회차 처리 시도 -> 중복 감지되어 스킵
        with patch.object(rx, "_mark_message_processed") as mock_mark:
            rx._handle_update(update_data)
            mock_mark.assert_not_called()

    # 8. test_ping_pong
    @patch("requests.post")
    def test_ping_pong(self, mock_post):
        """ping 메시지 수신 시 pong 응답 발송 검증"""
        mock_post_resp = MagicMock()
        mock_post_resp.status_code = 200
        mock_post_resp.json.return_value = {"ok": True}
        mock_post.return_value = mock_post_resp

        rx = TelegramReceiver(notifier=self.notifier, token=self.token, allowed_chat_id=self.chat_id, db_path=self.db_path)
        ping_update = {
            "update_id": 300,
            "message": {"message_id": 9999, "chat": {"id": 6824295325}, "text": "ping"}
        }

        rx._handle_update(ping_update)
        # pong 메시지 전송 확인
        self.assertTrue(mock_post.called)
        sent_payload = mock_post.call_args[1]["json"]
        self.assertEqual(sent_payload["text"], "pong")

    # 9. test_feedback_receive
    @patch("requests.post")
    def test_feedback_receive(self, mock_post):
        """'459510 오늘 진입이 늦었던 것 같음' 피드백 수신 -> 파싱 -> DB 저장 -> 확인 메시지 전송 파이프라인 검증"""
        mock_post_resp = MagicMock()
        mock_post_resp.status_code = 200
        mock_post_resp.json.return_value = {"ok": True}
        mock_post.return_value = mock_post_resp

        rx = TelegramReceiver(notifier=self.notifier, token=self.token, allowed_chat_id=self.chat_id, db_path=self.db_path)
        fb_update = {
            "update_id": 400,
            "message": {
                "message_id": 8801,
                "chat": {"id": 6824295325},
                "text": "459510 오늘 진입이 늦었던 것 같음"
            }
        }

        rx._handle_update(fb_update)

        # 피드백이 DB에 저장되었는지 확인
        fbs = self.notifier.feedback_manager.store.get_feedbacks_by_date(datetime.now().strftime("%Y-%m-%d"))
        self.assertEqual(len(fbs), 1)
        self.assertEqual(fbs[0].symbol, "459510")
        self.assertEqual(fbs[0].category, "ENTRY_TIMING")

        # 확인 메시지 발송 확인
        self.assertTrue(mock_post.called)
        sent_text = mock_post.call_args[1]["json"]["text"]
        self.assertIn("STORED", sent_text)
        self.assertIn("ENTRY_TIMING", sent_text)

    # 10. test_cross_process_lock_owner_json_and_blocking
    def test_cross_process_lock_owner_json_and_blocking(self):
        """OS 파일 락 획득 시 소유자 메타데이터(PID 등) 기록 및 제2 인스턴스 즉시 거부 검증 (Requirement 4)"""
        import json
        rx1 = TelegramReceiver(notifier=self.notifier, token=self.token, allowed_chat_id=self.chat_id, db_path=self.db_path)
        rx2 = TelegramReceiver(notifier=self.notifier, token=self.token, allowed_chat_id=self.chat_id, db_path=self.db_path)

        locked1 = rx1._acquire_cross_process_lock()
        self.assertTrue(locked1)
        self.assertTrue(os.path.exists(rx1.lock_file_path))
        self.assertTrue(os.path.exists(rx1.owner_file_path))

        with open(rx1.owner_file_path, "r", encoding="utf-8") as f:
            owner_data = json.load(f)
        self.assertEqual(owner_data["pid"], os.getpid())
        self.assertEqual(owner_data["instance_id"], rx1.instance_id)
        self.assertEqual(owner_data["token_hash"], compute_token_hash(self.token))

        # 제2 인스턴스 락 획득 시도 -> 실패해야 함
        locked2 = rx2._acquire_cross_process_lock()
        self.assertFalse(locked2)

        # 해제 검증
        rx1._release_cross_process_lock()
        self.assertFalse(os.path.exists(rx1.owner_file_path))

        # 해제 후 제2 인스턴스 획득 성공 검증
        locked2_after = rx2._acquire_cross_process_lock()
        self.assertTrue(locked2_after)
        rx2._release_cross_process_lock()

    # 11. test_getupdates_telemetry_fields
    @patch("requests.get")
    def test_getupdates_telemetry_fields(self, mock_get):
        """getUpdates 호출 시 7대 텔레메트리 필드 전수 기록 검증 (Requirement 3)"""
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = {"ok": True, "result": []}
        mock_get.return_value = mock_resp

        rx = TelegramReceiver(notifier=self.notifier, token=self.token, allowed_chat_id=self.chat_id, db_path=self.db_path)
        rx._poll_once()

        telem = rx.last_telemetry
        self.assertEqual(telem["process_id"], os.getpid())
        self.assertIn("parent_process_id", telem)
        self.assertIn("thread_id", telem)
        self.assertEqual(telem["receiver_instance_id"], rx.instance_id)
        self.assertEqual(telem["bot_token_hash"], compute_token_hash(self.token))
        self.assertIn("start_time", telem)
        self.assertIn("end_time", telem)
        self.assertIn("duration_ms", telem)
        self.assertEqual(telem["status_code"], 200)

    # 12. test_409_cause_duplicate_polling
    @patch("requests.get")
    def test_409_cause_duplicate_polling(self, mock_get):
        """HTTP 409 'terminated by other getUpdates' 발생 시 DUPLICATE_POLLING 분류 검증 (Requirement 5)"""
        mock_resp_409 = MagicMock()
        mock_resp_409.status_code = 409
        mock_resp_409.json.return_value = {
            "ok": False,
            "error_code": 409,
            "description": "Conflict: terminated by other getUpdates request; make sure that only one bot instance is running"
        }
        mock_get.return_value = mock_resp_409

        rx = TelegramReceiver(notifier=self.notifier, token=self.token, allowed_chat_id=self.chat_id, db_path=self.db_path)
        rx._poll_once()

        self.assertEqual(rx.last_telemetry["409_cause"], "DUPLICATE_POLLING")
        self.assertIn("DUPLICATE_POLLING", rx.last_error_message)

    # 13. test_409_cause_webhook_conflict
    @patch("requests.get")
    def test_409_cause_webhook_conflict(self, mock_get):
        """HTTP 409 'webhook is active' 발생 시 WEBHOOK_CONFLICT 분류 검증 (Requirement 5)"""
        mock_resp_409 = MagicMock()
        mock_resp_409.status_code = 409
        mock_resp_409.json.return_value = {
            "ok": False,
            "error_code": 409,
            "description": "Conflict: can't use getUpdates method while webhook is active"
        }
        mock_get.return_value = mock_resp_409

        rx = TelegramReceiver(notifier=self.notifier, token=self.token, allowed_chat_id=self.chat_id, db_path=self.db_path)
        rx._poll_once()

        self.assertEqual(rx.last_telemetry["409_cause"], "WEBHOOK_CONFLICT")

    # 14. test_in_process_duplicate_polling_blocked
    @patch("requests.get")
    def test_in_process_duplicate_polling_blocked(self, mock_get):
        """단일 프로세스 내에서 2개 스레드가 동시 getUpdates 진입 시 즉시 차단(IN_PROCESS_DUPLICATE) 검증 (Requirement 5)"""
        rx = TelegramReceiver(notifier=self.notifier, token=self.token, allowed_chat_id=self.chat_id, db_path=self.db_path)

        # 락을 수동으로 잡은 상태에서 _poll_once 호출
        rx._poll_lock.acquire()
        try:
            res = rx._poll_once()
            self.assertEqual(res, [])
            mock_get.assert_not_called()
        finally:
            rx._poll_lock.release()

    # 15. test_supervisor_singleton_and_pid_wait
    def test_supervisor_singleton_and_pid_wait(self):
        """Supervisor 단일 가동 락 및 프로세스 생존 판별 로직 검증 (Requirement 6)"""
        from execution.auto_restart_supervisor import ProcessSupervisor
        sp1 = ProcessSupervisor(mode="live")
        sp1.lock_file_path = os.path.join(self.temp_dir, "supervisor_live.lock")

        sp2 = ProcessSupervisor(mode="live")
        sp2.lock_file_path = sp1.lock_file_path

        self.assertTrue(sp1._acquire_supervisor_lock())
        # 동일 모드 두 번째 Supervisor는 락 획득 실패
        self.assertFalse(sp2._acquire_supervisor_lock())

        sp1._release_supervisor_lock()
        self.assertTrue(sp2._acquire_supervisor_lock())
        sp2._release_supervisor_lock()

        # PID 생존 판별 테스트 (현재 프로세스는 살아있고 가상 PID 99999999는 죽어있음)
        self.assertTrue(ProcessSupervisor._is_pid_alive(os.getpid()))
        self.assertFalse(ProcessSupervisor._is_pid_alive(99999999))


if __name__ == "__main__":
    unittest.main()
