# -*- coding: utf-8 -*-
"""
[AFTER-HOURS GOVERNANCE ENGINE] core/after_hours_manager.py
=============================================================================
장후 시간외 급등 감지, 익일 관찰 후보 등록, 정규장 재검증 및 세션 태깅 거버넌스 엔진

[핵심 정책 원칙]
1. 시간외 급등(AFTER_HOURS_SIGNAL)은 오직 NEXT_SESSION_WATCHLIST 후보로만 등록한다.
2. 시간외 급등(+5%, +10%, +15% 등) 자체로 즉시 매수하거나 신규 오버나잇 포지션을 생성하는 것을 전면 차단한다.
3. 시간외 급등 데이터는 익일 정규장을 위한 '사전 특징값(Prior Feature / Context)'으로만 유지한다.
4. 익일 정규장 개장 후 실시간 체결가, 거래량/RVOL, 호가스프레드, VWAP, 정규장 전략 Setup, ML/Edge/Risk를 전면 재검증한다.
   - 갭상승 과열/거래량 고갈(Overextended/Exhaustion Gap) -> NO_TRADE 차단
   - 적정 갭상승 + 거래량 유입 + 눌림목 VWAP 지지 + Setup 충족 -> 정상 BUY 승인
5. 보유 포지션 관리(장후 가격 모니터링/익일 시초가 대응)와 신규 진입(시간외 신규 진입 전면 금지)을 철저히 분리한다.
6. 시간외 급등 종목의 사후 성과(갭, 5분/15분/30분/종가 수익률, MFE, MAE)를 구간별(+3~5%, +5~10%, +10~15%, +15% 이상) 통계로 기록한다.
7. 모든 거래에 signal_session과 entry_session 태깅을 부여하며,
   AFTER_HOURS_SIGNAL / AFTER_HOURS_ENTRY 조합 발생 시 즉시 치명적 경고를 출력하고 발주를 차단한다.
=============================================================================
"""

import os
import sqlite3
import logging
from contextlib import contextmanager
from datetime import datetime, time, date
from dataclasses import dataclass, field, asdict
from enum import Enum
from typing import Dict, List, Optional, Tuple, Any

from core.models import TimeHorizon

logger = logging.getLogger("AfterHoursManager")
if not logger.handlers:
    handler = logging.StreamHandler()
    handler.setFormatter(logging.Formatter("[%(asctime)s] %(levelname)s: [AFTER_HOURS] %(message)s"))
    logger.addHandler(handler)
    logger.setLevel(logging.INFO)


class MarketSession(Enum):
    PRE_MARKET = "PRE_MARKET"        # 08:00 ~ 09:00 (장시작 전 동시호가/시간외종가)
    REGULAR = "REGULAR"              # 09:00 ~ 15:30 (정규장)
    AFTER_HOURS = "AFTER_HOURS"      # 15:30 ~ 18:00 (장후 시간외 종가 & 시간외 단일가)
    CLOSED = "CLOSED"                # 18:00 ~ 08:00 (야간/주말 장 마감)


class MarketSessionManager:
    """KRX 시장 세션 식별 및 세션별 주문 발주 허용 여부 제어기"""

    @staticmethod
    def get_market_session(dt: Optional[datetime] = None) -> MarketSession:
        now = dt or datetime.now()
        t = now.time()
        # 주말(토/일)은 CLOSED
        if now.weekday() >= 5:
            return MarketSession.CLOSED

        if time(8, 0, 0) <= t < time(9, 0, 0):
            return MarketSession.PRE_MARKET
        elif time(9, 0, 0) <= t < time(15, 30, 0):
            return MarketSession.REGULAR
        elif time(15, 30, 0) <= t < time(18, 0, 0):
            return MarketSession.AFTER_HOURS
        else:
            return MarketSession.CLOSED

    @staticmethod
    def is_order_entry_allowed(
        time_horizon: TimeHorizon = TimeHorizon.INTRADAY,
        dt: Optional[datetime] = None,
        is_buy: bool = True
    ) -> Tuple[bool, str]:
        """
        신규 주문 발주 허용 여부 판정 (매도는 비상 청산 허용 가능하나 신규 매수는 정규장만 허용)
        """
        now = dt or datetime.now()
        session = MarketSessionManager.get_market_session(now)

        if not is_buy:
            # 매도/손절 청산의 경우 정규장(09:00~15:30) 내에서만 브로커 전송 가능
            if session != MarketSession.REGULAR:
                return False, f"BLOCK_NON_REGULAR_SELL: 정규장 마감 후에는 브로커 매도 주문 전송 불가 ({session.value})"
            return True, "OK"

        # 신규 매수 (BUY) 주문 검사
        if session != MarketSession.REGULAR:
            return False, f"BLOCK_NON_REGULAR_SESSION: 정규장(09:00~15:30) 외 {session.value} 세션에서는 신규 매수 주문 발주가 전면 금지됩니다."

        # 단타(INTRADAY) 포지션은 15:00:00 이후 신규 진입 금지 (15:20 강제 청산 원칙 보호)
        if time_horizon == TimeHorizon.INTRADAY and now.time() >= time(15, 0, 0):
            return False, "BLOCK_INTRADAY_LATE_ENTRY: 15:00 이후 신규 단타 진입 금지 (15:20 일괄 강제청산 원칙 보호)"

        return True, "OK"

    @staticmethod
    def is_overnight_position_allowed(after_hours_signal: bool, dt: Optional[datetime] = None) -> Tuple[bool, str]:
        """
        시간외 급등으로 인한 신규 오버나잇 포지션 생성 차단 가드
        """
        if after_hours_signal:
            return False, "BLOCK_OVERNIGHT_FROM_AFTER_HOURS: 시간외 급등 신호만으로 신규 오버나잇 포지션을 생성하는 것은 엄격히 금지됩니다."
        return True, "OK"


class WatchlistStatus(str, Enum):
    ACTIVE = "ACTIVE"
    CONSUMED = "CONSUMED"
    REJECTED = "REJECTED"
    EXPIRED = "EXPIRED"
    COMPLETED = "COMPLETED"


@dataclass
class AfterHoursCandidate:
    """시간외 급등 감지 종목 정보"""
    iem_cd: str
    name: str
    detected_at: datetime
    regular_close: float
    after_hours_price: float
    after_hours_return: float       # % 단위 (예: +6.5)
    after_hours_volume: int
    after_hours_turnover: float
    after_hours_regime: str
    spike_bracket: str              # '+3%~+5%', '+5%~+10%', '+10%~+15%', '+15% 이상'
    next_session_watchlist: bool = True
    status: str = WatchlistStatus.ACTIVE.value       # ACTIVE, CONSUMED, REJECTED, EXPIRED, COMPLETED
    next_day_revalidated: bool = False
    next_day_decision: str = "PENDING"
    next_day_reason: str = ""


@dataclass
class AfterHoursOutcomeRecord:
    """시간외 급등 종목의 사후 성과 집계 레코드"""
    record_id: str
    iem_cd: str
    symbol_name: str
    event_date: str                 # YYYY-MM-DD
    after_hours_return: float
    spike_bracket: str
    next_day_open: float = 0.0
    next_day_gap_return: float = 0.0
    open_to_5m_return: float = 0.0
    open_to_15m_return: float = 0.0
    open_to_30m_return: float = 0.0
    open_to_close_return: float = 0.0
    max_favorable_excursion: float = 0.0
    max_adverse_excursion: float = 0.0
    was_traded: bool = False
    entry_price: float = 0.0
    exit_price: float = 0.0
    final_pnl: float = 0.0
    created_at: str = field(default_factory=lambda: datetime.now().isoformat())


class AfterHoursManager:
    """시간외 급등 관리, 익일 관찰 후보 등록, 정규장 재검증 및 태깅 거버넌스 관리자"""

    def __init__(self, db_path: str = "data/operational_v16.db", telegram_notifier: Any = None):
        self.db_path = db_path
        self.telegram_notifier = telegram_notifier
        self.watchlist: Dict[str, AfterHoursCandidate] = {}
        if os.path.dirname(self.db_path):
            os.makedirs(os.path.dirname(self.db_path), exist_ok=True)
        self._init_db()
        self._load_active_watchlist()

    @contextmanager
    def _get_conn(self):
        conn = sqlite3.connect(self.db_path, timeout=60.0)
        conn.row_factory = sqlite3.Row
        try:
            yield conn
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    def _load_active_watchlist(self):
        """DB에서 현재 ACTIVE 상태인 워치리스트 후보들을 메모리 캐시(self.watchlist)로 로드"""
        try:
            with self._get_conn() as conn:
                rows = conn.execute("""
                    SELECT * FROM after_hours_candidates 
                    WHERE status IN ('ACTIVE', 'WATCHLIST') AND next_session_watchlist = 1
                """).fetchall()
                for r in rows:
                    cand = self._row_to_candidate(r)
                    if cand:
                        self.watchlist[cand.iem_cd] = cand
        except Exception as e:
            logger.error(f"워치리스트 DB 로드 실패: {e}")

    def _row_to_candidate(self, row: Any) -> Optional[AfterHoursCandidate]:
        if not row:
            return None
        dt_val = row["detected_at"]
        if isinstance(dt_val, str):
            try:
                detected_dt = datetime.fromisoformat(dt_val)
            except Exception:
                detected_dt = datetime.now()
        else:
            detected_dt = dt_val or datetime.now()

        return AfterHoursCandidate(
            iem_cd=row["iem_cd"],
            name=row["name"],
            detected_at=detected_dt,
            regular_close=float(row["regular_close"]),
            after_hours_price=float(row["after_hours_price"]),
            after_hours_return=float(row["after_hours_return"]),
            after_hours_volume=int(row["after_hours_volume"]),
            after_hours_turnover=float(row["after_hours_turnover"]),
            after_hours_regime=str(row["after_hours_regime"]),
            spike_bracket=str(row["spike_bracket"]),
            next_session_watchlist=bool(row["next_session_watchlist"]),
            status=str(row["status"]),
            next_day_revalidated=bool(row["next_day_revalidated"]),
            next_day_decision=str(row["next_day_decision"]),
            next_day_reason=str(row["next_day_reason"] or "")
        )

    def _get_candidate_from_db(self, iem_cd: str) -> Optional[AfterHoursCandidate]:
        try:
            with self._get_conn() as conn:
                row = conn.execute("SELECT * FROM after_hours_candidates WHERE iem_cd = ?", (iem_cd,)).fetchone()
                return self._row_to_candidate(row)
        except Exception as e:
            logger.error(f"DB 후보 조회 실패 ({iem_cd}): {e}")
            return None

    def _init_db(self):
        with self._get_conn() as conn:
            conn.execute("PRAGMA journal_mode=WAL;")
            conn.executescript("""
            CREATE TABLE IF NOT EXISTS after_hours_candidates (
                iem_cd TEXT PRIMARY KEY,
                name TEXT,
                detected_at TEXT,
                regular_close REAL,
                after_hours_price REAL,
                after_hours_return REAL,
                after_hours_volume INTEGER,
                after_hours_turnover REAL,
                after_hours_regime TEXT,
                spike_bracket TEXT,
                next_session_watchlist INTEGER,
                status TEXT,
                next_day_revalidated INTEGER,
                next_day_decision TEXT,
                next_day_reason TEXT
            );

            CREATE TABLE IF NOT EXISTS after_hours_outcomes (
                record_id TEXT PRIMARY KEY,
                iem_cd TEXT,
                symbol_name TEXT,
                event_date TEXT,
                after_hours_return REAL,
                spike_bracket TEXT,
                next_day_open REAL,
                next_day_gap_return REAL,
                open_to_5m_return REAL,
                open_to_15m_return REAL,
                open_to_30m_return REAL,
                open_to_close_return REAL,
                max_favorable_excursion REAL,
                max_adverse_excursion REAL,
                was_traded INTEGER,
                entry_price REAL,
                exit_price REAL,
                final_pnl REAL,
                created_at TEXT
            );
            CREATE INDEX IF NOT EXISTS idx_ah_outcomes_date ON after_hours_outcomes(event_date);
            CREATE INDEX IF NOT EXISTS idx_ah_outcomes_bracket ON after_hours_outcomes(spike_bracket);
            """)

    @staticmethod
    def get_spike_bracket(return_pct: float) -> str:
        """상승률 구간 분류 (+3%~+5%, +5%~+10%, +10%~+15%, +15% 이상)"""
        if return_pct < 3.0:
            return "미달 (<+3%)"
        elif return_pct < 5.0:
            return "+3%~+5%"
        elif return_pct < 10.0:
            return "+5%~+10%"
        elif return_pct < 15.0:
            return "+10%~+15%"
        else:
            return "+15% 이상"

    def detect_after_hours_spike(
        self,
        iem_cd: str,
        name: str,
        regular_close: float,
        current_price: float,
        volume: int = 0,
        turnover: float = 0.0,
        regime: str = "NEUTRAL",
        dt: Optional[datetime] = None,
        min_spike_pct: float = 3.0
    ) -> Optional[AfterHoursCandidate]:
        """
        시간외 거래에서 급등 발생 시 감지하여 NEXT_SESSION_WATCHLIST에 등록
        ※ 절대로 즉시 매수 주문을 발주하지 않음!
        """
        now = dt or datetime.now()
        session = MarketSessionManager.get_market_session(now)

        # 정규장 중인 경우 시간외 급등이 아님
        if session == MarketSession.REGULAR and dt is None:
            return None

        # 1. 정규장 종가 및 현재가 유효성 검증 (0 이하이거나 유효하지 않으면 차단)
        if regular_close <= 0 or current_price <= 0:
            return None

        today_date = now.date() if isinstance(now, datetime) else datetime.now().date()

        # 2. 중복 등록 방지 가드:
        # 동일 거래일(trade date), 동일 종목이 이미 ACTIVE/WATCHLIST 상태인 경우
        # 스캐너의 3초 주기 반복 호출에 의한 DB 쓰기, 로그 폭풍, 알림 전송을 전면 차단
        existing = self.watchlist.get(iem_cd)
        if not existing:
            existing = self._get_candidate_from_db(iem_cd)
            if existing:
                self.watchlist[iem_cd] = existing

        if existing:
            cand_dt = existing.detected_at if isinstance(existing.detected_at, datetime) else datetime.fromisoformat(str(existing.detected_at))
            if cand_dt.date() == today_date and existing.status in (WatchlistStatus.ACTIVE.value, "WATCHLIST"):
                logger.debug(f"[{iem_cd}] 이미 당일({today_date}) ACTIVE 등록됨 -> 중복 감지 무시")
                return existing

        # 3. 애프터마켓 상승률 계산: (현재 시간외 체결가 - 당일 정규장 종가) / 당일 정규장 종가 * 100
        ret_pct = ((current_price - regular_close) / regular_close) * 100.0
        if ret_pct < min_spike_pct:
            return None

        bracket = self.get_spike_bracket(ret_pct)
        cand = AfterHoursCandidate(
            iem_cd=iem_cd,
            name=name,
            detected_at=now,
            regular_close=regular_close,
            after_hours_price=current_price,
            after_hours_return=round(ret_pct, 2),
            after_hours_volume=volume,
            after_hours_turnover=turnover,
            after_hours_regime=regime,
            spike_bracket=bracket,
            next_session_watchlist=True,
            status=WatchlistStatus.ACTIVE.value
        )
        self.watchlist[iem_cd] = cand
        self._persist_candidate(cand)

        logger.info(
            f"📌 [시간외 급등 감지] {name}({iem_cd}) {current_price:,}원 (+{ret_pct:.2f}%, {bracket}) "
            f"-> 익일 관찰 후보(NEXT_SESSION_WATCHLIST) 등록 완료 (즉시 매수 금지)"
        )

        # [TELEGRAM REFACTOR] 종목별 실시간 개별 알림 제거 (알림 폭풍 방지)
        # 전체 종목 데이터는 내부 DB(after_hours_candidates)에 온전히 저장되며,
        # 시간외 거래 종료 시 1일 1회 요약 메시지(AFTER_HOURS_DAILY_SUMMARY)로만 전송됩니다.

        return cand

    def _persist_candidate(self, cand: AfterHoursCandidate):
        with self._get_conn() as conn:
            conn.execute("""
            INSERT OR REPLACE INTO after_hours_candidates (
                iem_cd, name, detected_at, regular_close, after_hours_price,
                after_hours_return, after_hours_volume, after_hours_turnover,
                after_hours_regime, spike_bracket, next_session_watchlist,
                status, next_day_revalidated, next_day_decision, next_day_reason
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (
                cand.iem_cd, cand.name, cand.detected_at.isoformat(),
                cand.regular_close, cand.after_hours_price, cand.after_hours_return,
                cand.after_hours_volume, cand.after_hours_turnover,
                cand.after_hours_regime, cand.spike_bracket,
                1 if cand.next_session_watchlist else 0,
                cand.status,
                1 if cand.next_day_revalidated else 0,
                cand.next_day_decision,
                cand.next_day_reason
            ))

    def mark_consumed(self, iem_cd: str, reason: str = "ORDER_FILLED") -> bool:
        """익일 정규장 매수 체결 완료 -> ACTIVE에서 CONSUMED로 상태 전이 및 워치리스트 해제"""
        cand = self.watchlist.get(iem_cd) or self._get_candidate_from_db(iem_cd)
        if cand:
            cand.status = WatchlistStatus.CONSUMED.value
            cand.next_session_watchlist = False
            cand.next_day_revalidated = True
            cand.next_day_decision = "BUY_APPROVED"
            cand.next_day_reason = reason
            self.watchlist[iem_cd] = cand
            self._persist_candidate(cand)
            logger.info(f"✅ [{iem_cd}] 워치리스트 CONSUMED 전이 완료: {reason}")
            return True
        return False

    def mark_rejected(self, iem_cd: str, reason: str = "REVALIDATION_REJECTED") -> bool:
        """익일 정규장 재검증 탈락 -> ACTIVE에서 REJECTED로 상태 전이 및 워치리스트 해제"""
        cand = self.watchlist.get(iem_cd) or self._get_candidate_from_db(iem_cd)
        if cand:
            cand.status = WatchlistStatus.REJECTED.value
            cand.next_session_watchlist = False
            cand.next_day_revalidated = True
            cand.next_day_decision = "NO_TRADE"
            cand.next_day_reason = reason
            self.watchlist[iem_cd] = cand
            self._persist_candidate(cand)
            logger.info(f"🛑 [{iem_cd}] 워치리스트 REJECTED 전이 완료: {reason}")
            return True
        return False

    def expire_stale_watchlist(self, current_date: Optional[date] = None) -> int:
        """유효기간이 지난 이전 거래일의 ACTIVE 워치리스트를 EXPIRED로 일괄 전환"""
        cur_d = current_date or datetime.now().date()
        expired_count = 0
        with self._get_conn() as conn:
            rows = conn.execute("""
                SELECT iem_cd, detected_at FROM after_hours_candidates 
                WHERE status IN ('ACTIVE', 'WATCHLIST') AND next_session_watchlist = 1
            """).fetchall()
            for r in rows:
                dt_str = r["detected_at"]
                try:
                    cand_d = datetime.fromisoformat(dt_str).date()
                except Exception:
                    cand_d = cur_d
                if cand_d < cur_d:
                    conn.execute("""
                        UPDATE after_hours_candidates 
                        SET status = ?, next_session_watchlist = 0, next_day_decision = 'EXPIRED', next_day_reason = 'DATE_EXPIRED'
                        WHERE iem_cd = ?
                    """, (WatchlistStatus.EXPIRED.value, r["iem_cd"]))
                    expired_count += 1
                    if r["iem_cd"] in self.watchlist:
                        cand = self.watchlist[r["iem_cd"]]
                        cand.status = WatchlistStatus.EXPIRED.value
                        cand.next_session_watchlist = False
                        cand.next_day_decision = "EXPIRED"
                        cand.next_day_reason = "DATE_EXPIRED"

        if expired_count > 0:
            logger.info(f"⌛ 이전 거래일 만료 워치리스트 {expired_count}건 EXPIRED 처리 완료")
        return expired_count

    def build_daily_summary(self, trade_date: Optional[str] = None) -> Dict[str, Any]:
        """
        [Section 3 & 4] 시간외 거래 종료 후 해당 거래일 AFTER_HOURS_SPIKE 전체 데이터 집계
        - 기준 거래일
        - 시간외 급등 감지 종목 수
        - 신규 NEXT_SESSION_WATCHLIST 등록 수
        - 기존 Watchlist와 중복된 종목 수
        - 유효 후보 수
        - 거래량 부족 종목 수 (< 1,000주)
        - 거래대금 부족 종목 수 (< 10,000,000원)
        - 가격 왜곡/체결 품질 저하 의심 종목 수 (< 100주 또는 < 1,000,000원)
        - 최고 / 평균 / 중간값 상승률
        - 총 거래대금
        - 주요 후보 Top 3~5
        """
        target_date = trade_date or datetime.now().strftime("%Y-%m-%d")
        with self._get_conn() as conn:
            cur = conn.execute(
                "SELECT * FROM after_hours_candidates WHERE detected_at LIKE ? ORDER BY after_hours_return DESC",
                (f"{target_date}%",)
            )
            rows = [dict(r) for r in cur.fetchall()]

        detected_count = len(rows)
        if detected_count == 0:
            return {
                "trade_date": target_date,
                "detected_count": 0,
                "next_session_watchlist_count": 0,
                "duplicate_count": getattr(self, "daily_duplicate_count", 0),
                "valid_candidates_count": 0,
                "volume_deficient_count": 0,
                "turnover_deficient_count": 0,
                "price_distortion_count": 0,
                "not_qualified_count": 0,
                "max_return": 0.0,
                "avg_return": 0.0,
                "median_return": 0.0,
                "total_turnover": 0.0,
                "top_candidates": []
            }

        watchlist_cands = [r for r in rows if r.get("next_session_watchlist") == 1 or r.get("status") in (WatchlistStatus.ACTIVE.value, "WATCHLIST")]
        volume_deficient = [r for r in rows if int(r.get("after_hours_volume") or 0) < 1000]
        turnover_deficient = [r for r in rows if float(r.get("after_hours_turnover") or 0) < 10000000]
        price_distortion = [r for r in rows if int(r.get("after_hours_volume") or 0) < 100 or float(r.get("after_hours_turnover") or 0) < 1000000]
        valid_candidates = [r for r in rows if int(r.get("after_hours_volume") or 0) >= 1000 and float(r.get("after_hours_turnover") or 0) >= 10000000]

        returns = [float(r.get("after_hours_return") or 0.0) for r in rows]
        returns.sort()
        max_return = returns[-1] if returns else 0.0
        avg_return = round(sum(returns) / len(returns), 2) if returns else 0.0
        mid_idx = len(returns) // 2
        median_return = round(returns[mid_idx] if len(returns) % 2 != 0 else (returns[mid_idx - 1] + returns[mid_idx]) / 2, 2) if returns else 0.0
        total_turnover = sum(float(r.get("after_hours_turnover") or 0.0) for r in rows)

        top_candidates = []
        for r in rows[:5]:
            top_candidates.append({
                "iem_cd": r.get("iem_cd"),
                "name": r.get("name") or r.get("iem_cd"),
                "return_pct": float(r.get("after_hours_return") or 0.0),
                "volume": int(r.get("after_hours_volume") or 0),
                "turnover": float(r.get("after_hours_turnover") or 0.0)
            })

        return {
            "trade_date": target_date,
            "detected_count": detected_count,
            "next_session_watchlist_count": len(watchlist_cands),
            "duplicate_count": getattr(self, "daily_duplicate_count", 0),
            "valid_candidates_count": len(valid_candidates),
            "volume_deficient_count": len(volume_deficient),
            "turnover_deficient_count": len(turnover_deficient),
            "price_distortion_count": len(price_distortion),
            "not_qualified_count": detected_count - len(valid_candidates),
            "max_return": max_return,
            "avg_return": avg_return,
            "median_return": median_return,
            "total_turnover": total_turnover,
            "top_candidates": top_candidates
        }

    def send_after_hours_daily_summary(self, trade_date: Optional[str] = None) -> bool:
        """
        [Section 4, 6, 7] 시간외 거래 종료 후 1일 1회 요약 Telegram 메시지 발송 (Idempotent)
        - 고유 키: trade_date + _AFTER_HOURS_DAILY_SUMMARY
        - SENT 이후 프로세스 재시작 시에도 중복 재발송 차단
        """
        target_date = trade_date or datetime.now().strftime("%Y-%m-%d")
        idempotency_key = f"{target_date}_AFTER_HOURS_DAILY_SUMMARY"

        if self.telegram_notifier and hasattr(self.telegram_notifier, "idempotency"):
            if self.telegram_notifier.idempotency.is_sent(idempotency_key):
                logger.debug(f"[시간외 데일리 서머리] 이미 발송 완료됨 ({idempotency_key}) -> 중복 발송 스킵")
                return False

        summary = self.build_daily_summary(target_date)
        if summary["detected_count"] == 0:
            logger.info(f"[{target_date}] 시간외 급등 감지 종목 없음 -> 서머리 발송 스킵")
            return False

        lines = [
            "📊 <b>[시간외 급등 일일 분석]</b>",
            "",
            f"거래일: {summary['trade_date']}",
            "분석 시간: 15:30~18:00",
            "",
            f"• 급등 감지: {summary['detected_count']}종목",
            f"• 다음장 재검증 후보: {summary['next_session_watchlist_count']}종목",
            f"• 거래량 부족: {summary['volume_deficient_count']}종목",
            f"• 거래대금 부족: {summary['turnover_deficient_count']}종목",
            f"• 가격왜곡 의심: {summary['price_distortion_count']}종목",
            f"• 최고 상승률: +{summary['max_return']:.1f}%",
            f"• 평균 상승률: +{summary['avg_return']:.1f}%"
        ]

        if summary["top_candidates"]:
            lines.append("")
            lines.append("주요 후보:")
            for c in summary["top_candidates"][:5]:
                lines.append(f"• {c['name']} +{c['return_pct']:.1f}%")

        lines.extend([
            "",
            "→ 상세 종목 데이터는 시스템에 저장",
            "→ 다음 정규장 재검증 예정",
            "→ 시간외 직접 매수 없음"
        ])

        text = "\n".join(lines)

        if self.telegram_notifier:
            try:
                res = self.telegram_notifier.send_message(
                    text=text,
                    message_type="AFTER_HOURS_DAILY_SUMMARY",
                    idempotency_key=idempotency_key,
                    business_date=target_date,
                    event_id=idempotency_key
                )
                logger.info(f"✅ [시간외 데일리 서머리 발송 완료] {idempotency_key} -> {res}")
                return bool(res)
            except Exception as e:
                logger.error(f"시간외 데일리 서머리 발송 실패: {e}")
                return False
        return False

    def get_active_watchlist(self, target_date: Optional[date] = None) -> List[AfterHoursCandidate]:
        """
        현재 활성 상태인 익일 관찰 후보 목록 반환
        - status IN ('ACTIVE', 'WATCHLIST') 및 next_session_watchlist = 1 인 대상만 반환
        """
        with self._get_conn() as conn:
            rows = conn.execute("""
                SELECT * FROM after_hours_candidates 
                WHERE status IN ('ACTIVE', 'WATCHLIST') AND next_session_watchlist = 1
                ORDER BY detected_at DESC
            """).fetchall()
            active_list = []
            for r in rows:
                cand = self._row_to_candidate(r)
                if cand:
                    if target_date and cand.detected_at.date() != target_date:
                        continue
                    active_list.append(cand)
            return active_list

    def get_next_session_features(self, iem_cd: str) -> Dict[str, Any]:
        """
        익일 정규장에서 참조할 시간외 사전 특징값(Prior Feature / Context) 반환
        - 만료(EXPIRED) 상태이거나 비활성인 경우 신호 비활성화 반환
        """
        cand = self.watchlist.get(iem_cd)
        if not cand:
            cand = self._get_candidate_from_db(iem_cd)
            if cand:
                self.watchlist[iem_cd] = cand

        if not cand or cand.status == WatchlistStatus.EXPIRED.value:
            return {
                "AFTER_HOURS_SIGNAL": False,
                "AFTER_HOURS_RETURN": 0.0,
                "AFTER_HOURS_VOLUME": 0,
                "AFTER_HOURS_TURNOVER": 0.0,
                "AFTER_HOURS_REGIME": "NONE",
                "NEXT_SESSION_WATCHLIST": False
            }

        return {
            "AFTER_HOURS_SIGNAL": True,
            "AFTER_HOURS_RETURN": cand.after_hours_return,
            "AFTER_HOURS_VOLUME": cand.after_hours_volume,
            "AFTER_HOURS_TURNOVER": cand.after_hours_turnover,
            "AFTER_HOURS_REGIME": cand.after_hours_regime,
            "NEXT_SESSION_WATCHLIST": cand.next_session_watchlist
        }

    def evaluate_next_session_gap(
        self,
        iem_cd: str,
        open_price: float,
        prev_close: float,
        current_price: float,
        vwap: float,
        rvol: float = 1.0,
        is_selling_pressure: bool = False
    ) -> Tuple[bool, str]:
        """
        시간외 급등 후 정규장 갭 상태에 대한 정밀 조건 검증
        - 예: 시간외 +10% -> 시초가 +12% 갭상승 -> 거래량 급감/매도세 우세 -> NO_TRADE (Overextended / Exhaustion Gap)
        - 예: 시간외 +6% -> 시초가 +4% 갭상승 -> 정규장 시작 후 거래량 유입, 눌림 후 VWAP 지지 -> 통과 (BUY 검토 대상)
        """
        if prev_close <= 0 or open_price <= 0:
            return False, "INVALID_PRICE_DATA: 시초가 또는 전일종가 오류"

        gap_pct = ((open_price - prev_close) / prev_close) * 100.0

        # 1. 극단적 갭상승 과열/소진 갭 (Exhaustion Gap) 차단
        if gap_pct >= 10.0:
            if rvol < 1.2 or is_selling_pressure or current_price < vwap:
                reason = f"OVEREXTENDED_GAP_EXHAUSTION: 시초가 +{gap_pct:.1f}% 극단적 갭상승 후 거래량 고갈 및 매도세 (RVOL={rvol:.1f}, VWAP 하회)"
                logger.info(f"🛑 [{iem_cd}] {reason} -> NO_TRADE 차단")
                return False, reason

        # 2. 갭하락으로 시간외 모멘텀이 즉시 소멸한 경우 차단
        if gap_pct <= -2.0:
            reason = f"GAP_DOWN_MOMENTUM_LOST: 시초가 {gap_pct:.1f}% 갭하락으로 시간외 모멘텀 소멸"
            logger.info(f"🛑 [{iem_cd}] {reason} -> NO_TRADE 차단")
            return False, reason

        # 3. 건전한 갭상승 구간 (+2% ~ +8% 수준)
        # 장 시작 후 실시간 체결가가 VWAP을 지지하고 매도압력이 없는지 검증
        if current_price < vwap * 0.995:
            reason = f"VWAP_SUPPORT_FAILED: 현재가({current_price:,.0f})가 당일 VWAP({vwap:,.0f})을 이탈하여 지지 실패"
            logger.info(f"🛑 [{iem_cd}] {reason} -> NO_TRADE 차단")
            return False, reason

        if is_selling_pressure:
            reason = "EXCESSIVE_SELLING_PRESSURE: 시초 갭상승 후 차익실현 매도세 우세"
            logger.info(f"🛑 [{iem_cd}] {reason} -> NO_TRADE 차단")
            return False, reason

        return True, f"VALID_GAP_HEALTHY: 갭 +{gap_pct:.1f}%, VWAP 지지 확인, 수급 건전"

    def revalidate_in_regular_session(
        self,
        iem_cd: str,
        open_price: float,
        prev_close: float,
        current_price: float,
        vwap: float,
        rvol: float,
        setup_pass: bool,
        setup_name: str,
        ml_approved: bool,
        edge_approved: bool,
        risk_approved: bool,
        is_selling_pressure: bool = False,
        now: Optional[datetime] = None
    ) -> Tuple[bool, str]:
        """
        익일 정규장 개장 후 실시간 데이터로 전면 재검증 수행
        모든 조건 충족 시에만 최종 매수 승인
        """
        now = now or datetime.now()
        cand = self.watchlist.get(iem_cd) or self._get_candidate_from_db(iem_cd)

        # 1. 정규장 세션 확인
        session_allowed, s_reason = MarketSessionManager.is_order_entry_allowed(TimeHorizon.INTRADAY, now, is_buy=True)
        if not session_allowed:
            if cand:
                cand.next_day_revalidated = True
                cand.next_day_decision = "NO_TRADE"
                cand.next_day_reason = s_reason
                cand.status = WatchlistStatus.REJECTED.value
                cand.next_session_watchlist = False
                self.watchlist[iem_cd] = cand
                self._persist_candidate(cand)
            return False, s_reason

        # 2. 갭 상태 검증
        gap_valid, gap_reason = self.evaluate_next_session_gap(
            iem_cd, open_price, prev_close, current_price, vwap, rvol, is_selling_pressure
        )
        if not gap_valid:
            if cand:
                cand.next_day_revalidated = True
                cand.next_day_decision = "NO_TRADE"
                cand.next_day_reason = gap_reason
                cand.status = WatchlistStatus.REJECTED.value
                cand.next_session_watchlist = False
                self.watchlist[iem_cd] = cand
                self._persist_candidate(cand)
            return False, gap_reason

        # 3. 정규장 전략 Setup(눌림목/돌파) 충족 검증
        if not setup_pass:
            reason = f"REGULAR_SETUP_NOT_SATISFIED: 정규장 전략 Setup 미충족 ({setup_name})"
            if cand:
                cand.next_day_revalidated = True
                cand.next_day_decision = "NO_TRADE"
                cand.next_day_reason = reason
                cand.status = WatchlistStatus.REJECTED.value
                cand.next_session_watchlist = False
                self.watchlist[iem_cd] = cand
                self._persist_candidate(cand)
            return False, reason

        # 4. ML / Edge / Risk 게이트 검증
        if not (ml_approved and edge_approved and risk_approved):
            gate_failures = []
            if not ml_approved:
                gate_failures.append("ML_REJECT")
            if not edge_approved:
                gate_failures.append("EDGE_REJECT")
            if not risk_approved:
                gate_failures.append("RISK_REJECT")
            reason = f"GATE_REJECTED: {', '.join(gate_failures)}"
            if cand:
                cand.next_day_revalidated = True
                cand.next_day_decision = "NO_TRADE"
                cand.next_day_reason = reason
                cand.status = WatchlistStatus.REJECTED.value
                cand.next_session_watchlist = False
                self.watchlist[iem_cd] = cand
                self._persist_candidate(cand)
            return False, reason

        # 모든 조건 통과 -> 최종 매수 승인
        success_reason = f"REGULAR_REVALIDATION_PASSED: {setup_name} 셋업 충족, VWAP 지지, ML/Edge/Risk 전면 통과"
        if cand:
            cand.next_day_revalidated = True
            cand.next_day_decision = "BUY_APPROVED"
            cand.next_day_reason = success_reason
            cand.status = WatchlistStatus.CONSUMED.value
            cand.next_session_watchlist = False
            self.watchlist[iem_cd] = cand
            self._persist_candidate(cand)

        logger.info(f"🚀 [익일 정규장 매수 승인] {iem_cd}: {success_reason}")

        # 정규장 매수 승인 텔레그램 알림 전송
        if self.telegram_notifier and hasattr(self.telegram_notifier, "send_regular_entry_approval_alert"):
            try:
                ah_ret = cand.after_hours_return if cand else 0.0
                name = cand.name if cand else iem_cd
                self.telegram_notifier.send_regular_entry_approval_alert(
                    symbol=iem_cd,
                    name=name,
                    after_hours_return_pct=ah_ret,
                    regular_reason=f"{setup_name} 셋업 충족 및 VWAP 지지 확인"
                )
            except Exception as e:
                logger.error(f"정규장 매수 승인 텔레그램 알림 전송 실패: {e}")

        return True, success_reason

    @staticmethod
    def validate_trade_tagging(signal_session: str, entry_session: str) -> Tuple[bool, str]:
        """
        거래 태깅 정합성 검증
        - REGULAR_SIGNAL / REGULAR_ENTRY: 정상
        - AFTER_HOURS_SIGNAL / REGULAR_ENTRY: 정상 (익일 재검증 통과 진입)
        - AFTER_HOURS_SIGNAL / AFTER_HOURS_ENTRY: 비정상 (시간외 직접 진입 시도 - 즉시 차단!)
        """
        sig_s = signal_session.upper()
        ent_s = entry_session.upper()

        if sig_s == "AFTER_HOURS" and ent_s == "AFTER_HOURS":
            msg = "CRITICAL_SESSION_VIOLATION: AFTER_HOURS_SIGNAL / AFTER_HOURS_ENTRY (시간외 급등 즉시 매수는 엄격히 차단됩니다)"
            logger.error(f"🚨 [세션 위반 감지] {msg}")
            return False, msg

        tag_str = f"{sig_s}_SIGNAL / {ent_s}_ENTRY"
        return True, f"VALID_TAG: {tag_str}"

    def record_historical_outcome(self, outcome: AfterHoursOutcomeRecord):
        """시간외 급등 종목의 익일 사후 성과 영구 기록"""
        with self._get_conn() as conn:
            conn.execute("""
            INSERT OR REPLACE INTO after_hours_outcomes (
                record_id, iem_cd, symbol_name, event_date, after_hours_return,
                spike_bracket, next_day_open, next_day_gap_return,
                open_to_5m_return, open_to_15m_return, open_to_30m_return,
                open_to_close_return, max_favorable_excursion, max_adverse_excursion,
                was_traded, entry_price, exit_price, final_pnl, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (
                outcome.record_id, outcome.iem_cd, outcome.symbol_name, outcome.event_date,
                outcome.after_hours_return, outcome.spike_bracket, outcome.next_day_open,
                outcome.next_day_gap_return, outcome.open_to_5m_return, outcome.open_to_15m_return,
                outcome.open_to_30m_return, outcome.open_to_close_return,
                outcome.max_favorable_excursion, outcome.max_adverse_excursion,
                1 if outcome.was_traded else 0, outcome.entry_price, outcome.exit_price,
                outcome.final_pnl, outcome.created_at
            ))

    def get_historical_summary_by_bracket(self) -> Dict[str, Dict[str, Any]]:
        """시간외 급등 구간별 사후 성과 통계 산출"""
        brackets = ["+3%~+5%", "+5%~+10%", "+10%~+15%", "+15% 이상"]
        summary: Dict[str, Dict[str, Any]] = {}

        with self._get_conn() as conn:
            for b in brackets:
                rows = conn.execute("""
                    SELECT * FROM after_hours_outcomes WHERE spike_bracket = ?
                """, (b,)).fetchall()

                count = len(rows)
                if count == 0:
                    summary[b] = {
                        "count": 0, "win_rate": 0.0, "avg_gap": 0.0,
                        "avg_open_to_5m": 0.0, "avg_open_to_15m": 0.0,
                        "avg_open_to_close": 0.0, "avg_mfe": 0.0, "avg_mae": 0.0
                    }
                    continue

                wins = sum(1 for r in rows if r["final_pnl"] > 0)
                summary[b] = {
                    "count": count,
                    "win_rate": round((wins / count) * 100.0, 1),
                    "avg_gap": round(sum(r["next_day_gap_return"] for r in rows) / count, 2),
                    "avg_open_to_5m": round(sum(r["open_to_5m_return"] for r in rows) / count, 2),
                    "avg_open_to_15m": round(sum(r["open_to_15m_return"] for r in rows) / count, 2),
                    "avg_open_to_close": round(sum(r["open_to_close_return"] for r in rows) / count, 2),
                    "avg_mfe": round(sum(r["max_favorable_excursion"] for r in rows) / count, 2),
                    "avg_mae": round(sum(r["max_adverse_excursion"] for r in rows) / count, 2)
                }

        return summary
