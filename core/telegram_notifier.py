"""
core/telegram_notifier.py
Telegram Bot API를 통한 실시간 트레이딩 알림 및 원격 제어 모듈
"""

import os
import json
import logging
import requests
from datetime import datetime

logger = logging.getLogger("TelegramNotifier")
logger.propagate = False
if not logger.handlers:
    handler = logging.StreamHandler()
    handler.setFormatter(logging.Formatter("[%(asctime)s] %(levelname)s: [TELEGRAM] %(message)s"))
    logger.addHandler(handler)
    logger.setLevel(logging.INFO)


class TelegramEventFilter:
    """
    엄격한 실전 계좌(LIVE) 전용 텔레그램 필터.
    MOCK 또는 비-LIVE 계좌의 거래 이벤트, 주문, 체결, 손익, 거절 알림을 100% 원천 차단.
    """
    @staticmethod
    def is_live_account(
        account: Optional[str] = None,
        account_no: Optional[str] = None,
        trading_mode: Optional[str] = None,
        strict_explicit: bool = False
    ) -> bool:
        acc_str = str(account or "").upper().strip()
        mode_str = str(trading_mode or "").upper().strip()

        # 1. 명시적 MOCK / PAPER / 모의투자 차단
        if any(m in acc_str for m in ("MOCK", "PAPER", "SIMUL", "TEST", "DEMO")):
            return False
        if any(m in mode_str for m in ("MOCK", "PAPER", "SIMUL", "TEST", "DEMO")):
            return False

        # 2. 계좌번호 대조 차단 (설정 파일 기준)
        try:
            from config import settings
            acc_clean = str(account_no or "").replace("-", "").strip()
            mock_clean = str(getattr(settings, "ACCOUNT_MOCK", "") or "").replace("-", "").strip()
            live_clean = str(getattr(settings, "ACCOUNT_LIVE", "") or "").replace("-", "").strip()

            if mock_clean and acc_clean and acc_clean == mock_clean:
                return False
            if live_clean and acc_clean and acc_clean == live_clean:
                return True
        except Exception:
            pass

        # 3. 명시적 LIVE 확인
        if acc_str == "LIVE" or mode_str == "LIVE":
            return True

        if strict_explicit:
            return False

        # 레거시 호환: 별도 인자가 전혀 주어지지 않은 경우 (단위 테스트 등) 기본 통과
        if not account and not account_no and not trading_mode:
            return True

        return False

    @staticmethod
    def should_send(
        account: Optional[str] = None,
        account_no: Optional[str] = None,
        trading_mode: Optional[str] = None,
        event_category: str = "TRADE"
    ) -> bool:
        if event_category in ("SYSTEM_COMMON", "MOBILE_LINK", "RECONNECT"):
            return True
        return TelegramEventFilter.is_live_account(account=account, account_no=account_no, trading_mode=trading_mode)


from typing import Optional


class TelegramNotifier:
    def __init__(self, token=None, chat_id=None, db_path="data/operational_v16.db"):
        self.base_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        self.config_file = os.path.join(self.base_dir, "config", "telegram_config.json")
        self.token = token
        self.chat_id = chat_id
        self.enabled = True
        self._load_config()
        from core.telegram_feedback_manager import TelegramFeedbackManager
        self.db_path = db_path
        self.feedback_manager = TelegramFeedbackManager(db_path=db_path)
        self.idempotency = self.feedback_manager.idempotency
        self.send_status = "OK"
        self.last_sent_at = None
        from core.telegram_receiver import TelegramReceiver
        self.receiver = TelegramReceiver(
            notifier=self,
            token=self.token,
            allowed_chat_id=self.chat_id,
            db_path=db_path
        )
        self.last_update_id = self.receiver.last_update_id

    def _load_config(self):
        if os.path.exists(self.config_file):
            try:
                with open(self.config_file, "r", encoding="utf-8") as f:
                    cfg = json.load(f)
                    self.token = cfg.get("bot_token", self.token)
                    self.chat_id = cfg.get("chat_id", self.chat_id)
                    self.enabled = cfg.get("enabled", True)
            except Exception as e:
                logger.error(f"설정 파일 로드 실패: {e}")

    def save_config(self, token, chat_id, user_name="", enabled=True):
        os.makedirs(os.path.dirname(self.config_file), exist_ok=True)
        data = {
            "bot_token": token,
            "chat_id": chat_id,
            "user_name": user_name,
            "enabled": enabled,
            "updated_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        }
        with open(self.config_file, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2, ensure_ascii=False)
        self.token = token
        self.chat_id = chat_id
        self.enabled = enabled
        logger.info(f"텔레그램 설정 저장 완료 (chat_id={chat_id})")

    def send_message(
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
        """텔레그램 메시지 발송 (Idempotency 보장 및 메시지 타입별 feedback_id 엄격 격리)"""
        # 메시지 타입별 feedback_id 격리 검증 (Requirement 2, 3, 7, 9)
        from core.telegram_feedback_manager import TelegramMessageType
        if message_type in (TelegramMessageType.USER_FEEDBACK_CONFIRMATION.value, "FEEDBACK_CONFIRMATION"):
            if not feedback_id or not str(feedback_id).startswith("FB-"):
                logger.error(f"[텔레그램 발송 차단] USER_FEEDBACK_CONFIRMATION은 유효한 feedback_id가 필수입니다: {feedback_id}")
                return False
        elif message_type in (
            TelegramMessageType.TRADE_ALERT.value,
            TelegramMessageType.SYSTEM_ALERT.value,
            TelegramMessageType.EOD_REPORT.value,
            TelegramMessageType.QUERY_RESPONSE.value,
            TelegramMessageType.COMMAND_RESPONSE.value,
            "ZERO_TRADE_DAY"
        ) or (message_type and str(message_type).startswith("TRADE_")):
            # 거래 이벤트, 시스템 알림, EOD 리포트, 질의/명령어 응답은 절대로 feedback_id를 가질 수 없음 (strictly NULL)
            feedback_id = None

        # 멱등성 키 자동 계산 및 중복 발송 차단 검사 (Requirement 2)
        if not idempotency_key and message_type:
            b_date = business_date or datetime.now().strftime("%Y-%m-%d")
            idempotency_key = self.idempotency.make_idempotency_key(
                message_type=message_type,
                business_date=b_date,
                event_id=event_id,
                trade_id=trade_id,
                feedback_id=feedback_id
            )

        if idempotency_key:
            if self.idempotency.is_sent(idempotency_key):
                logger.info(f"[텔레그램 멱등성 차단] 이미 발송된 메시지입니다 (Key={idempotency_key})")
                return True

        if not self.enabled:
            return False
        if not self.token or not self.chat_id:
            logger.warning("텔레그램 토큰 또는 Chat ID가 설정되지 않았습니다.")
            return False

        url = f"https://api.telegram.org/bot{self.token}/sendMessage"
        payload = {
            "chat_id": self.chat_id,
            "text": text,
            "parse_mode": parse_mode,
            "disable_web_page_preview": False
        }
        if reply_markup:
            payload["reply_markup"] = reply_markup

        try:
            resp = requests.post(url, json=payload, timeout=10)
            if resp.status_code == 200:
                self.send_status = "OK"
                self.last_sent_at = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
                logger.info("텔레그램 메시지 발송 성공!")
                if idempotency_key:
                    b_date = business_date or datetime.now().strftime("%Y-%m-%d")
                    m_type = message_type or "GENERAL"
                    self.idempotency.mark_sent(
                        idempotency_key=idempotency_key,
                        message_type=m_type,
                        business_date=b_date,
                        event_id=event_id,
                        trade_id=trade_id,
                        feedback_id=feedback_id
                    )
                return True
            else:
                self.send_status = "FAIL"
                logger.error(f"텔레그램 발송 실패 ({resp.status_code}): {resp.text}")
                return False
        except Exception as e:
            self.send_status = "FAIL"
            logger.error(f"텔레그램 발송 통신 에러: {e}")
            return False

    def send_mobile_dashboard_link(self, tunnel_url):
        """원격 모바일 대시보드 URL 발송"""
        now_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        lines = [
            "📱 <b>[나무증권 AI 퀀트] 모바일 대시보드 오픈</b>",
            "",
            "오늘의 보안 원격 접속 링크입니다:",
            f"🔗 <code>{tunnel_url}</code>",
            "",
            f"• <b>발급시각</b>: {now_str}",
            "• <b>안내</b>: 아래 버튼을 터치하면 바로 대시보드가 열립니다."
        ]
        text = "\n".join(lines)
        reply_markup = {
            "inline_keyboard": [
                [{"text": "📱 모바일 대시보드 열기", "url": tunnel_url}]
            ]
        }
        return self.send_message(text, reply_markup=reply_markup)

    def send_reconnect_link(self, tunnel_url):
        """인터넷 재연결 알림 발송"""
        now_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        lines = [
            "🔄 <b>[나무증권 AI 퀀트] 인터넷 회선 자동 복구</b>",
            "",
            "인터넷 연결이 재개되어 새로운 모바일 접속 링크가 발급되었습니다:",
            f"🔗 <code>{tunnel_url}</code>",
            "",
            f"• <b>갱신시각</b>: {now_str}",
            "• <b>안내</b>: 아래 버튼을 누르면 끊김 없이 대시보드로 이동합니다."
        ]
        text = "\n".join(lines)
        reply_markup = {
            "inline_keyboard": [
                [{"text": "📱 복구된 대시보드 열기", "url": tunnel_url}]
            ]
        }
        return self.send_message(text, reply_markup=reply_markup)

    def send_trade_event(
        self,
        event_type,
        symbol,
        name,
        price,
        qty,
        reason="",
        return_pct=0.0,
        pnl_won=0,
        entry_price=0.0,
        dashboard_url=None,
        trade_id=None,
        event_id=None,
        business_date=None,
        account=None,
        account_no=None,
        trading_mode=None
    ):
        """체결 알림 발송 (BUY/SELL) - Idempotency 및 LIVE 계좌 전용 필터 적용"""
        # [Section 14 & 15] MOCK 또는 비-LIVE 거래 알림 원천 차단
        if not TelegramEventFilter.is_live_account(account=account, account_no=account_no, trading_mode=trading_mode):
            logger.info(
                f"[TELEGRAM_FILTER_BLOCK] MOCK/비-LIVE 거래 알림 차단: "
                f"account={account}, mode={trading_mode}, symbol={symbol}, event={event_type}"
            )
            return False

        now = datetime.now()
        now_str = now.strftime("%H:%M:%S")
        b_date = business_date or now.strftime("%Y-%m-%d")
        is_buy = event_type.upper() == "BUY"

        if is_buy:
            header = "🟢 <b>[매수 체결 완료]</b>"
            total_amt = price * qty
            body_lines = [
                f"• <b>종목명</b>: {name} (<code>{symbol}</code>)",
                f"• <b>체결단가</b>: {price:,}원 ({qty}주)",
                f"• <b>매수총액</b>: {total_amt:,}원",
                f"• <b>매수전략</b>: {reason or 'N/A'}",
                f"• <b>체결시각</b>: {now_str}"
            ]
        else:
            pnl_icon = "🚀" if return_pct >= 3.0 else ("📈" if return_pct > 0 else "📉")
            won_str = f" ({pnl_won:+,}원)" if pnl_won != 0 else ""
            header = f"🔴 <b>[매도 체결 완료]</b> {pnl_icon} <b>{return_pct:+.2f}%</b>"
            body_lines = [
                f"• <b>종목명</b>: {name} (<code>{symbol}</code>)",
                f"• <b>체결단가</b>: {price:,}원 ({qty}주)",
            ]
            if entry_price > 0:
                body_lines.append(f"• <b>진입단가</b>: {int(entry_price):,}원")
            body_lines.extend([
                f"• <b>실현손익</b>: <b>{return_pct:+.2f}%</b>{won_str}",
                f"• <b>매도사유</b>: {reason or 'N/A'}",
                f"• <b>체결시각</b>: {now_str}"
            ])

        text = header + "\n\n" + "\n".join(body_lines)
        reply_markup = None
        if dashboard_url:
            reply_markup = {
                "inline_keyboard": [
                    [{"text": "📊 대시보드 바로가기", "url": dashboard_url}]
                ]
            }

        msg_type = f"TRADE_{event_type.upper()}"
        t_id = trade_id or f"{symbol}_{int(price)}_{int(qty)}"
        idempotency_key = self.idempotency.make_idempotency_key(
            message_type=msg_type,
            business_date=b_date,
            event_id=event_id,
            trade_id=t_id,
            feedback_id=None
        )

        return self.send_message(
            text,
            reply_markup=reply_markup,
            idempotency_key=idempotency_key,
            message_type=msg_type,
            business_date=b_date,
            event_id=event_id,
            trade_id=t_id,
            feedback_id=None
        )

    def send_system_alert(self, title, message, event_id=None, business_date=None, account=None, account_no=None, trading_mode=None):
        """시스템 상태 및 이상 감지 알림 (MOCK 관련 알림 차단)"""
        if account or trading_mode or account_no:
            if not TelegramEventFilter.is_live_account(account=account, account_no=account_no, trading_mode=trading_mode):
                logger.info(f"[TELEGRAM_FILTER_BLOCK] MOCK 시스템 알림 차단: title={title}, account={account}")
                return False

        now = datetime.now()
        now_str = now.strftime("%Y-%m-%d %H:%M:%S")
        b_date = business_date or now.strftime("%Y-%m-%d")
        lines = [
            f"⚠️ <b>[시스템 알림] {title}</b>",
            "",
            message,
            "",
            f"• <b>시각</b>: {now_str}"
        ]
        text = "\n".join(lines)
        idempotency_key = None
        if event_id:
            idempotency_key = self.idempotency.make_idempotency_key(
                message_type="SYSTEM_ALERT",
                business_date=b_date,
                event_id=event_id
            )
        return self.send_message(
            text,
            idempotency_key=idempotency_key,
            message_type="SYSTEM_ALERT",
            business_date=b_date,
            event_id=event_id
        )

    def send_eod_retrospective_report(self, report_text, dashboard_url=None, business_date=None, is_zero_trade_day=False):
        """장 마감 EOD 결산 및 칭찬/반성 피드백 리포트 발송 (Idempotency 보장)"""
        b_date = business_date or datetime.now().strftime("%Y-%m-%d")
        msg_type = "ZERO_TRADE_DAY" if is_zero_trade_day else "EOD_REPORT"
        idempotency_key = self.idempotency.make_idempotency_key(
            message_type=msg_type,
            business_date=b_date
        )

        reply_markup = None
        if dashboard_url and not is_zero_trade_day:
            reply_markup = {
                "inline_keyboard": [
                    [{"text": "📱 모바일 대시보드에서 복기하기", "url": dashboard_url}]
                ]
            }
        return self.send_message(
            report_text,
            reply_markup=reply_markup,
            idempotency_key=idempotency_key,
            message_type=msg_type,
            business_date=b_date
        )

    def send_zero_trade_day_message(self, business_date=None):
        """오늘 신규 이벤트가 없는 경우 안내 메시지 발송 (Requirement 1)"""
        from core.telegram_feedback_manager import format_zero_trade_day_message
        text = format_zero_trade_day_message()
        return self.send_eod_retrospective_report(
            report_text=text,
            dashboard_url=None,
            business_date=business_date,
            is_zero_trade_day=True
        )

    # =========================================================================
    # 사용자 피드백 수신 및 확인 응답 처리 (Requirement 3, 4, 7)
    # =========================================================================
    def process_user_feedback(self, user_text: str, now: Optional[datetime] = None):
        """자연어 피드백 수신 및 자동 분류, DB 저장, 확인 메시지 생성"""
        return self.feedback_manager.process_incoming_feedback(user_text, now=now)

    def receive_and_reply_feedback(self, user_text: str, chat_id: Optional[str] = None, now: Optional[datetime] = None):
        """자연어 피드백을 저장하고 Telegram으로 Confirmation 즉시 회신 (Requirement 7 & 9)"""
        from core.telegram_feedback_manager import TelegramMessageType
        rec, conf_msg = self.process_user_feedback(user_text, now=now)
        if not rec or not rec.feedback_id:
            logger.info(f"[TELEGRAM] 피드백 저장 대상 아님 (feedback_id=NULL): Confirmation 발송 생략 (text='{user_text[:30]}')")
            return None, ""

        conf_key = self.idempotency.make_idempotency_key(
            message_type=TelegramMessageType.USER_FEEDBACK_CONFIRMATION.value,
            business_date=rec.business_date,
            feedback_id=rec.feedback_id
        )
        self.send_message(
            conf_msg,
            idempotency_key=conf_key,
            message_type=TelegramMessageType.USER_FEEDBACK_CONFIRMATION.value,
            business_date=rec.business_date,
            feedback_id=rec.feedback_id
        )
        return rec, conf_msg

    def handle_query(self, query_text: str, chat_id: Optional[str] = None, message_id: Optional[int] = None) -> str:
        """
        사용자 자연어 질문(QUERY) 처리 및 분석 응답 회신 (Requirement 4 & 5)
        * 중요: feedback_id를 절대 생성하지 않으며 Feedback Store에 저장하지 않음 (feedback_id=NULL)
        """
        from core.telegram_feedback_manager import QueryResponder, TelegramMessageType
        target_chat = chat_id or self.chat_id
        b_date = datetime.now().strftime("%Y-%m-%d")

        reply_text = QueryResponder.answer_query(
            query_text=query_text,
            business_date=b_date,
            trade_db_path="data/trade_history_v7.db",
            operational_db_path=self.db_path or "data/operational_v16.db"
        )

        msg_key = f"QUERY_RESP_{target_chat}_{message_id}" if message_id else None
        self.send_message(
            reply_text,
            idempotency_key=msg_key,
            message_type=TelegramMessageType.QUERY_RESPONSE.value,
            business_date=b_date,
            feedback_id=None
        )
        return reply_text

    def handle_command(self, command_text: str, chat_id: Optional[str] = None, message_id: Optional[int] = None) -> str:
        """슬래시(/) 명령어 처리 및 응답 회신 (Requirement 4)"""
        from core.telegram_feedback_manager import TelegramMessageType
        target_chat = chat_id or self.chat_id
        cmd = command_text.strip().split()[0].lower()

        if cmd in ("/start", "/help"):
            reply_text = (
                "🤖 <b>[나무증권 AI 퀀트 봇 안내]</b>\n\n"
                "• <b>질문(QUERY)</b>: '오늘 왜 샀어?', '보유 종목 뭐야?', '오늘 거래 몇 건 했어?'\n"
                "• <b>피드백(FEEDBACK)</b>: '459510 진입이 너무 늦었음', '손절이 너무 빠름'\n"
                "• <b>상태 확인</b>: /status, ping"
            )
        elif cmd == "/status":
            health = self.get_health()
            reply_text = (
                f"📊 <b>[시스템 상태]</b>\n\n"
                f"• 발신 상태: {health.get('send')}\n"
                f"• 수신 상태: {health.get('receive')}\n"
                f"• 마지막 발신: {health.get('last_sent_at')}\n"
                f"• 마지막 수신: {health.get('last_received_at')}"
            )
        else:
            reply_text = f"ℹ️ 등록되지 않은 명령어입니다: {cmd} (/help 로 사용법 확인)"

        msg_key = f"CMD_RESP_{target_chat}_{message_id}" if message_id else None
        self.send_message(
            reply_text,
            idempotency_key=msg_key,
            message_type=TelegramMessageType.COMMAND_RESPONSE.value,
            feedback_id=None
        )
        return reply_text

    def poll_incoming_feedback_updates(self):
        """Telegram getUpdates API 폴링 - Receiver에 위임하여 getUpdates 중복 호출 방지"""
        if not self.enabled or not self.token:
            return []
        if self.receiver:
            return self.receiver._poll_once()
        return []

    def start_receiver(self):
        """백그라운드 수신 데몬 스레드 가동"""
        if self.receiver:
            self.receiver.start()

    def stop_receiver(self, timeout: float = 2.0):
        """백그라운드 수신 데몬 스레드 중지"""
        if self.receiver:
            self.receiver.stop(timeout=timeout)

    def get_health(self):
        """Telegram 발신/수신 건강도 분리 스냅샷 (Requirement 7)"""
        if self.receiver:
            return self.receiver.get_health()
        return {
            "send": self.send_status,
            "receive": "FAIL",
            "last_sent_at": self.last_sent_at,
            "last_received_at": None,
            "last_update_id": 0
        }


    def send_after_hours_spike_alert(self, symbol: str, name: str, after_hours_return_pct: float, volume: int = 0):
        """시간외 급등 감지 알림 (정규장 매수 전송 아님!)"""
        lines = [
            "📌 <b>시간외 급등 감지</b>",
            f"종목: {name} (<code>{symbol}</code>)",
            f"시간외 상승률: +{after_hours_return_pct:.1f}%",
            "상태: 익일 관찰 후보 등록",
            "※ 시간외 급등만으로 매수하지 않습니다.",
            "※ 익일 정규장 조건 충족 시에만 매수 검토합니다."
        ]
        text = "\n".join(lines)
        return self.send_message(text, message_type="AFTER_HOURS_SPIKE", event_id=f"AH_{symbol}_{datetime.now().strftime('%Y%m%d')}")

    def send_after_hours_daily_summary(self, summary: dict):
        """[Section 4] 시간외 거래 종료 후 1일 1회 요약 Telegram 메시지 발송 (Idempotent)"""
        target_date = summary.get("trade_date", datetime.now().strftime("%Y-%m-%d"))
        idempotency_key = f"{target_date}_AFTER_HOURS_DAILY_SUMMARY"

        lines = [
            "📊 <b>[시간외 급등 일일 분석]</b>",
            "",
            f"거래일: {summary.get('trade_date', target_date)}",
            "분석 시간: 15:30~18:00",
            "",
            f"• 급등 감지: {summary.get('detected_count', 0)}종목",
            f"• 다음장 재검증 후보: {summary.get('next_session_watchlist_count', 0)}종목",
            f"• 거래량 부족: {summary.get('volume_deficient_count', 0)}종목",
            f"• 거래대금 부족: {summary.get('turnover_deficient_count', 0)}종목",
            f"• 가격왜곡 의심: {summary.get('price_distortion_count', 0)}종목",
            f"• 최고 상승률: +{summary.get('max_return', 0.0):.1f}%",
            f"• 평균 상승률: +{summary.get('avg_return', 0.0):.1f}%"
        ]

        top_cands = summary.get("top_candidates", [])
        if top_cands:
            lines.append("")
            lines.append("주요 후보:")
            for c in top_cands[:5]:
                lines.append(f"• {c['name']} +{c['return_pct']:.1f}%")

        lines.extend([
            "",
            "→ 상세 종목 데이터는 시스템에 저장",
            "→ 다음 정규장 재검증 예정",
            "→ 시간외 직접 매수 없음"
        ])

        text = "\n".join(lines)
        return self.send_message(
            text=text,
            message_type="AFTER_HOURS_DAILY_SUMMARY",
            idempotency_key=idempotency_key,
            business_date=target_date,
            event_id=idempotency_key
        )


# 싱글톤 인스턴스
telegram_notifier = TelegramNotifier()
