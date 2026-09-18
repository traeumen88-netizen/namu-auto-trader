"""
tests/test_telegram_feedback_system.py
텔레그램 과거 데이터 재전송 차단, 메시지 멱등성(Idempotency),
사용자 피드백 수신 및 자동 분류, EOD 연동 및 전략 불변성 가드 검증 테스트
"""

import os
import json
import sqlite3
import tempfile
import shutil
import unittest
import unittest.mock
from datetime import datetime, timedelta

from core.telegram_feedback_manager import (
    TelegramFeedbackManager,
    TelegramIdempotencyManager,
    TelegramFeedbackStore,
    FeedbackRecord,
    FeedbackCategory,
    FeedbackStatus,
    CandidateStage,
    extract_symbol,
    extract_strategy,
    classify_category,
    classify_sentiment,
    classify_severity,
    format_feedback_confirmation,
    format_zero_trade_day_message,
    link_feedback_to_eod_data,
    evaluate_action_candidate
)
from core.telegram_notifier import TelegramNotifier
from core.telegram_receiver import TelegramReceiver, mask_token
from ml.eod_worker import EODRetrospectiveWorker
from database.persistence import ExperienceDB
from ml.model_registry import ModelRegistry
from config import settings
from strategies.full_strategy_suite import FullStrategySuite


class TestTelegramFeedbackSystem(unittest.TestCase):
    def setUp(self):
        self.tmp_dir = tempfile.mkdtemp()
        self.db_path = os.path.join(self.tmp_dir, "test_operational.db")
        self.trade_db_path = os.path.join(self.tmp_dir, "test_trades.db")
        self.exp_db_path = os.path.join(self.tmp_dir, "test_exp.db")
        self.reg_path = os.path.join(self.tmp_dir, "test_reg.json")
        self.audit_path = os.path.join(self.tmp_dir, "test_audit.jsonl")

        self._init_mock_databases()

        self.notifier = TelegramNotifier(token="MOCK_TOKEN", chat_id="MOCK_CHAT_ID", db_path=self.db_path)
        self.notifier.enabled = True
        self.feedback_mgr = TelegramFeedbackManager(db_path=self.db_path)

    def tearDown(self):
        shutil.rmtree(self.tmp_dir, ignore_errors=True)

    def _init_mock_databases(self):
        """테스트용 가상 SQLite DB 초기화"""
        # 1. trades db
        with sqlite3.connect(self.trade_db_path) as conn:
            conn.execute("""
            CREATE TABLE trades (
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

        # 2. operational db
        with sqlite3.connect(self.db_path) as conn:
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

    # =========================================================================
    # Test 1: test_no_previous_feedback_rebroadcast
    # =========================================================================
    def test_no_previous_feedback_rebroadcast(self):
        """
        과거 거래일(예: 금요일)의 데이터가 오늘 0건 거래일 때
        오늘의 신규 피드백/거래로 취급되어 재전송되는 현상이 완전 차단되었는지 검증
        """
        today = datetime.now().strftime("%Y-%m-%d")
        past_date = (datetime.now() - timedelta(days=3)).strftime("%Y-%m-%d")

        # 과거 거래일(금요일) 데이터만 DB에 주입 (오늘 데이터는 0건)
        with sqlite3.connect(self.trade_db_path) as conn:
            conn.execute("""
            INSERT INTO trades (symbol, symbol_name, strategy, entry_time, exit_time, entry_price, exit_price, pnl, return_pct, exit_reason)
            VALUES ('005930', '삼성전자', 'INT_ORB', ?, ?, 70000, 71500, 150000, 2.14, '목표가 익절')
            """, (f"{past_date} 09:30:00", f"{past_date} 10:00:00"))

        exp_db = ExperienceDB(db_path=self.exp_db_path, operational_db=self.db_path, trade_db=self.trade_db_path)
        registry = ModelRegistry(registry_file=self.reg_path, audit_log_file=self.audit_path)
        worker = EODRetrospectiveWorker(exp_db=exp_db, registry=registry)

        # Mock telegram notifier를 주입하여 실제로 발송되는 메시지 캡처
        sent_messages = []
        def mock_send_msg(text, **kwargs):
            sent_messages.append({"text": text, "kwargs": kwargs})
            return True

        self.notifier.send_message = mock_send_msg
        import core.telegram_notifier as tg_mod
        orig_notifier = tg_mod.telegram_notifier
        tg_mod.telegram_notifier = self.notifier

        try:
            worker.generate_and_send_eod_report({})
            # 검증 1: 발송된 메시지에 과거 종목(삼성전자 150,000원 수익)이 절대로 포함되지 않아야 함
            self.assertEqual(len(sent_messages), 1)
            sent_text = sent_messages[0]["text"]
            self.assertNotIn("삼성전자", sent_text)
            self.assertNotIn("150,000", sent_text)
            # 검증 2: 대신 ZERO_TRADE_DAY 안내 메시지가 발송되어야 함
            self.assertIn("신규 거래: 0건", sent_text)
            self.assertIn("이전 거래일 데이터는 재전송하지 않습니다", sent_text)
        finally:
            tg_mod.telegram_notifier = orig_notifier

    # =========================================================================
    # Test 2: test_zero_trade_day_message
    # =========================================================================
    def test_zero_trade_day_message(self):
        """오늘 신규 이벤트가 없는 경우 표준 양식 안내 메시지 생성 및 발송 검증"""
        msg = format_zero_trade_day_message()
        self.assertIn("📊 <b>오늘 거래 피드백</b>", msg)
        self.assertIn("신규 거래: 0건", msg)
        self.assertIn("신규 청산: 0건", msg)
        self.assertIn("신규 주요 이슈: 0건", msg)
        self.assertIn("금일 신규 거래/피드백이 없어 이전 거래일 데이터는 재전송하지 않습니다.", msg)

        # TelegramNotifier.send_zero_trade_day_message 호출 시 정상 발송 확인
        sent_log = []
        self.notifier.send_message = lambda text, **kwargs: sent_log.append(text) or True
        res = self.notifier.send_zero_trade_day_message(business_date="2026-09-14")
        self.assertTrue(res)
        self.assertEqual(len(sent_log), 1)
        self.assertIn("신규 거래: 0건", sent_log[0])

    # =========================================================================
    # Test 3: test_message_idempotency
    # =========================================================================
    def test_message_idempotency(self):
        """동일 이벤트가 여러 cycle 및 재시작 후에도 중복 전송되지 않는 멱등성 검증"""
        idemp = self.notifier.idempotency
        key = idemp.make_idempotency_key(
            message_type="TRADE_BUY",
            business_date="2026-09-14",
            event_id="EVT_001",
            trade_id="TRD_1001"
        )
        self.assertEqual(key, "TRADE_BUY:2026-09-14:EVT_001:TRD_1001:NONE")

        # 1. 초기 미발송 상태
        self.assertFalse(idemp.is_sent(key))

        # 2. 첫 발송 시도
        import unittest.mock
        with unittest.mock.patch("requests.post") as mock_post:
            mock_post.return_value.status_code = 200
            success1 = self.notifier.send_message(
                "매수 체결",
                idempotency_key=key,
                message_type="TRADE_BUY",
                business_date="2026-09-14",
                event_id="EVT_001",
                trade_id="TRD_1001"
            )
            self.assertTrue(success1)
            self.assertEqual(mock_post.call_count, 1)
            self.assertTrue(idemp.is_sent(key))

        # 3. 동일 cycle 또는 다음 cycle 재발송 시도 (Idempotent Skip)
        with unittest.mock.patch("requests.post") as mock_post2:
            success2 = self.notifier.send_message(
                "매수 체결 재전송",
                idempotency_key=key,
                message_type="TRADE_BUY",
                business_date="2026-09-14",
                event_id="EVT_001",
                trade_id="TRD_1001"
            )
            self.assertTrue(success2)
            # HTTP 발송 없이 멱등성 차단됨
            self.assertEqual(mock_post2.call_count, 0)

        # 4. 프로그램 재시작 시뮬레이션: 새로운 IdempotencyManager 인스턴스를 동일 DB로 로드
        restarted_idemp = TelegramIdempotencyManager(db_path=self.db_path)
        self.assertTrue(restarted_idemp.is_sent(key))

    # =========================================================================
    # Test 4: test_feedback_receive
    # =========================================================================
    def test_feedback_receive(self):
        """사용자 자연어 텍스트로부터 종목/전략/카테고리/감정/심각도 추출 검증"""
        text = "459510 오늘 진입이 너무 늦었음. 거래량 증가 확인 후 진입하도록 검토."
        now = datetime(2026, 9, 14, 14, 30, 0)
        rec, conf_msg = self.feedback_mgr.process_incoming_feedback(text, now=now)

        self.assertEqual(rec.symbol, "459510")
        self.assertEqual(rec.category, "ENTRY_TIMING")
        self.assertEqual(rec.sentiment, "NEGATIVE")
        self.assertIn(rec.severity, ("HIGH", "MEDIUM", "CRITICAL"))
        self.assertEqual(rec.source, "TELEGRAM")
        self.assertEqual(rec.status, "NEW TELEGRAM FEEDBACK")
        self.assertEqual(rec.business_date, "2026-09-14")
        self.assertTrue(rec.feedback_id.startswith("FB-20260914-"))

    # =========================================================================
    # Test 5: test_feedback_persistence
    # =========================================================================
    def test_feedback_persistence(self):
        """피드백이 SQLite DB에 정확히 보존되고 재시작 후에도 재조회되는지 검증"""
        text = "오늘 삼성전자 매수 판단은 좋았음."
        now = datetime(2026, 9, 14, 15, 0, 0)
        rec, _ = self.feedback_mgr.process_incoming_feedback(text, now=now)

        # 1. DB에서 ID로 조회
        loaded = self.feedback_mgr.store.get_feedback(rec.feedback_id)
        self.assertIsNotNone(loaded)
        self.assertEqual(loaded.feedback_id, rec.feedback_id)
        self.assertEqual(loaded.symbol, "005930")  # 삼성전자 자동 매핑
        self.assertEqual(loaded.sentiment, "POSITIVE")

        # 2. 날짜별 조회
        by_date = self.feedback_mgr.store.get_feedbacks_by_date("2026-09-14")
        self.assertEqual(len(by_date), 1)
        self.assertEqual(by_date[0].feedback_id, rec.feedback_id)

        # 3. 재시작 시뮬레이션
        new_store = TelegramFeedbackStore(db_path=self.db_path)
        re_loaded = new_store.get_feedback(rec.feedback_id)
        self.assertIsNotNone(re_loaded)
        self.assertEqual(re_loaded.user_text, text)

    # =========================================================================
    # Test 6: test_feedback_category
    # =========================================================================
    def test_feedback_category(self):
        """13대 범주 분류 및 불확실 시 GENERAL 기본값 및 종목/전략 미추정(None) 검증"""
        cases = [
            ("459510 오늘 진입이 너무 늦었음", FeedbackCategory.ENTRY_TIMING, "459510", None),
            ("너무 일찍 팔아서 아쉽다 매도 타이밍 성급", FeedbackCategory.EXIT_TIMING, None, None),
            ("VWAP Pullback은 지금 조건이 너무 느슨한 것 같음", FeedbackCategory.STRATEGY, None, "VWAP_PULLBACK"),
            ("계좌 손실폭 리스크 한도가 너무 위험함", FeedbackCategory.RISK, None, None),
            ("종목당 매수 수량과 비중 사이징을 줄여야 함", FeedbackCategory.POSITION_SIZING, None, None),
            ("체결오차와 슬리피지가 심해서 비싸게 체결됨", FeedbackCategory.SLIPPAGE, None, None),
            ("주문 전송 후 미체결 및 지연 발생", FeedbackCategory.EXECUTION, None, None),
            ("호가 데이터가 stale 상태로 지연 시세 발생", FeedbackCategory.DATA_QUALITY, None, None),
            ("오늘 장세에서 왜 아무것도 안 삼? 노 트레이드 이유", FeedbackCategory.NO_TRADE, None, None),
            ("과도한 매매와 뇌동매매로 수수료 낭비", FeedbackCategory.OVERTRADING, None, None),
            ("너무 안 삼 매매가 너무 적음 소극적임", FeedbackCategory.UNDERTRADING, None, None),
            ("하락장 변동성 장세 레짐 대응 필요", FeedbackCategory.MARKET_REGIME, None, None),
            ("오늘 날씨가 참 좋네요 화이팅입니다", FeedbackCategory.GENERAL, None, None),
        ]

        for text, expected_cat, expected_sym, expected_strat in cases:
            cat = classify_category(text)
            sym = extract_symbol(text)
            strat = extract_strategy(text)
            self.assertEqual(cat, expected_cat, f"Category mismatch for: {text}")
            self.assertEqual(sym, expected_sym, f"Symbol mismatch for: {text}")
            self.assertEqual(strat, expected_strat, f"Strategy mismatch for: {text}")

    # =========================================================================
    # Test 7: test_feedback_confirmation
    # =========================================================================
    def test_feedback_confirmation(self):
        """수신 확인 메시지가 요구된 형식에 부합하며 전략 변경 완료로 오해되지 않는지 검증"""
        rec = FeedbackRecord(
            feedback_id="FB-20260914-00017",
            received_at="2026-09-14 14:30:00",
            business_date="2026-09-14",
            user_text="459510 진입이 늦음",
            symbol="459510",
            strategy="VWAP_PULLBACK",
            category="ENTRY_TIMING",
            sentiment="NEGATIVE",
            severity="MEDIUM"
        )
        conf = format_feedback_confirmation(rec)

        self.assertIn("✅ 피드백 저장 완료", conf)
        self.assertIn("Feedback ID: FB-20260914-00017", conf)
        self.assertIn("Category: ENTRY_TIMING", conf)
        self.assertIn("Symbol: 459510", conf)
        self.assertIn("Status: STORED", conf)
        self.assertIn("다음 EOD Learning 분석에 반영됩니다.", conf)
        # 전략 변경 완료 문구는 없어야 함
        self.assertNotIn("전략 변경 완료", conf)
        self.assertNotIn("파라미터가 수정되었습니다", conf)

    # =========================================================================
    # Test 8: test_feedback_trade_link
    # =========================================================================
    def test_feedback_trade_link(self):
        """EOD Learning 시 사용자 피드백이 실제 거래 및 NO_TRADE 내역과 정상 결합되는지 검증"""
        today = "2026-09-14"

        # 1. 실제 체결된 거래 데이터 주입 (459510)
        with sqlite3.connect(self.trade_db_path) as conn:
            conn.execute("""
            INSERT INTO trades (symbol, symbol_name, strategy, entry_time, exit_time, entry_price, exit_price, pnl, return_pct, exit_reason)
            VALUES ('459510', '파두', 'VWAP_PULLBACK', '2026-09-14 10:00:00', '2026-09-14 11:00:00', 25000, 24500, -50000, -2.0, '손절 청산')
            """)

        # 2. NO_TRADE 데이터 주입 (000660)
        with sqlite3.connect(self.db_path) as conn:
            conn.execute("""
            INSERT INTO no_trade_records (iem_cd, name, timestamp, rule_score, ml_prob, expected_net_r, primary_reason, strategy_id)
            VALUES ('000660', 'SK하이닉스', '2026-09-14 09:40:00', 45.0, 0.45, 0.8, 'LOW_EXPECTED_EDGE', 'INT_ORB')
            """)

        # 3. 거래 있는 피드백 결합 검증 (459510)
        rec1 = FeedbackRecord(
            feedback_id="FB-20260914-00001",
            received_at="2026-09-14 11:30:00",
            business_date=today,
            user_text="459510 진입이 늦음",
            symbol="459510",
            strategy=None,
            category="ENTRY_TIMING",
            sentiment="NEGATIVE",
            severity="MEDIUM"
        )
        linked1 = link_feedback_to_eod_data(rec1, trade_db_path=self.trade_db_path, operational_db_path=self.db_path)
        self.assertEqual(linked1.status, FeedbackStatus.LINKED_TO_TRADES.value)
        self.assertIsNotNone(linked1.linked_trade_id)
        self.assertEqual(linked1.link_metadata["strategy"], "VWAP_PULLBACK")
        self.assertEqual(linked1.link_metadata["final_pnl"], -50000.0)

        # 4. NO_TRADE 피드백 결합 검증 (000660)
        rec2 = FeedbackRecord(
            feedback_id="FB-20260914-00002",
            received_at="2026-09-14 11:35:00",
            business_date=today,
            user_text="000660 왜 안 샀는지 의문",
            symbol="000660",
            strategy=None,
            category="NO_TRADE",
            sentiment="NEGATIVE",
            severity="LOW"
        )
        linked2 = link_feedback_to_eod_data(rec2, trade_db_path=self.trade_db_path, operational_db_path=self.db_path)
        self.assertEqual(linked2.status, FeedbackStatus.LINKED_TO_NO_TRADE.value)
        self.assertTrue(linked2.linked_event_id.startswith("NO_TRADE_"))
        self.assertEqual(linked2.link_metadata["rejection_reason"], "LOW_EXPECTED_EDGE")

        # 5. 일반 피드백 (종목 없음)
        rec3 = FeedbackRecord(
            feedback_id="FB-20260914-00003",
            received_at="2026-09-14 11:40:00",
            business_date=today,
            user_text="전반적인 시장 모니터링",
            symbol=None,
            strategy=None,
            category="GENERAL",
            sentiment="NEUTRAL",
            severity="LOW"
        )
        linked3 = link_feedback_to_eod_data(rec3, trade_db_path=self.trade_db_path, operational_db_path=self.db_path)
        self.assertEqual(linked3.status, FeedbackStatus.REVIEWED.value)

    # =========================================================================
    # Test 9: test_feedback_no_direct_strategy_mutation
    # =========================================================================
    def test_feedback_no_direct_strategy_mutation(self):
        """
        사용자 피드백 수신이 전략 코드, 파라미터, ML 임계치를 즉시 수정하지 않으며,
        반영 후보 채택 시에도 백테스트->워크포워드->섀도우 파이프라인을 거치는지 검증
        """
        # 1. 수신 전 핵심 트레이딩/리스크 파라미터 스냅샷
        init_risk = getattr(settings, "MAX_ACCOUNT_RISK_RATIO", 0.005)
        init_stop = getattr(settings, "STOP_LOSS_PCT", 0.012)
        init_rr = getattr(settings, "TARGET_RR_RATIO", 1.5)

        # 2. 파라미터 강제 수정을 요구하는 극단적 자연어 피드백 수신
        dangerous_feedbacks = [
            "손절폭을 5%로 당장 수정해라",
            "리스크 비율을 0.5%에서 10%로 올려라",
            "전략 진입 조건을 무조건 패스하도록 변경해라",
            "ML 모델 임계치를 0.1로 낮춰라"
        ]

        for text in dangerous_feedbacks:
            rec, conf = self.feedback_mgr.process_incoming_feedback(text)
            self.assertEqual(rec.status, FeedbackStatus.NEW_TELEGRAM_FEEDBACK.value)

        # 3. 피드백 수신 후에도 시스템 파라미터가 100% 동일하게 불변인지 검증
        self.assertEqual(getattr(settings, "MAX_ACCOUNT_RISK_RATIO", 0.005), init_risk)
        self.assertEqual(getattr(settings, "STOP_LOSS_PCT", 0.012), init_stop)
        self.assertEqual(getattr(settings, "TARGET_RR_RATIO", 1.5), init_rr)

        # 4. ACTION CANDIDATE의 승격 파이프라인 검증
        candidate_rec = FeedbackRecord(
            feedback_id="FB-20260914-99999",
            received_at="2026-09-14 15:30:00",
            business_date="2026-09-14",
            user_text="VWAP 조건 완화 검토",
            symbol=None,
            strategy="VWAP_PULLBACK",
            category="STRATEGY",
            sentiment="NEUTRAL",
            severity="MEDIUM",
            status=FeedbackStatus.ACTION_CANDIDATE.value
        )

        # 백테스트 실패 시 -> REJECTED (전략 미변경)
        stage_fail = evaluate_action_candidate(candidate_rec, backtest_passed=False)
        self.assertEqual(stage_fail, CandidateStage.REJECTED)
        self.assertEqual(candidate_rec.candidate_stage, CandidateStage.REJECTED.value)

        # 워크포워드 실패 시 -> REJECTED
        stage_fail2 = evaluate_action_candidate(candidate_rec, backtest_passed=True, walkforward_passed=False)
        self.assertEqual(stage_fail2, CandidateStage.REJECTED)

        # 섀도우 실패 시 -> REJECTED
        stage_fail3 = evaluate_action_candidate(candidate_rec, backtest_passed=True, walkforward_passed=True, shadow_passed=False)
        self.assertEqual(stage_fail3, CandidateStage.REJECTED)

        # 전수 통과 시에만 -> PROMOTION
        stage_ok = evaluate_action_candidate(candidate_rec, backtest_passed=True, walkforward_passed=True, shadow_passed=True)
        self.assertEqual(stage_ok, CandidateStage.PROMOTION)
        self.assertEqual(candidate_rec.candidate_stage, CandidateStage.PROMOTION.value)

    # =========================================================================
    # Telegram RX 10대 단위 테스트
    # =========================================================================

    # 1) test_telegram_getme
    @unittest.mock.patch("requests.get")
    def test_telegram_getme(self, mock_get):
        """Telegram getMe API 호출 및 봇 신원 응답, 토큰 마스킹 검증"""
        mock_resp = unittest.mock.MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = {
            "ok": True,
            "result": {
                "id": 8704110151,
                "is_bot": True,
                "first_name": "Namu_bot",
                "username": "Namu_choi_bot"
            }
        }
        mock_get.return_value = mock_resp

        rx = TelegramReceiver(token="8704110151:AAHibUhbqXraZMEeeA8Mj9Oe_EKTtKDNOF8", allowed_chat_id="6824295325", db_path=self.db_path)
        status = rx.check_api_connectivity()

        self.assertEqual(status["telegram_api"], "OK")
        self.assertIn("Namu_choi_bot", status["bot_identity"])
        self.assertEqual(status["details"]["bot_id"], 8704110151)
        # 토큰 마스킹 검증 (원문 전체 노출 차단)
        self.assertNotIn("AAHibUhbqXraZMEeeA8Mj9Oe_EKTtKDNOF8", status["details"]["token_masked"])
        self.assertTrue(status["details"]["token_masked"].startswith("870411..."))

    # 2) test_telegram_polling_start
    def test_telegram_polling_start(self):
        """Telegram Receiver 백그라운드 데몬 스레드 시작 및 상태 플래그, 안전 종료 검증"""
        rx = TelegramReceiver(token="MOCK_TOKEN", allowed_chat_id="6824295325", db_path=self.db_path)
        self.assertFalse(rx.running)
        self.assertIsNone(rx.thread)

        rx.start()
        try:
            self.assertTrue(rx.running)
            self.assertIsNotNone(rx.thread)
            self.assertTrue(rx.thread.is_alive())
            self.assertTrue(rx.thread.daemon)
        finally:
            rx.stop(timeout=1.0)
            self.assertFalse(rx.running)

    # 3) test_telegram_update_receive
    @unittest.mock.patch("requests.get")
    def test_telegram_update_receive(self, mock_get):
        """getUpdates로 수신된 update 메시지 객체의 정상 파싱 및 last_received_at 갱신 검증"""
        sample_update = {
            "update_id": 1001,
            "message": {
                "message_id": 501,
                "chat": {"id": 6824295325},
                "text": "안녕하세요",
                "date": int(datetime.now().timestamp())
            }
        }
        mock_resp = unittest.mock.MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = {"ok": True, "result": [sample_update]}
        mock_get.return_value = mock_resp

        rx = TelegramReceiver(notifier=self.notifier, token="MOCK_TOKEN", allowed_chat_id="6824295325", db_path=self.db_path)
        self.assertIsNone(rx.last_received_at)

        updates = rx._poll_once()
        self.assertEqual(len(updates), 1)
        self.assertEqual(updates[0]["update_id"], 1001)
        self.assertIsNotNone(rx.last_received_at)
        self.assertTrue(rx.last_poll_ok)

    # 4) test_telegram_offset_progress
    @unittest.mock.patch("requests.get")
    def test_telegram_offset_progress(self, mock_get):
        """update_id 수신 시 offset이 max(last_update_id)로 전진하고 SQLite에 영속 저장되는지 검증"""
        mock_resp = unittest.mock.MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = {
            "ok": True,
            "result": [
                {"update_id": 5001, "message": {"message_id": 701, "chat": {"id": 6824295325}, "text": "첫번째"}},
                {"update_id": 5005, "message": {"message_id": 702, "chat": {"id": 6824295325}, "text": "두번째"}}
            ]
        }
        mock_get.return_value = mock_resp

        rx = TelegramReceiver(notifier=self.notifier, token="MOCK_TOKEN", allowed_chat_id="6824295325", db_path=self.db_path)
        self.assertEqual(rx.last_update_id, 0)

        rx._poll_once()
        self.assertEqual(rx.last_update_id, 5005)

        # 재시작 시뮬레이션: 새로운 receiver 인스턴스에서 5005가 복원되는지 확인
        restarted_rx = TelegramReceiver(token="MOCK_TOKEN", allowed_chat_id="6824295325", db_path=self.db_path)
        self.assertEqual(restarted_rx.last_update_id, 5005)

        # 다음 폴링 시 params["offset"]이 5006으로 요청되는지 검증
        mock_get.reset_mock()
        mock_resp.json.return_value = {"ok": True, "result": []}
        restarted_rx._poll_once()
        self.assertEqual(mock_get.call_args[1]["params"]["offset"], 5006)

    # 5) test_telegram_duplicate_update
    def test_telegram_duplicate_update(self):
        """동일 message_id 중복 수신 시 2회차 처리가 건너뛰어지는지 검증"""
        rx = TelegramReceiver(notifier=self.notifier, token="MOCK_TOKEN", allowed_chat_id="6824295325", db_path=self.db_path)

        processed_texts = []
        self.notifier.receive_and_reply_feedback = lambda txt, **kw: processed_texts.append(txt) or (None, None)

        update = {
            "update_id": 6001,
            "message": {"message_id": 888, "chat": {"id": 6824295325}, "text": "테스트 피드백"}
        }

        # 1회차 수신
        rx._handle_update(update)
        self.assertEqual(len(processed_texts), 1)
        self.assertTrue(rx._is_message_processed(888))

        # 2회차 중복 수신
        rx._handle_update(update)
        self.assertEqual(len(processed_texts), 1)  # 여전히 1회 (중복 처리 차단)

    # 6) test_telegram_chat_authorization
    def test_telegram_chat_authorization(self):
        """허가되지 않은 chat_id로부터의 메시지가 UNAUTHORIZED_CHAT으로 차단되고 무시되는지 검증"""
        rx = TelegramReceiver(notifier=self.notifier, token="MOCK_TOKEN", allowed_chat_id="6824295325", db_path=self.db_path)

        dispatched = []
        self.notifier.receive_and_reply_feedback = lambda txt, **kw: dispatched.append(txt) or (None, None)
        self.notifier.send_message = lambda txt, **kw: dispatched.append(txt) or True

        unauthorized_update = {
            "update_id": 7001,
            "message": {"message_id": 991, "chat": {"id": 11112222}, "text": "해킹 시도 ping"}
        }

        rx._handle_update(unauthorized_update)
        # 허가되지 않은 사용자의 메시지는 dispatch되지 않음
        self.assertEqual(len(dispatched), 0)
        # 처리 완료 기록도 생성되지 않음
        self.assertFalse(rx._is_message_processed(991))

    # 7) test_telegram_ping_pong
    def test_telegram_ping_pong(self):
        """ping 수신 시 콘솔 로그 출력 및 pong 즉시 회신 검증"""
        rx = TelegramReceiver(notifier=self.notifier, token="MOCK_TOKEN", allowed_chat_id="6824295325", db_path=self.db_path)

        sent_replies = []
        self.notifier.send_message = lambda text, **kw: sent_replies.append({"text": text, "kw": kw}) or True

        import io
        from contextlib import redirect_stdout
        buf = io.StringIO()

        ping_update = {
            "update_id": 8001,
            "message": {"message_id": 1234, "chat": {"id": 6824295325}, "text": "ping"}
        }

        with redirect_stdout(buf):
            rx._handle_update(ping_update)

        out = buf.getvalue()
        # 콘솔 출력 포맷 검증
        self.assertIn("[TELEGRAM RX]", out)
        self.assertIn("chat_id=6824295325", out)
        self.assertIn("message_id=1234", out)
        self.assertIn('text="ping"', out)
        self.assertIn("ping received -> sending pong", out)

        # pong 발송 검증
        self.assertEqual(len(sent_replies), 1)
        self.assertEqual(sent_replies[0]["text"], "pong")

    # 8) test_telegram_feedback_receive
    def test_telegram_feedback_receive(self):
        """피드백 자연어 텍스트 수신 시 종목/전략/카테고리/감정 파싱 검증"""
        text = "459510 오늘 진입이 너무 늦었음. 거래량 증가 확인 후 진입하도록 검토."
        now = datetime(2026, 9, 14, 14, 30, 0)
        rec, conf_msg = self.feedback_mgr.process_incoming_feedback(text, now=now)

        self.assertEqual(rec.symbol, "459510")
        self.assertEqual(rec.category, "ENTRY_TIMING")
        self.assertEqual(rec.sentiment, "NEGATIVE")
        self.assertIn(rec.severity, ("HIGH", "MEDIUM", "CRITICAL"))
        self.assertEqual(rec.source, "TELEGRAM")
        self.assertEqual(rec.status, "NEW TELEGRAM FEEDBACK")
        self.assertEqual(rec.business_date, "2026-09-14")
        self.assertTrue(rec.feedback_id.startswith("FB-20260914-"))

    # 9) test_telegram_feedback_persist
    def test_telegram_feedback_persist(self):
        """Telegram 수신기를 통해 들어온 피드백이 SQLite DB에 영구 보존되는지 검증"""
        rx = TelegramReceiver(notifier=self.notifier, token="MOCK_TOKEN", allowed_chat_id="6824295325", db_path=self.db_path)
        self.notifier.send_message = lambda text, **kw: True

        update = {
            "update_id": 9001,
            "message": {"message_id": 1501, "chat": {"id": 6824295325}, "text": "459510 체결 슬리피지가 과다함"}
        }
        rx._handle_update(update)

        # DB에서 저장된 피드백 확인
        records = self.feedback_mgr.store.get_feedbacks_by_date(datetime.now().strftime("%Y-%m-%d"))
        self.assertGreaterEqual(len(records), 1)
        saved = [r for r in records if r.symbol == "459510"]
        self.assertGreaterEqual(len(saved), 1)
        self.assertEqual(saved[0].category, "SLIPPAGE")
        self.assertEqual(saved[0].source, "TELEGRAM")

    # 10) test_telegram_feedback_confirmation
    def test_telegram_feedback_confirmation(self):
        """Telegram 피드백 수신 후 사용자에게 Confirmation 회신 메시지가 정확히 전송되는지 검증"""
        rx = TelegramReceiver(notifier=self.notifier, token="MOCK_TOKEN", allowed_chat_id="6824295325", db_path=self.db_path)

        sent_messages = []
        self.notifier.send_message = lambda text, **kw: sent_messages.append({"text": text, "kw": kw}) or True

        update = {
            "update_id": 9002,
            "message": {"message_id": 1502, "chat": {"id": 6824295325}, "text": "459510 오늘 진입이 너무 늦었음"}
        }
        rx._handle_update(update)

        self.assertEqual(len(sent_messages), 1)
        conf_text = sent_messages[0]["text"]
        self.assertIn("✅ 피드백 저장 완료", conf_text)
        self.assertIn("Category: ENTRY_TIMING", conf_text)
        self.assertIn("Symbol: 459510", conf_text)
        self.assertIn("다음 EOD Learning 분석에 반영됩니다.", conf_text)
        self.assertNotIn("전략 변경 완료", conf_text)


if __name__ == "__main__":
    unittest.main()
