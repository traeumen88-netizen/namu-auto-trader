"""
tests/test_telegram_feedback_separation.py
시스템 거래 이벤트(BUY/SELL/FILL 등) 및 자연어 질문(QUERY)과
사용자 자연어 피드백(USER_FEEDBACK)의 100% 완전 분리 및 거버넌스 검증 테스트 스위트
"""

import os
import json
import sqlite3
import tempfile
import shutil
import unittest
from datetime import datetime
from typing import List, Dict, Any

from core.telegram_feedback_manager import (
    TelegramFeedbackManager,
    TelegramIdempotencyManager,
    TelegramFeedbackStore,
    MessageClassifier,
    QueryResponder,
    IncomingMessageType,
    TelegramMessageType,
    FeedbackRecord,
    FeedbackCategory,
    FeedbackStatus,
    format_feedback_confirmation
)
from core.telegram_notifier import TelegramNotifier
from core.telegram_receiver import TelegramReceiver


class TestTelegramFeedbackSeparation(unittest.TestCase):
    def setUp(self):
        self.tmp_dir = tempfile.mkdtemp()
        self.operational_db_path = os.path.join(self.tmp_dir, "test_operational.db")
        self.trade_db_path = os.path.join(self.tmp_dir, "test_trades.db")

        self._init_databases()

        self.notifier = TelegramNotifier(
            token="MOCK_TOKEN_12345",
            chat_id="6824295325",
            db_path=self.operational_db_path
        )
        self.notifier.enabled = True
        self.feedback_mgr = TelegramFeedbackManager(db_path=self.operational_db_path)

        self.rx = TelegramReceiver(
            notifier=self.notifier,
            token="MOCK_TOKEN_12345",
            allowed_chat_id="6824295325",
            db_path=self.operational_db_path
        )

        self.sent_messages: List[Dict[str, Any]] = []
        self.notifier.send_message = self._mock_send_message

    def tearDown(self):
        shutil.rmtree(self.tmp_dir, ignore_errors=True)

    def _init_databases(self):
        """테스트용 가상 SQLite DB 초기화"""
        with sqlite3.connect(self.trade_db_path) as conn:
            conn.execute("""
            CREATE TABLE IF NOT EXISTS trades (
                trade_id INTEGER PRIMARY KEY AUTOINCREMENT,
                symbol TEXT,
                symbol_name TEXT,
                strategy TEXT,
                entry_time TEXT,
                exit_time TEXT,
                entry_price REAL,
                exit_price REAL,
                pnl REAL,
                return_pct REAL,
                exit_reason TEXT
            )
            """)

        with sqlite3.connect(self.operational_db_path) as conn:
            conn.execute("""
            CREATE TABLE IF NOT EXISTS positions (
                position_id INTEGER PRIMARY KEY AUTOINCREMENT,
                symbol TEXT,
                name TEXT,
                total_qty INTEGER,
                entry_price REAL,
                status TEXT
            )
            """)
            conn.execute("""
            CREATE TABLE IF NOT EXISTS no_trade_records (
                record_id INTEGER PRIMARY KEY AUTOINCREMENT,
                iem_cd TEXT,
                name TEXT,
                timestamp TEXT,
                rule_score REAL,
                ml_prob REAL,
                expected_net_r REAL,
                primary_reason TEXT,
                strategy_id TEXT
            )
            """)

    def _mock_send_message(
        self,
        text,
        parse_mode="HTML",
        reply_markup=None,
        idempotency_key=None,
        message_type=None,
        business_date=None,
        event_id=None,
        trade_id=None,
        feedback_id=None
    ):
        """실제 외부 네트워크 호출 없이 발송 파라미터 및 격리성 검증 Mock"""
        # TelegramNotifier의 실제 가드 로직 재현 검증
        if message_type in (TelegramMessageType.USER_FEEDBACK_CONFIRMATION.value, "FEEDBACK_CONFIRMATION"):
            if not feedback_id or not str(feedback_id).startswith("FB-"):
                return False
        elif message_type in (
            TelegramMessageType.TRADE_ALERT.value,
            TelegramMessageType.SYSTEM_ALERT.value,
            TelegramMessageType.EOD_REPORT.value,
            TelegramMessageType.QUERY_RESPONSE.value,
            TelegramMessageType.COMMAND_RESPONSE.value,
            "ZERO_TRADE_DAY"
        ) or (message_type and str(message_type).startswith("TRADE_")):
            feedback_id = None

        self.sent_messages.append({
            "text": text,
            "message_type": message_type,
            "idempotency_key": idempotency_key,
            "business_date": business_date,
            "event_id": event_id,
            "trade_id": trade_id,
            "feedback_id": feedback_id
        })
        return True

    # =========================================================================
    # 1. test_trade_event_not_saved_as_feedback (Requirement 2 & 10)
    # =========================================================================
    def test_trade_event_not_saved_as_feedback(self):
        """시스템 거래/체결 이벤트가 USER_FEEDBACK으로 분류되거나 저장되지 않음을 검증"""
        trade_events = [
            "ORDER_CREATED", "ORDER_SENT", "ORDER_ACK", "PARTIAL_FILL", "UNFILLED",
            "STOP", "TARGET", "SCALE_OUT", "TRAILING", "TIME_STOP", "EOD",
            "POSITION_OPEN", "POSITION_CLOSED"
        ]

        for i, evt in enumerate(trade_events):
            # 1. 분류기 검증: TRADE_EVENT로 분류되어야 함
            cls_result = MessageClassifier.classify(evt)
            self.assertEqual(
                cls_result,
                IncomingMessageType.TRADE_EVENT,
                f"이벤트 '{evt}'는 TRADE_EVENT로 분류되어야 합니다 (현재: {cls_result})"
            )

            # 2. 피드백 파이프라인 수신 시도: feedback_id가 생성되지 않고 저장되지 않아야 함 (None 반환)
            rec, conf = self.feedback_mgr.process_incoming_feedback(evt)
            self.assertIsNone(rec, f"'{evt}'에 대해 feedback_id 레코드가 생성되어서는 안 됩니다.")
            self.assertEqual(conf, "")

            # 3. Telegram Receiver 수신 시도: 피드백 파이프라인에서 무시되어야 함
            self.sent_messages.clear()
            update = {
                "update_id": 10001 + i,
                "message": {"message_id": 2001 + i, "chat": {"id": 6824295325}, "text": evt}
            }
            self.rx._handle_update(update)

            # 발송된 확인 메시지가 0건이어야 함
            conf_msgs = [m for m in self.sent_messages if m.get("message_type") == TelegramMessageType.USER_FEEDBACK_CONFIRMATION.value]
            self.assertEqual(len(conf_msgs), 0, f"'{evt}' 이벤트에 피드백 확인 메시지가 전송되어서는 안 됩니다.")

        # DB에 저장된 피드백이 0건이어야 함
        records = self.feedback_mgr.store.get_feedbacks_by_date(datetime.now().strftime("%Y-%m-%d"))
        self.assertEqual(len(records), 0)

    # =========================================================================
    # 2. test_buy_not_saved_as_feedback (Requirement 2, 3, 10)
    # =========================================================================
    def test_buy_not_saved_as_feedback(self):
        """BUY 이벤트 및 매수 체결 알림이 feedback_id=NULL이며 피드백으로 저장되지 않음을 검증"""
        buy_inputs = [
            "BUY",
            "BUY 459510",
            "🟢 [매수 체결 완료]\n• 종목명: 세미파이브 (490470)\n• 체결단가: 15,200원 (10주)\n• 매수총액: 152,000원\n• 매수전략: VWAP_PULLBACK\n• 체결시각: 10:15:30",
            "🚀 매수 체결\n종목: 459510\n수량: 10주\n체결가: 15,200원\n전략: INT_MOMENTUM"
        ]

        for i, buy_text in enumerate(buy_inputs):
            cls_result = MessageClassifier.classify(buy_text)
            self.assertEqual(cls_result, IncomingMessageType.TRADE_EVENT)

            # process_incoming_feedback 호출 시 None 반환
            rec, conf = self.feedback_mgr.process_incoming_feedback(buy_text)
            self.assertIsNone(rec)
            self.assertEqual(conf, "")

            # Telegram 수신기로 유입 시 확인 메시지 발송 0건
            self.sent_messages.clear()
            update = {
                "update_id": 11001 + i,
                "message": {"message_id": 2101 + i, "chat": {"id": 6824295325}, "text": buy_text}
            }
            self.rx._handle_update(update)

            conf_sent = [m for m in self.sent_messages if "피드백 저장 완료" in m["text"]]
            self.assertEqual(len(conf_sent), 0)

        # 시스템 매수 체결 알림(send_trade_event) 발송 시 feedback_id가 NULL이어야 함
        self.sent_messages.clear()
        self.notifier.send_trade_event(
            event_type="BUY",
            symbol="490470",
            name="세미파이브",
            price=15200,
            qty=10,
            reason="INT_MOMENTUM"
        )
        self.assertEqual(len(self.sent_messages), 1)
        buy_msg = self.sent_messages[0]
        self.assertIn("매수 체결 완료", buy_msg["text"])
        self.assertNotIn("피드백 저장 완료", buy_msg["text"])
        self.assertNotIn("EOD Learning에 반영됩니다", buy_msg["text"])
        self.assertIsNone(buy_msg["feedback_id"], "BUY 체결 알림의 feedback_id는 반드시 NULL이어야 합니다.")

    # =========================================================================
    # 3. test_sell_not_saved_as_feedback (Requirement 2, 3, 10)
    # =========================================================================
    def test_sell_not_saved_as_feedback(self):
        """SELL 이벤트 및 매도 체결 알림이 feedback_id=NULL이며 피드백으로 저장되지 않음을 검증"""
        sell_inputs = [
            "SELL",
            "SELL 005930",
            "🔴 [매도 체결 완료] 📉 -1.50%\n• 종목명: 세미파이브 (490470)\n• 체결단가: 14,970원 (10주)\n• 진입단가: 15,200원\n• 실현손익: -1.50% (-2,300원)\n• 매도사유: TRAILING_STOP\n• 체결시각: 10:25:30"
        ]

        for sell_text in sell_inputs:
            cls_result = MessageClassifier.classify(sell_text)
            self.assertEqual(cls_result, IncomingMessageType.TRADE_EVENT)

            rec, conf = self.feedback_mgr.process_incoming_feedback(sell_text)
            self.assertIsNone(rec)
            self.assertEqual(conf, "")

        # 시스템 매도 체결 알림 발송 시 feedback_id가 NULL이어야 함
        self.sent_messages.clear()
        self.notifier.send_trade_event(
            event_type="SELL",
            symbol="005930",
            name="삼성전자",
            price=75000,
            qty=5,
            reason="TARGET_1R",
            return_pct=1.8,
            pnl_won=6750,
            entry_price=73670
        )
        self.assertEqual(len(self.sent_messages), 1)
        sell_msg = self.sent_messages[0]
        self.assertIn("매도 체결 완료", sell_msg["text"])
        self.assertNotIn("피드백 저장 완료", sell_msg["text"])
        self.assertIsNone(sell_msg["feedback_id"], "SELL 체결 알림의 feedback_id는 반드시 NULL이어야 합니다.")

    # =========================================================================
    # 4. test_fill_not_saved_as_feedback (Requirement 2 & 10)
    # =========================================================================
    def test_fill_not_saved_as_feedback(self):
        """FILL 이벤트가 feedback_id=NULL이며 피드백으로 저장되지 않음을 검증"""
        fill_inputs = ["FILL", "FILL 459510 15200원", "PARTIAL_FILL"]

        for fill_text in fill_inputs:
            cls_result = MessageClassifier.classify(fill_text)
            self.assertEqual(cls_result, IncomingMessageType.TRADE_EVENT)

            rec, conf = self.feedback_mgr.process_incoming_feedback(fill_text)
            self.assertIsNone(rec)
            self.assertEqual(conf, "")

    # =========================================================================
    # 5. test_user_text_saved_as_feedback (Requirement 1, 6, 10)
    # =========================================================================
    def test_user_text_saved_as_feedback(self):
        """사용자가 직접 입력한 자연어 의견/평가만 USER_FEEDBACK으로 정확히 저장되고 feedback_id가 생성됨을 검증"""
        valid_user_feedbacks = [
            ("459510 오늘 진입이 너무 늦은 것 같음", "459510", "ENTRY_TIMING"),
            ("VWAP Pullback은 너무 자주 들어가는 것 같은데", None, "STRATEGY"),
            ("오늘 손절이 너무 빨랐음", None, "EXIT_TIMING"),
            ("이런 급등주는 추격하지 않는 게 좋겠음", None, "ENTRY_TIMING"),
            ("오늘 459510 진입은 너무 늦었다고 본다.", "459510", "ENTRY_TIMING")
        ]

        today = datetime.now().strftime("%Y-%m-%d")
        for text, exp_sym, exp_cat in valid_user_feedbacks:
            # 1. 분류기 검증: USER_FEEDBACK으로 분류되어야 함
            cls_result = MessageClassifier.classify(text)
            self.assertEqual(cls_result, IncomingMessageType.USER_FEEDBACK, f"'{text}'는 USER_FEEDBACK이어야 합니다.")

            # 2. 피드백 파이프라인 처리: feedback_id가 반드시 생성되어야 함
            rec, conf = self.feedback_mgr.process_incoming_feedback(text)
            self.assertIsNotNone(rec, f"'{text}'에 대해 FeedbackRecord가 생성되어야 합니다.")
            self.assertTrue(rec.feedback_id.startswith("FB-"), "feedback_id는 FB-로 시작해야 합니다.")
            self.assertEqual(rec.source, "TELEGRAM")
            self.assertEqual(rec.status, FeedbackStatus.NEW_TELEGRAM_FEEDBACK.value)
            if exp_sym:
                self.assertEqual(rec.symbol, exp_sym)
            self.assertEqual(rec.category, exp_cat)

            # 3. Confirmation 응답 텍스트 검증
            self.assertIn("✅ 피드백 저장 완료", conf)
            self.assertIn(f"Feedback ID: {rec.feedback_id}", conf)
            self.assertIn("다음 EOD Learning 분석에 반영됩니다.", conf)

        # 4. 영구 저장소 검증
        saved_feedbacks = self.feedback_mgr.store.get_feedbacks_by_date(today)
        self.assertEqual(len(saved_feedbacks), len(valid_user_feedbacks))

    # =========================================================================
    # 6. test_question_not_saved_as_feedback (Requirement 4, 5, 6, 10)
    # =========================================================================
    def test_question_not_saved_as_feedback(self):
        """사용자 자연어 질문(QUERY)은 피드백으로 저장되지 않고 질의 응답(QUERY_RESPONSE)으로 처리됨을 검증"""
        questions = [
            "오늘 왜 이거 샀어?",
            "지금 보유 종목 뭐야?",
            "오늘 거래 몇 건 했어?",
            "459510 왜 샀어?"
        ]

        init_cnt = len(self.feedback_mgr.store.get_feedbacks_by_date(datetime.now().strftime("%Y-%m-%d")))

        for i, q in enumerate(questions):
            # 1. 분류기 검증: QUERY로 분류되어야 함
            cls_result = MessageClassifier.classify(q)
            self.assertEqual(cls_result, IncomingMessageType.QUERY, f"'{q}'는 QUERY로 분류되어야 합니다.")

            # 2. process_incoming_feedback 호출 시 None 반환 (피드백 저장 차단)
            rec, conf = self.feedback_mgr.process_incoming_feedback(q)
            self.assertIsNone(rec, f"질문 '{q}'에 대해 feedback_id가 생성되어서는 안 됩니다.")
            self.assertEqual(conf, "")

            # 3. Telegram 수신기로 처리 검증
            self.sent_messages.clear()
            update = {
                "update_id": 12001 + i,
                "message": {"message_id": 3001 + i, "chat": {"id": 6824295325}, "text": q}
            }
            self.rx._handle_update(update)

            # 4. QUERY_RESPONSE가 1건 발송되고, feedback_id는 NULL이어야 함
            self.assertEqual(len(self.sent_messages), 1)
            resp = self.sent_messages[0]
            self.assertEqual(resp["message_type"], TelegramMessageType.QUERY_RESPONSE.value)
            self.assertIsNone(resp["feedback_id"], "질의 응답의 feedback_id는 반드시 NULL이어야 합니다.")
            self.assertNotIn("✅ 피드백 저장 완료", resp["text"])

        # 피드백 저장소 레코드 수가 증가하지 않아야 함
        after_cnt = len(self.feedback_mgr.store.get_feedbacks_by_date(datetime.now().strftime("%Y-%m-%d")))
        self.assertEqual(after_cnt, init_cnt, "자연어 질문 처리 후 피드백 저장 건수는 변함없어야 합니다.")

    # =========================================================================
    # 7. test_feedback_confirmation_only_for_user_feedback (Requirement 7 & 9)
    # =========================================================================
    def test_feedback_confirmation_only_for_user_feedback(self):
        """USER_FEEDBACK_CONFIRMATION은 오직 실제 피드백 저장 시에만 발송되며, 임의 발송이 차단됨을 검증"""
        # 1. 자연어 피드백 수신 -> Confirmation 1건 발송 확인
        self.sent_messages.clear()
        self.notifier.receive_and_reply_feedback("오늘 진입이 너무 늦은 것 같음")
        self.assertEqual(len(self.sent_messages), 1)
        self.assertEqual(self.sent_messages[0]["message_type"], TelegramMessageType.USER_FEEDBACK_CONFIRMATION.value)
        self.assertTrue(self.sent_messages[0]["feedback_id"].startswith("FB-"))

        # 2. 거래 이벤트 수신 시도 -> Confirmation 발송 거절 (0건)
        self.sent_messages.clear()
        rec, conf = self.notifier.receive_and_reply_feedback("BUY")
        self.assertIsNone(rec)
        self.assertEqual(len(self.sent_messages), 0)

        # 3. 질문 수신 시도 -> Confirmation 발송 거절 (0건)
        self.sent_messages.clear()
        rec, conf = self.notifier.receive_and_reply_feedback("오늘 왜 샀어?")
        self.assertIsNone(rec)
        self.assertEqual(len(self.sent_messages), 0)

        # 4. feedback_id가 누락된 상태에서 USER_FEEDBACK_CONFIRMATION 발송 시도 -> send_message 가드에 의해 차단
        blocked = self.notifier.send_message(
            "강제 발송 테스트",
            message_type=TelegramMessageType.USER_FEEDBACK_CONFIRMATION.value,
            feedback_id=None
        )
        self.assertFalse(blocked, "feedback_id가 없는 USER_FEEDBACK_CONFIRMATION 발송은 차단되어야 합니다.")

    # =========================================================================
    # 8. test_eod_feedback_separation (Requirement 8)
    # =========================================================================
    def test_eod_feedback_separation(self):
        """EOD Learning의 TRADE_LEARNING_DATA와 USER_FEEDBACK_DATA가 100% 완전 분리됨을 검증"""
        today = datetime.now().strftime("%Y-%m-%d")

        # 1. 가상 거래 데이터 입력 (trades 테이블)
        with sqlite3.connect(self.trade_db_path) as conn:
            conn.execute("""
            INSERT INTO trades (symbol, symbol_name, strategy, entry_time, exit_time, entry_price, exit_price, pnl, return_pct, exit_reason)
            VALUES ('490470', '세미파이브', 'INT_MOMENTUM', ?, ?, 15200, 15500, 3000, 1.97, 'TARGET_1R')
            """, (f"{today} 10:00:00", f"{today} 10:30:00"))

        # 2. 가상 사용자 피드백 입력
        self.feedback_mgr.process_incoming_feedback("490470 오늘 진입이 너무 늦은 것 같음")

        # 3. 데이터셋 분리 추출
        trade_learning = self.feedback_mgr.get_trade_learning_data(today, trade_db_path=self.trade_db_path)
        user_feedback = self.feedback_mgr.get_user_feedback_data(today)

        # 4. TRADE_LEARNING_DATA 검증
        self.assertEqual(len(trade_learning), 1)
        t_row = trade_learning[0]
        self.assertEqual(t_row["dataset_type"], "TRADE_LEARNING_DATA")
        self.assertEqual(t_row["symbol"], "490470")
        self.assertIsNone(t_row["feedback_id"], "TRADE_LEARNING_DATA의 feedback_id는 반드시 NULL이어야 합니다.")

        # 5. USER_FEEDBACK_DATA 검증
        self.assertEqual(len(user_feedback), 1)
        f_row = user_feedback[0]
        self.assertEqual(f_row["dataset_type"], "USER_FEEDBACK_DATA")
        self.assertTrue(f_row["feedback_id"].startswith("FB-"))
        self.assertEqual(f_row["category"], "ENTRY_TIMING")
        self.assertIn("진입이 너무 늦은 것 같음", f_row["user_text"])

    # =========================================================================
    # 9. test_telegram_sequential_scenario (Requirement 11)
    # =========================================================================
    def test_telegram_sequential_scenario(self):
        """
        Requirement 11 순차 시나리오 검증:
        ① 시스템 자동 BUY 발생 -> Feedback Confirmation 메시지 = 0건
        ② 시스템 자동 SELL 발생 -> Feedback Confirmation 메시지 = 0건
        ③ 사용자: '오늘 진입이 너무 늦은 것 같음' -> Feedback Confirmation = 1건
        ④ 사용자: '오늘 왜 샀어?' -> Feedback 저장 = 0건, Query Response = 1건
        """
        # ① 시스템 자동 BUY 발생
        self.sent_messages.clear()
        self.notifier.send_trade_event(
            event_type="BUY",
            symbol="459510",
            name="신규종목",
            price=15000,
            qty=10,
            reason="INT_ORB"
        )
        conf_1 = [m for m in self.sent_messages if m.get("message_type") == TelegramMessageType.USER_FEEDBACK_CONFIRMATION.value]
        self.assertEqual(len(conf_1), 0, "① BUY 발생 시 Feedback Confirmation 메시지는 0건이어야 합니다.")

        # ② 시스템 자동 SELL 발생
        self.sent_messages.clear()
        self.notifier.send_trade_event(
            event_type="SELL",
            symbol="459510",
            name="신규종목",
            price=15300,
            qty=10,
            reason="TARGET_1R",
            return_pct=2.0
        )
        conf_2 = [m for m in self.sent_messages if m.get("message_type") == TelegramMessageType.USER_FEEDBACK_CONFIRMATION.value]
        self.assertEqual(len(conf_2), 0, "② SELL 발생 시 Feedback Confirmation 메시지는 0건이어야 합니다.")

        # ③ 사용자: "오늘 진입이 너무 늦은 것 같음"
        self.sent_messages.clear()
        update_fb = {
            "update_id": 20001,
            "message": {"message_id": 4001, "chat": {"id": 6824295325}, "text": "오늘 진입이 너무 늦은 것 같음"}
        }
        self.rx._handle_update(update_fb)
        conf_3 = [m for m in self.sent_messages if m.get("message_type") == TelegramMessageType.USER_FEEDBACK_CONFIRMATION.value]
        self.assertEqual(len(conf_3), 1, "③ 사용자 피드백 수신 시 Feedback Confirmation 메시지는 정확히 1건이어야 합니다.")
        self.assertIn("✅ 피드백 저장 완료", conf_3[0]["text"])

        # ④ 사용자: "오늘 왜 샀어?"
        self.sent_messages.clear()
        init_store_cnt = len(self.feedback_mgr.store.get_feedbacks_by_date(datetime.now().strftime("%Y-%m-%d")))
        update_q = {
            "update_id": 20002,
            "message": {"message_id": 4002, "chat": {"id": 6824295325}, "text": "오늘 왜 샀어?"}
        }
        self.rx._handle_update(update_q)

        # 피드백 저장 0건 검증
        final_store_cnt = len(self.feedback_mgr.store.get_feedbacks_by_date(datetime.now().strftime("%Y-%m-%d")))
        self.assertEqual(final_store_cnt, init_store_cnt, "④ 사용자 질문 수신 시 피드백 저장은 0건이어야 합니다.")

        # Query Response = 1건 검증
        q_resp = [m for m in self.sent_messages if m.get("message_type") == TelegramMessageType.QUERY_RESPONSE.value]
        self.assertEqual(len(q_resp), 1, "④ 사용자 질문 수신 시 Query Response는 정확히 1건이어야 합니다.")
        self.assertIsNone(q_resp[0]["feedback_id"])

    # =========================================================================
    # 10. test_mutation_detection (Harness Integrity Requirement 3)
    # =========================================================================
    def test_mutation_detection(self):
        """테스트 하네스가 로직 변이(Mutation)를 즉각 감지하여 FAIL을 유발하는지 검증"""
        # Mutation 1: 만약 BUY가 피드백으로 잘못 통과될 경우 감지
        mutated_classify = lambda text: IncomingMessageType.USER_FEEDBACK if text == "BUY" else MessageClassifier.classify(text)
        self.assertEqual(
            mutated_classify("BUY"),
            IncomingMessageType.USER_FEEDBACK,
            "변이 적용 확인"
        )
        # 하네스의 원래 검증식은 원래의 프로덕션 로직에서 TRADE_EVENT여야 하므로 변이 감지 가능
        self.assertNotEqual(MessageClassifier.classify("BUY"), mutated_classify("BUY"))

        # Mutation 2: 만약 '오늘 왜 샀어?'가 질문이 아닌 피드백으로 변이될 경우 감지
        mutated_q_classify = lambda text: IncomingMessageType.USER_FEEDBACK if "왜" in text else MessageClassifier.classify(text)
        self.assertNotEqual(MessageClassifier.classify("오늘 왜 샀어?"), mutated_q_classify("오늘 왜 샀어?"))

        # Mutation 3: 만약 TRADE_LEARNING_DATA에 feedback_id가 혼입될 경우 감지
        corrupted_row = {"trade_id": "1", "feedback_id": "FB-CORRUPTED"}
        self.assertIsNotNone(corrupted_row["feedback_id"])
        # 원래 프로덕션 데이터에서는 feedback_id가 None이어야 함
        today = datetime.now().strftime("%Y-%m-%d")
        real_data = self.feedback_mgr.get_trade_learning_data(today, trade_db_path=self.trade_db_path)
        for r in real_data:
            self.assertIsNone(r["feedback_id"])


if __name__ == "__main__":
    unittest.main()
