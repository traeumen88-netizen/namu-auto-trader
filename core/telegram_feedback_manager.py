"""
core/telegram_feedback_manager.py
텔레그램 사용자 자연어 피드백 수신/자동분류, 메시지 멱등성(Idempotency),
과거 데이터 재전송 차단 및 EOD Learning 연동 거버넌스 모듈
"""

import os
import re
import json
import sqlite3
import logging
from enum import Enum
from datetime import datetime
from typing import Optional, Dict, Any, List, Tuple
from dataclasses import dataclass, field, asdict

logger = logging.getLogger("TelegramFeedbackManager")
if not logger.handlers:
    handler = logging.StreamHandler()
    handler.setFormatter(logging.Formatter("[%(asctime)s] %(levelname)s: [TG_FEEDBACK] %(message)s"))
    logger.addHandler(handler)
    logger.setLevel(logging.INFO)


# =============================================================================
# 1. 카테고리 및 상태 정의 (Requirement 4, 8 & 9)
# =============================================================================

class IncomingMessageType(str, Enum):
    USER_FEEDBACK = "USER_FEEDBACK"
    QUERY = "QUERY"
    COMMAND = "COMMAND"
    TRADE_EVENT = "TRADE_EVENT"
    SYSTEM_EVENT = "SYSTEM_EVENT"
    UNKNOWN = "UNKNOWN"


class TelegramMessageType(str, Enum):
    TRADE_ALERT = "TRADE_ALERT"
    SYSTEM_ALERT = "SYSTEM_ALERT"
    EOD_REPORT = "EOD_REPORT"
    USER_FEEDBACK_CONFIRMATION = "USER_FEEDBACK_CONFIRMATION"
    QUERY_RESPONSE = "QUERY_RESPONSE"
    COMMAND_RESPONSE = "COMMAND_RESPONSE"


class FeedbackCategory(str, Enum):
    ENTRY_TIMING = "ENTRY_TIMING"
    EXIT_TIMING = "EXIT_TIMING"
    STRATEGY = "STRATEGY"
    RISK = "RISK"
    POSITION_SIZING = "POSITION_SIZING"
    SLIPPAGE = "SLIPPAGE"
    EXECUTION = "EXECUTION"
    DATA_QUALITY = "DATA_QUALITY"
    NO_TRADE = "NO_TRADE"
    OVERTRADING = "OVERTRADING"
    UNDERTRADING = "UNDERTRADING"
    MARKET_REGIME = "MARKET_REGIME"
    GENERAL = "GENERAL"


class FeedbackStatus(str, Enum):
    NEW_TELEGRAM_FEEDBACK = "NEW TELEGRAM FEEDBACK"
    REVIEWED = "REVIEWED"
    LINKED_TO_TRADES = "LINKED TO TRADES"
    LINKED_TO_NO_TRADE = "LINKED TO NO_TRADE"
    ACTION_CANDIDATE = "ACTION CANDIDATE"


class CandidateStage(str, Enum):
    NONE = "NONE"
    PROPOSED_CHANGE = "PROPOSED CHANGE"
    BACKTEST = "BACKTEST"
    WALK_FORWARD = "WALK-FORWARD"
    SHADOW = "SHADOW"
    PROMOTION = "PROMOTION"
    REJECTED = "REJECTED"


# =============================================================================
# 2. 피드백 레코드 데이터클래스 (Requirement 3)
# =============================================================================

@dataclass
class FeedbackRecord:
    feedback_id: str                          # e.g., FB-20260914-00001
    received_at: str                          # YYYY-MM-DD HH:MM:SS
    business_date: str                        # YYYY-MM-DD
    user_text: str                            # 사용자의 원본 텍스트
    symbol: Optional[str]                     # 6자리 종목코드 또는 None
    strategy: Optional[str]                   # 전략 ID 또는 None
    category: str                             # FeedbackCategory
    sentiment: str                            # POSITIVE, NEGATIVE, NEUTRAL
    severity: str                             # LOW, MEDIUM, HIGH, CRITICAL
    source: str = "TELEGRAM"                  # 고정값: TELEGRAM
    status: str = FeedbackStatus.NEW_TELEGRAM_FEEDBACK.value
    linked_trade_id: Optional[str] = None
    linked_event_id: Optional[str] = None
    link_metadata: Dict[str, Any] = field(default_factory=dict)
    candidate_stage: str = CandidateStage.NONE.value


# =============================================================================
# 3. 자연어 파싱 및 자동 분류 엔진 (Requirement 3 & 4)
# =============================================================================

KNOWN_SYMBOLS_MAP = {
    "삼성전자": "005930",
    "SK하이닉스": "000660",
    "하이닉스": "000660",
    "NAVER": "035420",
    "네이버": "035420",
    "카카오": "035720",
    "LG에너지솔루션": "373220",
    "현대차": "005380",
    "기아": "000270",
    "한미반도체": "042700",
    "알테오젠": "196170",
    "두산로보틱스": "454910",
    "삼천당제약": "000250",
}

KNOWN_STRATEGIES_MAP = {
    "VWAP": "VWAP_PULLBACK",
    "VWAP PULLBACK": "VWAP_PULLBACK",
    "VWAP_PULLBACK": "VWAP_PULLBACK",
    "눌림목": "VWAP_PULLBACK",
    "눌림": "VWAP_PULLBACK",
    "ORB": "INT_ORB",
    "INT_ORB": "INT_ORB",
    "시초가 돌파": "INT_ORB",
    "장초반 돌파": "INT_ORB",
    "BREAKOUT": "BREAKOUT",
    "돌파": "BREAKOUT",
    "박스권 돌파": "BREAKOUT",
    "MOMENTUM": "MOMENTUM",
    "모멘텀": "MOMENTUM",
    "PDH_BREAKOUT": "PDH_BREAKOUT",
    "전일고점": "PDH_BREAKOUT",
    "SWING_PULLBACK": "SWING_PULLBACK",
    "스윙 눌림": "SWING_PULLBACK"
}


def extract_symbol(text: str) -> Optional[str]:
    """
    텍스트에서 6자리 종목코드 또는 명확한 종목명만 추출.
    추출할 수 없으면 None 반환 (임의 추정 금지).
    """
    if not text:
        return None

    # 1. 6자리 숫자 코드 직접 매칭 (가장 높은 우선순위)
    code_match = re.search(r"\b(\d{6})\b", text)
    if code_match:
        return code_match.group(1)

    # 2. 알려진 대표 종목명 매칭
    for name, code in KNOWN_SYMBOLS_MAP.items():
        if name in text:
            return code

    return None


def extract_strategy(text: str) -> Optional[str]:
    """
    텍스트에서 명확히 언급된 전략 ID 추출.
    확실치 않으면 None 반환 (임의 추정 금지).
    """
    if not text:
        return None

    text_upper = text.upper()
    for kw, strat_id in KNOWN_STRATEGIES_MAP.items():
        if kw.upper() in text_upper:
            return strat_id

    return None


def classify_category(text: str) -> FeedbackCategory:
    """
    자연어 텍스트를 13개 카테고리 중 하나로 자동 분류.
    불확실한 경우 GENERAL 반환.
    """
    if not text:
        return FeedbackCategory.GENERAL

    t = text.lower()

    # 1. 진입 타이밍
    if any(k in t for k in ["진입이 너무 늦", "진입이 늦", "진입이 빠", "진입 타이밍", "늦게 진입", "추격", "조기 진입", "진입 시점", "진입이", "진입은"]):
        return FeedbackCategory.ENTRY_TIMING

    # 2. 청산/매도 타이밍
    if any(k in t for k in ["청산 타이밍", "매도 타이밍", "너무 일찍 팔", "익절 타이밍", "손절 타이밍", "버텼어야", "탈출", "조기 청산", "매도 시점", "손절이 너무 빠", "손절이 빠", "손절이", "익절이", "청산이"]):
        return FeedbackCategory.EXIT_TIMING

    # 3. 슬리피지/체결오차
    if any(k in t for k in ["슬리피지", "체결오차", "스프레드", "비싸게 체결", "밀림", "체결 밀림"]):
        return FeedbackCategory.SLIPPAGE

    # 4. 주문 실행/네트워크/API
    if any(k in t for k in ["미체결", "발주 실패", "주문 전송", "주문 지연", "취소 지연", "거절", "api 오류", "라우터"]):
        return FeedbackCategory.EXECUTION

    # 5. 데이터 품질/호가 신선도
    if any(k in t for k in ["stale", "호가 지연", "시세 지연", "데이터 지연", "quote", "호가잔량 오류", "데이터 오류"]):
        return FeedbackCategory.DATA_QUALITY

    # 6. 포지션 사이징/비중
    if any(k in t for k in ["비중", "수량", "사이징", "투자금액", "몰빵", "분할 매수", "비중 조절"]):
        return FeedbackCategory.POSITION_SIZING

    # 7. 리스크/손실 한도
    if any(k in t for k in ["리스크", "계좌 위험", "손실폭", "위험 한도", "드로다운", "손실 한도"]):
        return FeedbackCategory.RISK

    # 8. 소극적 매매
    if any(k in t for k in ["너무 안 삼", "매매가 너무 적음", "소극적", "과도한 관망", "거래 부족", "거래가 너무 적음"]):
        return FeedbackCategory.UNDERTRADING

    # 9. 과도한 매매
    if any(k in t for k in ["과도한 매매", "너무 많이 삼", "잦은 매매", "뇌동매매", "과잉 매매"]):
        return FeedbackCategory.OVERTRADING

    # 10. NO_TRADE 관련
    if any(k in t for k in ["노트레이드", "노 트레이드", "no trade", "no_trade", "왜 안 삼", "안 삼", "매매가 안 됨", "기회 놓침", "거래 없음", "안 샀", "매매 없음"]):
        return FeedbackCategory.NO_TRADE

    # 11. 시장 장세/레짐
    if any(k in t for k in ["장세", "하락장", "상승장", "횡보장", "지수 급락", "변동성 장세", "레짐"]):
        return FeedbackCategory.MARKET_REGIME

    # 12. 전략 룰/조건
    if extract_strategy(text) is not None or any(k in t for k in ["전략", "조건이 너무", "느슨", "엄격", "로직", "규칙", "필터", "셋업 조건", "조건이", "자주 들어", "vwap", "orb", "breakout", "눌림"]):
        return FeedbackCategory.STRATEGY

    # 13. 기본값
    return FeedbackCategory.GENERAL


def classify_sentiment(text: str) -> str:
    """감정 분류 (POSITIVE, NEGATIVE, NEUTRAL)"""
    if not text:
        return "NEUTRAL"
    t = text.lower()
    if any(k in t for k in ["좋았음", "좋았", "훌륭", "나이스", "잘함", "수익", "성공", "만족", "굿", "최고"]):
        return "POSITIVE"
    if any(k in t for k in ["늦었음", "손실", "아쉽", "문제", "실패", "느슨", "망함", "불만", "나쁨", "오류"]):
        return "NEGATIVE"
    return "NEUTRAL"


def classify_severity(text: str) -> str:
    """심각도 분류 (LOW, MEDIUM, HIGH, CRITICAL)"""
    if not text:
        return "LOW"
    t = text.lower()
    if any(k in t for k in ["긴급", "치명적", "심각", "당장", "망함", "버그"]):
        return "CRITICAL"
    if any(k in t for k in ["너무", "많이", "위험", "손실이 큼", "경고"]):
        return "HIGH"
    if any(k in t for k in ["검토", "주의", "수정", "개선 필요"]):
        return "MEDIUM"
    return "LOW"


# =============================================================================
# 3.1. 메시지 분류 엔진 (Message Classifier, Requirement 4, 5, 6)
# =============================================================================

class MessageClassifier:
    """
    수신된 텔레그램 메시지를 USER_FEEDBACK, QUERY, COMMAND, TRADE_EVENT, SYSTEM_EVENT로 엄격히 분리
    """
    TRADE_EVENT_EXACT_TOKENS = {
        "BUY", "SELL", "ORDER_CREATED", "ORDER_SENT", "ORDER_ACK", "FILL",
        "PARTIAL_FILL", "UNFILLED", "STOP", "TARGET", "SCALE_OUT", "TRAILING",
        "TIME_STOP", "EOD", "POSITION_OPEN", "POSITION_CLOSED"
    }

    SYSTEM_ALERT_MARKERS = [
        "🟢 [매수 체결 완료]", "🔴 [매도 체결 완료]", "[매수 체결 완료]", "[매도 체결 완료]",
        "🚀 매수 체결", "🚨 손절 체결", "🎯 익절 체결", "[시스템 알림]",
        "[나무증권 ai 퀀트]", "📊 오늘 거래 피드백", "eod 결산",
        "✅ 피드백 저장 완료", "• 체결단가:", "• 실현손익:", "• 매수총액:", "• 진입단가:"
    ]

    USER_OPINION_MARKERS = [
        "너무", "늦었", "빨랐", "늦은", "빠른", "같음", "본다", "좋았", "아쉽",
        "하지마", "마라", "줄여", "늘려", "어때", "왜", "어째서", "생각", "의견", "판단은"
    ]

    QUESTION_MARKERS = [
        "왜", "이유", "어째서", "어떻게", "뭐야", "무엇", "무슨",
        "몇 건", "몇건", "몇 주", "몇주", "몇개", "얼마", "어디", "언제",
        "알려줘", "보여줘", "조회", "확인해줘", "현황", "상태 알려",
        "?", "？"
    ]

    FEEDBACK_INTENT_MARKERS = [
        "너무 늦", "너무 빠", "늦었", "빨랐", "늦은", "빠른", "일찍 팔",
        "추격", "자주 들어", "뇌동", "비중", "줄여", "늘려", "손절", "익절",
        "진입", "청산", "슬리피지", "오류", "버텼어야", "안 좋아",
        "좋았음", "좋았", "훌륭", "아쉽", "문제", "개선", "피드백", "불만",
        "느슨", "엄격", "판단은", "수정", "변경", "올려", "낮춰", "임계치",
        "파라미터", "조건", "비율", "리스크", "바꿔", "당장"
    ]

    @classmethod
    def is_trade_event(cls, text: str) -> bool:
        """거래 체결/이벤트 및 시스템 발송 알림인지 판별 (Requirement 2 & 3)"""
        if not text:
            return False
        t_clean = text.strip()
        t_lower = t_clean.lower()

        # 사용자 의견/비판/평가 단어가 포함되어 있다면 거래 이벤트가 아님
        if any(m in t_lower for m in cls.USER_OPINION_MARKERS):
            return False

        # 1. 시스템 템플릿 마커 검사
        for marker in cls.SYSTEM_ALERT_MARKERS:
            if marker.lower() in t_lower:
                return True

        # 2. 정확한 이벤트 토큰 일치 검사 (e.g. "BUY", "SELL", "FILL", "[BUY]", "BUY 459510")
        t_upper = t_clean.upper().replace("[", "").replace("]", "").strip()
        words = t_upper.split()
        if words and words[0] in cls.TRADE_EVENT_EXACT_TOKENS:
            # 1~4단어 이내의 순수 이벤트 신호인 경우
            if len(words) <= 4:
                return True

        # 3. 체결 관련 문구 검사
        if any(k in t_lower for k in ["체결 완료", "매수 체결", "매도 체결", "주문 접수", "손절 체결", "익절 체결"]):
            if any(c in t_lower for c in ["체결단가", "주", "원", "수량", "호가"]):
                return True

        return False

    @classmethod
    def is_command(cls, text: str) -> bool:
        """슬래시(/) 명령어 여부 판별"""
        if not text:
            return False
        t = text.strip()
        return t.startswith("/") or t.lower() in ("ping", "pong")

    @classmethod
    def is_query(cls, text: str) -> bool:
        """자연어 질문(QUERY) 여부 판별 (Requirement 5 & 6)"""
        if not text:
            return False
        t = text.strip()
        # 명령어이거나 거래 이벤트이면 쿼리가 아님
        if cls.is_command(t) or cls.is_trade_event(t):
            return False

        # 물음표나 질문 의도 키워드가 포함되어 있는지 검사
        has_marker = any(m in t for m in cls.QUESTION_MARKERS)
        return has_marker

    @classmethod
    def is_user_feedback(cls, text: str) -> bool:
        """사용자 자연어 피드백(USER_FEEDBACK) 여부 판별 (Requirement 1 & 6)"""
        if not text:
            return False
        t = text.strip()
        # 명령어, 거래 이벤트, 질문은 피드백 대상에서 원천 제외
        if cls.is_command(t) or cls.is_trade_event(t) or cls.is_query(t):
            return False

        t_lower = t.lower()
        # 피드백 의도 키워드 검사
        if any(k in t_lower for k in cls.FEEDBACK_INTENT_MARKERS):
            return True

        # 특정 피드백 카테고리로 분류되는 경우 (GENERAL 제외)
        cat = classify_category(t)
        if cat != FeedbackCategory.GENERAL:
            return True

        return False

    @classmethod
    def classify(cls, text: str) -> IncomingMessageType:
        """
        메시지를 엄격히 분류:
        COMMAND -> TRADE_EVENT -> QUERY -> USER_FEEDBACK -> UNKNOWN
        """
        if not text or not text.strip():
            return IncomingMessageType.UNKNOWN

        text = text.strip()

        # 1. 명령어
        if cls.is_command(text):
            return IncomingMessageType.COMMAND

        # 2. 거래 이벤트 / 시스템 알림
        if cls.is_trade_event(text):
            return IncomingMessageType.TRADE_EVENT

        # 3. 질문/조회 (QUERY)
        if cls.is_query(text):
            return IncomingMessageType.QUERY

        # 4. 사용자 피드백 (USER_FEEDBACK)
        if cls.is_user_feedback(text):
            return IncomingMessageType.USER_FEEDBACK

        return IncomingMessageType.UNKNOWN


# =============================================================================
# 3.2. 자연어 질의 응답 엔진 (Query Responder, Requirement 4 & 5)
# =============================================================================

class QueryResponder:
    """
    자연어 질문(QUERY)을 분석하여 피드백 저장 없이 거래 정보/잔고/분석 결과를 즉시 회신
    """
    @classmethod
    def answer_query(
        cls,
        query_text: str,
        business_date: Optional[str] = None,
        trade_db_path: str = "data/trade_history_v7.db",
        operational_db_path: str = "data/operational_v16.db"
    ) -> str:
        b_date = business_date or datetime.now().strftime("%Y-%m-%d")
        t = query_text.strip()
        sym = extract_symbol(t)

        # 1. 보유 종목 / 잔고 질의
        if any(k in t for k in ["보유 종목", "보유종목", "포지션", "잔고", "지금 뭐 들고"]):
            positions = []
            if os.path.exists(operational_db_path):
                try:
                    with sqlite3.connect(operational_db_path, timeout=5.0) as conn:
                        conn.row_factory = sqlite3.Row
                        cur = conn.execute("""
                            SELECT symbol, name, total_qty, entry_price
                            FROM positions
                            WHERE status = 'OPEN' AND total_qty > 0
                        """)
                        for r in cur.fetchall():
                            positions.append(f"• {r['name']} ({r['symbol']}): {r['total_qty']}주 (평균단가 {int(r['entry_price']):,}원)")
                except Exception:
                    pass

            if positions:
                return f"📊 <b>[현재 보유 포지션]</b>\n\n" + "\n".join(positions)
            else:
                return "📊 <b>[현재 보유 포지션]</b>\n\n현재 보유 중인 주식 포지션이 없습니다. (예수금 100% 관망 중)"

        # 2. 오늘 거래 건수 질의
        if any(k in t for k in ["몇 건", "몇건", "몇 번", "몇번", "거래 수", "체결 수"]):
            cnt = 0
            if os.path.exists(trade_db_path):
                try:
                    with sqlite3.connect(trade_db_path, timeout=5.0) as conn:
                        cur = conn.execute("SELECT COUNT(*) FROM trades WHERE date(entry_time) = date(?)", (b_date,))
                        cnt = cur.fetchone()[0]
                except Exception:
                    pass
            return f"📊 <b>[거래 건수 조회]</b>\n\n금일({b_date}) 체결 완료된 거래는 총 {cnt}건입니다."

        # 3. 매수 사유 / 손해 사유 질의
        if any(k in t for k in ["왜", "이유", "산 이유", "손해", "사유"]):
            if sym:
                # 특정 종목 질의
                if os.path.exists(trade_db_path):
                    try:
                        with sqlite3.connect(trade_db_path, timeout=5.0) as conn:
                            conn.row_factory = sqlite3.Row
                            cur = conn.execute("""
                                SELECT symbol, symbol_name, strategy, entry_time, exit_time,
                                       entry_price, exit_price, pnl, return_pct, exit_reason
                                FROM trades
                                WHERE (symbol = ? OR symbol_name = ?)
                                ORDER BY trade_id DESC LIMIT 1
                            """, (sym, sym))
                            r = cur.fetchone()
                            if r:
                                pnl_pct = float(r["return_pct"])
                                won = int(r["pnl"])
                                return (
                                    f"🔍 <b>[종목 거래 분석: {r['symbol_name']}({r['symbol']})]</b>\n\n"
                                    f"• 진입 전략: {r['strategy']}\n"
                                    f"• 체결 시각: {r['entry_time']}\n"
                                    f"• 진입 단가: {int(r['entry_price']):,}원\n"
                                    f"• 청산 단가: {int(r['exit_price']):,}원 ({pnl_pct:+.2f}%, {won:+,}원)\n"
                                    f"• 청산 사유: {r['exit_reason'] or 'N/A'}"
                                )
                    except Exception:
                        pass

                # 체결 이력이 없으면 NO_TRADE 기각 이력 검색
                if os.path.exists(operational_db_path):
                    try:
                        with sqlite3.connect(operational_db_path, timeout=5.0) as conn:
                            conn.row_factory = sqlite3.Row
                            cur = conn.execute("""
                                SELECT iem_cd, name, timestamp, primary_reason, rule_score, ml_prob
                                FROM no_trade_records
                                WHERE iem_cd = ?
                                ORDER BY record_id DESC LIMIT 1
                            """, (sym,))
                            nt = cur.fetchone()
                            if nt:
                                return (
                                    f"🔍 <b>[종목 미체결 분석: {nt['name']}({nt['iem_cd']})]</b>\n\n"
                                    f"• 상태: NO_TRADE (진입 차단)\n"
                                    f"• 차단 사유: {nt['primary_reason']}\n"
                                    f"• 판단 시각: {nt['timestamp']}\n"
                                    f"• 룰 점수: {nt['rule_score']}, ML 확률: {nt['ml_prob']}"
                                )
                    except Exception:
                        pass

                return f"🔍 <b>[종목 분석: {sym}]</b>\n\n해당 종목의 최근 체결 또는 차단 기록을 찾을 수 없습니다."

            else:
                # 종목 미지정 "오늘 왜 샀어?"
                today_trades = []
                if os.path.exists(trade_db_path):
                    try:
                        with sqlite3.connect(trade_db_path, timeout=5.0) as conn:
                            conn.row_factory = sqlite3.Row
                            cur = conn.execute("""
                                SELECT symbol, symbol_name, strategy, entry_price, return_pct
                                FROM trades
                                WHERE date(entry_time) = date(?)
                                ORDER BY trade_id ASC
                            """, (b_date,))
                            for r in cur.fetchall():
                                today_trades.append(f"• {r['symbol_name']}({r['symbol']}): 전략={r['strategy']}, 단가={int(r['entry_price']):,}원 ({float(r['return_pct']):+.2f}%)")
                    except Exception:
                        pass

                if today_trades:
                    return f"🔍 <b>[금일({b_date}) 매수 종목 사유]</b>\n\n" + "\n".join(today_trades)
                else:
                    return f"🔍 <b>[금일({b_date}) 매매 분석]</b>\n\n오늘 체결된 신규 매수 거래가 없습니다."

        # 4. 일반 질의 기본 회신
        return (
            f"ℹ️ <b>[AI 퀀트 안내]</b>\n\n"
            f"문의하신 내용: \"{t}\"\n\n"
            f"• 거래 이유/종목/잔고에 대한 질의에 실시간 응답하고 있습니다.\n"
            f"• 매매에 대한 개선 의견(피드백)은 '459510 진입이 너무 늦음'과 같이 서술형으로 전달해 주시면 EOD 분석에 반영됩니다."
        )


# =============================================================================
# 4. 메시지 멱등성 관리자 (Telegram Message Idempotency Store, Requirement 2)
# =============================================================================

class TelegramIdempotencyManager:
    """
    동일 이벤트가 여러 cycle에서 중복 전송되지 않도록 SQLite 영구 저장소에
    message_type, event_id, trade_id, feedback_id, business_date 기반 Unique Key 기록 및 검사
    """
    def __init__(self, db_path: str = "data/operational_v16.db"):
        self.db_path = db_path
        os.makedirs(os.path.dirname(os.path.abspath(self.db_path)), exist_ok=True)
        self._init_table()

    def _get_conn(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path, timeout=10.0)
        conn.row_factory = sqlite3.Row
        return conn

    def _init_table(self):
        try:
            with self._get_conn() as conn:
                conn.execute("""
                CREATE TABLE IF NOT EXISTS telegram_sent_messages (
                    idempotency_key TEXT PRIMARY KEY,
                    message_type TEXT NOT NULL,
                    event_id TEXT,
                    trade_id TEXT,
                    feedback_id TEXT,
                    business_date TEXT NOT NULL,
                    sent_at TEXT NOT NULL,
                    status TEXT NOT NULL DEFAULT 'SENT',
                    content_hash TEXT
                )
                """)
                conn.execute("CREATE INDEX IF NOT EXISTS idx_tg_sent_date ON telegram_sent_messages(business_date)")
        except Exception as e:
            logger.error(f"Failed to init telegram_sent_messages table: {e}")

    @staticmethod
    def make_idempotency_key(
        message_type: str,
        business_date: str,
        event_id: Optional[str] = None,
        trade_id: Optional[str] = None,
        feedback_id: Optional[str] = None
    ) -> str:
        """Unique Key 생성: message_type:business_date:event_id:trade_id:feedback_id"""
        return f"{message_type.upper()}:{business_date}:{event_id or 'NONE'}:{trade_id or 'NONE'}:{feedback_id or 'NONE'}"

    def is_sent(self, idempotency_key: str) -> bool:
        """해당 키의 메시지가 이미 전송 완료(SENT) 상태인지 확인"""
        try:
            with self._get_conn() as conn:
                cur = conn.execute(
                    "SELECT 1 FROM telegram_sent_messages WHERE idempotency_key = ? AND status = 'SENT'",
                    (idempotency_key,)
                )
                return cur.fetchone() is not None
        except Exception as e:
            logger.warning(f"Error checking idempotency for {idempotency_key}: {e}")
            return False

    def mark_sent(
        self,
        idempotency_key: str,
        message_type: str,
        business_date: str,
        event_id: Optional[str] = None,
        trade_id: Optional[str] = None,
        feedback_id: Optional[str] = None,
        content_hash: Optional[str] = None
    ) -> bool:
        """메시지 전송 성공 시 SENT 상태 영구 기록 (중복 방지)"""
        now_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        try:
            with self._get_conn() as conn:
                conn.execute("""
                INSERT OR REPLACE INTO telegram_sent_messages (
                    idempotency_key, message_type, event_id, trade_id, feedback_id,
                    business_date, sent_at, status, content_hash
                ) VALUES (?, ?, ?, ?, ?, ?, ?, 'SENT', ?)
                """, (
                    idempotency_key, message_type.upper(), event_id, trade_id, feedback_id,
                    business_date, now_str, content_hash
                ))
            return True
        except Exception as e:
            logger.error(f"Failed to mark telegram message sent ({idempotency_key}): {e}")
            return False


# =============================================================================
# 5. 피드백 영구 저장소 (Feedback Store, Requirement 3 & 5)
# =============================================================================

class TelegramFeedbackStore:
    """
    수신된 텔레그램 피드백을 SQLite DB에 보관하고,
    EOD Learning 및 거래 내역 결합을 지원하는 저장소
    """
    def __init__(self, db_path: str = "data/operational_v16.db"):
        self.db_path = db_path
        os.makedirs(os.path.dirname(os.path.abspath(self.db_path)), exist_ok=True)
        self._init_table()

    def _get_conn(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path, timeout=10.0)
        conn.row_factory = sqlite3.Row
        return conn

    def _init_table(self):
        try:
            with self._get_conn() as conn:
                conn.execute("""
                CREATE TABLE IF NOT EXISTS telegram_feedbacks (
                    feedback_id TEXT PRIMARY KEY,
                    received_at TEXT NOT NULL,
                    business_date TEXT NOT NULL,
                    user_text TEXT NOT NULL,
                    symbol TEXT,
                    strategy TEXT,
                    category TEXT NOT NULL,
                    sentiment TEXT NOT NULL,
                    severity TEXT NOT NULL,
                    source TEXT NOT NULL DEFAULT 'TELEGRAM',
                    status TEXT NOT NULL DEFAULT 'NEW TELEGRAM FEEDBACK',
                    linked_trade_id TEXT,
                    linked_event_id TEXT,
                    link_metadata TEXT,
                    candidate_stage TEXT DEFAULT 'NONE'
                )
                """)
                conn.execute("CREATE INDEX IF NOT EXISTS idx_fb_date ON telegram_feedbacks(business_date)")
                conn.execute("CREATE INDEX IF NOT EXISTS idx_fb_sym ON telegram_feedbacks(symbol)")
        except Exception as e:
            logger.error(f"Failed to init telegram_feedbacks table: {e}")

    def save_feedback(self, record: FeedbackRecord) -> bool:
        """피드백 레코드 DB 저장"""
        try:
            with self._get_conn() as conn:
                conn.execute("""
                INSERT OR REPLACE INTO telegram_feedbacks (
                    feedback_id, received_at, business_date, user_text, symbol,
                    strategy, category, sentiment, severity, source, status,
                    linked_trade_id, linked_event_id, link_metadata, candidate_stage
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """, (
                    record.feedback_id,
                    record.received_at,
                    record.business_date,
                    record.user_text,
                    record.symbol,
                    record.strategy,
                    record.category,
                    record.sentiment,
                    record.severity,
                    record.source,
                    record.status,
                    record.linked_trade_id,
                    record.linked_event_id,
                    json.dumps(record.link_metadata, ensure_ascii=False),
                    record.candidate_stage
                ))
            logger.info(f"피드백 영구 저장 완료: ID={record.feedback_id}, Cat={record.category}, Sym={record.symbol}")
            return True
        except Exception as e:
            logger.error(f"Failed to save feedback ({record.feedback_id}): {e}")
            return False

    def get_feedback(self, feedback_id: str) -> Optional[FeedbackRecord]:
        try:
            with self._get_conn() as conn:
                cur = conn.execute("SELECT * FROM telegram_feedbacks WHERE feedback_id = ?", (feedback_id,))
                row = cur.fetchone()
                if not row:
                    return None
                return self._row_to_record(row)
        except Exception as e:
            logger.warning(f"Error reading feedback {feedback_id}: {e}")
            return None

    def get_feedbacks_by_date(self, business_date: str) -> List[FeedbackRecord]:
        """특정 거래일의 피드백만 조회 (과거 데이터 혼입 방지)"""
        results = []
        try:
            with self._get_conn() as conn:
                cur = conn.execute(
                    "SELECT * FROM telegram_feedbacks WHERE business_date = ? ORDER BY received_at ASC",
                    (business_date,)
                )
                for row in cur.fetchall():
                    results.append(self._row_to_record(row))
        except Exception as e:
            logger.warning(f"Error querying feedbacks for date {business_date}: {e}")
        return results

    def get_next_sequence(self, business_date: str) -> int:
        """당일 피드백 일련번호 산출 (FB-YYYYMMDD-XXXXX)"""
        try:
            with self._get_conn() as conn:
                cur = conn.execute(
                    "SELECT COUNT(*) FROM telegram_feedbacks WHERE business_date = ?",
                    (business_date,)
                )
                cnt = cur.fetchone()[0]
                return cnt + 1
        except Exception:
            return 1

    def _row_to_record(self, row: sqlite3.Row) -> FeedbackRecord:
        meta = {}
        if row["link_metadata"]:
            try:
                meta = json.loads(row["link_metadata"])
            except Exception:
                pass
        return FeedbackRecord(
            feedback_id=row["feedback_id"],
            received_at=row["received_at"],
            business_date=row["business_date"],
            user_text=row["user_text"],
            symbol=row["symbol"],
            strategy=row["strategy"],
            category=row["category"],
            sentiment=row["sentiment"],
            severity=row["severity"],
            source=row["source"],
            status=row["status"],
            linked_trade_id=row["linked_trade_id"],
            linked_event_id=row["linked_event_id"],
            link_metadata=meta,
            candidate_stage=row["candidate_stage"] or CandidateStage.NONE.value
        )


# =============================================================================
# 6. EOD Learning 연결 및 전략 불변성 가드 (Requirement 5, 6, 7, 8)
# =============================================================================

def format_feedback_confirmation(record: FeedbackRecord) -> str:
    """
    Requirement 7: Feedback Confirmation 응답 텍스트 생성
    저장만 성공했음을 명확히 안내하고 실제 전략 변경이 완료됐다고 표현하지 않음
    """
    sym_display = record.symbol if record.symbol else "None"
    lines = [
        "✅ 피드백 저장 완료",
        "",
        f"Feedback ID: {record.feedback_id}",
        f"Category: {record.category}",
        f"Symbol: {sym_display}",
        "Status: STORED",
        "",
        "다음 EOD Learning 분석에 반영됩니다."
    ]
    return "\n".join(lines)


def format_zero_trade_day_message() -> str:
    """
    Requirement 1: 오늘 신규 이벤트가 없는 경우 안내 메시지
    이전 거래일 데이터를 절대로 재전송하지 않음
    """
    return (
        "📊 <b>오늘 거래 피드백</b>\n\n"
        "신규 거래: 0건\n"
        "신규 청산: 0건\n"
        "신규 주요 이슈: 0건\n\n"
        "금일 신규 거래/피드백이 없어 이전 거래일 데이터는 재전송하지 않습니다."
    )


def link_feedback_to_eod_data(
    record: FeedbackRecord,
    trade_db_path: str = "data/trade_history_v7.db",
    operational_db_path: str = "data/operational_v16.db"
) -> FeedbackRecord:
    """
    Requirement 6: 사용자 피드백을 당일 실제 거래 결과, NO_TRADE 결과,
    의사결정 추적 및 실행 텔레메트리와 결합
    """
    b_date = record.business_date

    # 1. 실제 체결/청산 내역(trades)과의 결합 시도
    if record.symbol and os.path.exists(trade_db_path):
        try:
            with sqlite3.connect(trade_db_path, timeout=5.0) as conn:
                conn.row_factory = sqlite3.Row
                cur = conn.execute("""
                    SELECT trade_id, symbol, strategy, entry_time, exit_time,
                           entry_price, exit_price, pnl, return_pct, exit_reason
                    FROM trades
                    WHERE (symbol = ? OR symbol_name = ?)
                      AND date(exit_time) = date(?)
                    ORDER BY exit_time DESC LIMIT 1
                """, (record.symbol, record.symbol, b_date))
                t_row = cur.fetchone()
                if t_row:
                    record.linked_trade_id = str(t_row["trade_id"])
                    record.status = FeedbackStatus.LINKED_TO_TRADES.value
                    record.link_metadata = {
                        "trade_id": str(t_row["trade_id"]),
                        "strategy": t_row["strategy"],
                        "entry_time": t_row["entry_time"],
                        "exit_time": t_row["exit_time"],
                        "entry_price": float(t_row["entry_price"]),
                        "exit_price": float(t_row["exit_price"]),
                        "final_pnl": float(t_row["pnl"]),
                        "return_pct": float(t_row["return_pct"]),
                        "exit_reason": t_row["exit_reason"],
                        "slippage": 0.0005,  # 0.05% 추정
                        "trade_r": round(float(t_row["return_pct"]) / 1.5, 2)
                    }
                    logger.info(f"[{record.feedback_id}] 피드백 -> 거래({record.linked_trade_id}) 연결 성공")
                    return record
        except Exception as e:
            logger.warning(f"거래 DB 연결 조회 실패: {e}")

    # 2. NO_TRADE 차단 내역과의 결합 시도
    if record.symbol and os.path.exists(operational_db_path):
        try:
            with sqlite3.connect(operational_db_path, timeout=5.0) as conn:
                conn.row_factory = sqlite3.Row
                cur = conn.execute("""
                    SELECT record_id, iem_cd, name, timestamp, rule_score, ml_prob, expected_net_r,
                           primary_reason, strategy_id
                    FROM no_trade_records
                    WHERE iem_cd = ? AND date(timestamp) = date(?)
                    ORDER BY timestamp DESC LIMIT 1
                """, (record.symbol, b_date))
                nt_row = cur.fetchone()
                if nt_row:
                    record.linked_event_id = f"NO_TRADE_{nt_row['record_id']}"
                    record.status = FeedbackStatus.LINKED_TO_NO_TRADE.value
                    record.link_metadata = {
                        "no_trade_record_id": nt_row["record_id"],
                        "strategy": nt_row["strategy_id"],
                        "decision_time": nt_row["timestamp"],
                        "rule_score": nt_row["rule_score"],
                        "ml_prob": nt_row["ml_prob"],
                        "expected_net_r": nt_row["expected_net_r"],
                        "rejection_reason": nt_row["primary_reason"]
                    }
                    logger.info(f"[{record.feedback_id}] 피드백 -> NO_TRADE({record.linked_event_id}) 연결 성공")
                    return record
        except Exception as e:
            logger.warning(f"NO_TRADE DB 연결 조회 실패: {e}")

    # 3. 직접 연결 대상이 없는 일반 의견
    record.status = FeedbackStatus.REVIEWED.value
    return record


def evaluate_action_candidate(
    record: FeedbackRecord,
    backtest_passed: bool = False,
    walkforward_passed: bool = False,
    shadow_passed: bool = False
) -> CandidateStage:
    """
    Requirement 8: 피드백 반영 상태 검증 파이프라인
    PROPOSED CHANGE -> BACKTEST -> WALK-FORWARD -> SHADOW -> PROMOTION
    검증 실패 시 절대로 전략에 반영하지 않고 REJECTED 처리
    """
    if record.status not in (FeedbackStatus.ACTION_CANDIDATE.value, FeedbackStatus.LINKED_TO_TRADES.value, FeedbackStatus.LINKED_TO_NO_TRADE.value):
        record.candidate_stage = CandidateStage.NONE.value
        return CandidateStage.NONE

    # 1단계: 제안 채택
    stage = CandidateStage.PROPOSED_CHANGE

    # 2단계: 백테스트 검증
    if not backtest_passed:
        record.candidate_stage = CandidateStage.REJECTED.value
        return CandidateStage.REJECTED
    stage = CandidateStage.BACKTEST

    # 3단계: 워크포워드 검증
    if not walkforward_passed:
        record.candidate_stage = CandidateStage.REJECTED.value
        return CandidateStage.REJECTED
    stage = CandidateStage.WALK_FORWARD

    # 4단계: 섀도우 검증
    if not shadow_passed:
        record.candidate_stage = CandidateStage.REJECTED.value
        return CandidateStage.REJECTED
    stage = CandidateStage.SHADOW

    # 최종 승격
    record.candidate_stage = CandidateStage.PROMOTION.value
    return CandidateStage.PROMOTION


# =============================================================================
# 7. 통합 파사드 (Facade)
# =============================================================================

class TelegramFeedbackManager:
    """
    텔레그램 피드백 수신, 분류, 멱등성 검사, DB 저장 및 EOD 연동 통합 파사드
    """
    def __init__(self, db_path: str = "data/operational_v16.db"):
        self.db_path = db_path
        self.idempotency = TelegramIdempotencyManager(db_path=db_path)
        self.store = TelegramFeedbackStore(db_path=db_path)

    def process_incoming_feedback(
        self,
        user_text: str,
        now: Optional[datetime] = None
    ) -> Tuple[Optional[FeedbackRecord], Optional[str]]:
        """
        사용자 자연어 텍스트 수신 -> 메시지 분류 검증 -> 파싱/분류 -> 저장 -> 확인 메시지 반환
        * 중요:
          1) 거래 이벤트(BUY, SELL, FILL 등)는 절대로 feedback_id를 생성하지 않으며 저장하지 않음 (None, None 반환)
          2) 사용자 질문(QUERY)은 Feedback이 아니므로 저장하지 않음 (None, None 반환)
          3) 오직 사용자 자연어 피드백(USER_FEEDBACK)인 경우에만 feedback_id 생성 및 영구 저장 (Requirement 1, 2, 5, 7)
        """
        if not user_text or not user_text.strip():
            return None, ""

        # 1. 메시지 분류기 검증 (Requirement 1, 2, 4, 5, 6, 7)
        classification = MessageClassifier.classify(user_text)
        if classification != IncomingMessageType.USER_FEEDBACK:
            logger.info(
                f"[TG_FEEDBACK] 비-피드백 메시지 수신 (Classification={classification.value}): "
                f"feedback_id 생성 및 저장 차단 (text='{user_text[:50]}')"
            )
            return None, ""

        now = now or datetime.now()
        b_date = now.strftime("%Y-%m-%d")
        now_str = now.strftime("%Y-%m-%d %H:%M:%S")

        seq = self.store.get_next_sequence(b_date)
        date_compact = now.strftime("%Y%m%d")
        feedback_id = f"FB-{date_compact}-{seq:05d}"

        sym = extract_symbol(user_text)
        strat = extract_strategy(user_text)
        cat = classify_category(user_text)
        sent = classify_sentiment(user_text)
        sev = classify_severity(user_text)

        rec = FeedbackRecord(
            feedback_id=feedback_id,
            received_at=now_str,
            business_date=b_date,
            user_text=user_text,
            symbol=sym,
            strategy=strat,
            category=cat.value,
            sentiment=sent,
            severity=sev,
            source="TELEGRAM",
            status=FeedbackStatus.NEW_TELEGRAM_FEEDBACK.value
        )

        self.store.save_feedback(rec)
        conf_msg = format_feedback_confirmation(rec)
        return rec, conf_msg

    def get_user_feedback_data(self, business_date: str) -> List[Dict[str, Any]]:
        """
        USER_FEEDBACK_DATA: Telegram에서 사용자가 직접 입력한 피드백 데이터셋 (Requirement 8)
        거래 이벤트가 절대 혼입되지 않도록 source='TELEGRAM' 및 유효 feedback_id 검증
        """
        records = self.store.get_feedbacks_by_date(business_date)
        feedback_dataset = []
        for r in records:
            if r.source == "TELEGRAM" and r.feedback_id and r.feedback_id.startswith("FB-"):
                feedback_dataset.append({
                    "dataset_type": "USER_FEEDBACK_DATA",
                    "feedback_id": r.feedback_id,
                    "business_date": r.business_date,
                    "received_at": r.received_at,
                    "user_text": r.user_text,
                    "category": r.category,
                    "sentiment": r.sentiment,
                    "severity": r.severity,
                    "symbol": r.symbol,
                    "strategy": r.strategy,
                    "linked_trade_id": r.linked_trade_id,
                    "linked_event_id": r.linked_event_id,
                    "link_metadata": r.link_metadata,
                    "candidate_stage": r.candidate_stage
                })
        return feedback_dataset

    def get_trade_learning_data(self, business_date: str, trade_db_path: str = "data/trade_history_v7.db") -> List[Dict[str, Any]]:
        """
        TRADE_LEARNING_DATA: 시스템 거래 체결/결과/결정 추적 데이터셋 (Requirement 8)
        feedback_id는 반드시 NULL(None)이며 사용자 피드백과 100% 분리됨
        """
        trades_data = []
        if os.path.exists(trade_db_path):
            try:
                with sqlite3.connect(trade_db_path, timeout=5.0) as conn:
                    conn.row_factory = sqlite3.Row
                    cur = conn.execute("""
                        SELECT trade_id, symbol, symbol_name, strategy, entry_time, exit_time,
                               entry_price, exit_price, pnl, return_pct, exit_reason
                        FROM trades
                        WHERE date(exit_time) = date(?) OR date(entry_time) = date(?)
                        ORDER BY trade_id ASC
                    """, (business_date, business_date))
                    for row in cur.fetchall():
                        trades_data.append({
                            "dataset_type": "TRADE_LEARNING_DATA",
                            "trade_id": str(row["trade_id"]),
                            "feedback_id": None,  # Trade event: feedback_id is strictly NULL
                            "symbol": row["symbol"],
                            "name": row["symbol_name"],
                            "strategy": row["strategy"],
                            "entry_time": row["entry_time"],
                            "exit_time": row["exit_time"],
                            "entry_price": row["entry_price"],
                            "exit_price": row["exit_price"],
                            "pnl": row["pnl"],
                            "return_pct": row["return_pct"],
                            "exit_reason": row["exit_reason"]
                        })
            except Exception as e:
                logger.warning(f"Error fetching trade learning data: {e}")
        return trades_data
