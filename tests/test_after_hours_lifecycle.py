# -*- coding: utf-8 -*-
"""
[UNIT TEST SUITE] tests/test_after_hours_lifecycle.py
=============================================================================
애프터마켓 데이터 처리 및 NEXT_SESSION_WATCHLIST Lifecycle 7대 핵심 검증 테스트

Test 1: 전일 종가 vs 당일 정규장 종가 기준 분리 검증 (오감지 차단)
Test 2: 당일 정규장 종가 기준 시간외 정상 급등 감지 및 ACTIVE 등록 검증
Test 3: 3초 주기 연속 10회 호출 시 중복 DB 쓰기/알림 폭풍 차단 검증
Test 4: 스캐너 반복 루프에서 이미 ACTIVE인 후보 종목 스킵 검증
Test 5: 익일 정규장 재검증 성공(BUY 승인) 시 ACTIVE -> CONSUMED 전이 및 활성 목록 제외 검증
Test 6: 익일 정규장 재검증 실패 시 ACTIVE -> REJECTED 전이 및 활성 목록 제외 검증
Test 7: 종료/만료 상태 종목의 다음 거래일 신규 이벤트 재등록 허용 검증
=============================================================================
"""

import os
import shutil
import unittest
import sqlite3
from datetime import datetime, date, timedelta
from unittest.mock import MagicMock

from core.after_hours_manager import (
    AfterHoursManager,
    AfterHoursCandidate,
    MarketSessionManager,
    MarketSession,
    WatchlistStatus
)
from core.models import SymbolInfo


import uuid


class TestAfterHoursLifecycle(unittest.TestCase):

    def setUp(self):
        self.test_dir = f"data/test_lifecycle_db_{uuid.uuid4().hex[:8]}"
        os.makedirs(self.test_dir, exist_ok=True)
        self.db_path = os.path.join(self.test_dir, "test_ah_lifecycle.db")
        self.mock_notifier = MagicMock()
        self.manager = AfterHoursManager(db_path=self.db_path, telegram_notifier=self.mock_notifier)

    def tearDown(self):
        if os.path.exists(self.test_dir):
            shutil.rmtree(self.test_dir, ignore_errors=True)

    def test_01_prev_close_vs_regular_close_false_spike_prevention(self):
        """
        Test 1: 전일 종가 vs 당일 정규장 종가 기준 분리 검증
        - 전일 종가 10,000원, 당일 정규장 종가 12,000원, 시간외 현재가 12,100원
        - 과거 오류 공식: (12,100 - 10,000) / 10,000 = +21.0% -> +15% 이상 과열 오감지
        - 수정 공식: (12,100 - 12,000) / 12,000 = +0.83% (< 3.0%) -> 미등록 (None 반환)
        """
        ah_time = datetime(2026, 9, 15, 16, 30, 0)
        
        # 수정 공식 적용 시 당일 정규장 종가(12,000) 기준으로 계산 -> +0.83% 미달로 등록 차단
        cand = self.manager.detect_after_hours_spike(
            iem_cd="005930",
            name="삼성전자",
            regular_close=12000.0,
            current_price=12100.0,
            volume=5000,
            dt=ah_time,
            min_spike_pct=3.0
        )
        self.assertIsNone(cand, "당일 정규장 종가 기준 +0.83%는 최소 기준(3.0%) 미달이므로 None이어야 함")
        self.assertNotIn("005930", self.manager.watchlist)

        # DB에도 기록되지 않았는지 검증
        with self.manager._get_conn() as conn:
            row = conn.execute("SELECT * FROM after_hours_candidates WHERE iem_cd = '005930'").fetchone()
            self.assertIsNone(row)

    def test_02_valid_after_hours_spike_active_registration(self):
        """
        Test 2: 당일 정규장 종가 기준 시간외 정상 급등 감지 및 ACTIVE 등록 검증
        - 당일 정규장 종가 12,000원, 시간외 현재가 12,500원
        - 등락률: (12,500 - 12,000) / 12,000 = +4.17% (>= 3.0%)
        - 상태: ACTIVE, next_session_watchlist: True, spike_bracket: +3%~+5%
        """
        ah_time = datetime(2026, 9, 15, 16, 45, 0)
        cand = self.manager.detect_after_hours_spike(
            iem_cd="000660",
            name="SK하이닉스",
            regular_close=12000.0,
            current_price=12500.0,
            volume=30000,
            turnover=375000000.0,
            dt=ah_time,
            min_spike_pct=3.0
        )

        self.assertIsNotNone(cand)
        self.assertEqual(cand.status, WatchlistStatus.ACTIVE.value)
        self.assertTrue(cand.next_session_watchlist)
        self.assertEqual(cand.after_hours_return, 4.17)
        self.assertEqual(cand.spike_bracket, "+3%~+5%")
        self.assertEqual(cand.regular_close, 12000.0)
        self.assertEqual(cand.after_hours_price, 12500.0)

        # DB 저장 검증
        with self.manager._get_conn() as conn:
            row = conn.execute("SELECT * FROM after_hours_candidates WHERE iem_cd = '000660'").fetchone()
            self.assertIsNotNone(row)
            self.assertEqual(row["status"], WatchlistStatus.ACTIVE.value)
            self.assertEqual(row["next_session_watchlist"], 1)
            self.assertEqual(float(row["regular_close"]), 12000.0)

        # [정책 변경] 텔레그램 개별 즉시 알림은 제거되어 0회 호출되어야 함 (일일 서머리로 일괄 발송)
        self.mock_notifier.send_after_hours_spike_alert.assert_not_called()

    def test_03_dedup_guard_prevents_db_write_and_alert_storm(self):
        """
        Test 3: 3초 주기 스캐너 10회 연속 호출 시 중복 감지 차단 및 알림/DB 폭풍 방지 검증
        - 1회차: 신규 ACTIVE 등록, DB INSERT
        - 2~10회차: 기존 ACTIVE 인스턴스 반환, DB 중복 쓰기 0건
        - 텔레그램 개별 알림은 0건 유지 (일일 서머리로 대체)
        """
        base_time = datetime(2026, 9, 15, 16, 0, 0)

        # 10회 연속 호출
        results = []
        for i in range(10):
            tick_time = base_time + timedelta(seconds=i * 3)
            res = self.manager.detect_after_hours_spike(
                iem_cd="042700",
                name="한미반도체",
                regular_close=50000.0,
                current_price=53000.0,  # +6.0%
                volume=10000,
                dt=tick_time
            )
            results.append(res)

        # 모든 호출이 None이 아니고 동일 후보를 반환했는지 검증
        self.assertEqual(len(results), 10)
        for r in results:
            self.assertIsNotNone(r)
            self.assertEqual(r.iem_cd, "042700")
            self.assertEqual(r.status, WatchlistStatus.ACTIVE.value)

        # [정책 변경] 텔레그램 개별 알림은 0회여야 함 (일일 서머리 1회로 통합)
        self.assertEqual(self.mock_notifier.send_after_hours_spike_alert.call_count, 0)

        # DB 레코드 확인: 오직 1건만 존재해야 하며, detected_at이 1회차 시각(16:00:00) 유지
        with self.manager._get_conn() as conn:
            rows = conn.execute("SELECT * FROM after_hours_candidates WHERE iem_cd = '042700'").fetchall()
            self.assertEqual(len(rows), 1)
            self.assertEqual(rows[0]["detected_at"], base_time.isoformat())

    def test_04_scanner_rolling_loop_skips_active_candidate(self):
        """
        Test 4: 스캐너 반복 루프에서 이미 ACTIVE인 후보 종목 스킵 검증
        - 이미 ACTIVE로 등록된 상태에서 재조회 시 추가 작업 없이 캐시 반환
        """
        ah_time = datetime(2026, 9, 15, 16, 10, 0)
        self.manager.detect_after_hours_spike(
            iem_cd="051910",
            name="LG화학",
            regular_close=300000.0,
            current_price=318000.0,  # +6.0%
            volume=5000,
            dt=ah_time
        )

        active_list = self.manager.get_active_watchlist()
        self.assertEqual(len(active_list), 1)
        self.assertEqual(active_list[0].iem_cd, "051910")
        self.assertEqual(active_list[0].status, WatchlistStatus.ACTIVE.value)

        # 스캐너 루프에서 재감지 시도
        res = self.manager.detect_after_hours_spike(
            iem_cd="051910",
            name="LG화학",
            regular_close=300000.0,
            current_price=319000.0,
            volume=6000,
            dt=ah_time + timedelta(seconds=5)
        )
        self.assertIsNotNone(res)
        # 기존 등록 상태 그대로 유지
        self.assertEqual(res.status, WatchlistStatus.ACTIVE.value)
        self.assertEqual(res.after_hours_price, 318000.0)

    def test_05_revalidation_success_transitions_active_to_consumed(self):
        """
        Test 5: 익일 정규장 재검증 성공 (BUY 승인) 시
        - ACTIVE -> CONSUMED 상태 전이
        - next_session_watchlist: False
        - get_active_watchlist() 쿼리에서 즉시 제외
        """
        ah_time = datetime(2026, 9, 15, 17, 0, 0)
        cand = self.manager.detect_after_hours_spike(
            iem_cd="005930",
            name="삼성전자",
            regular_close=70000.0,
            current_price=73500.0,  # +5.0%
            volume=20000,
            dt=ah_time
        )
        self.assertEqual(cand.status, WatchlistStatus.ACTIVE.value)
        self.assertEqual(len(self.manager.get_active_watchlist()), 1)

        # 익일 09:10 정규장 재검증 성공
        reg_time = datetime(2026, 9, 16, 9, 10, 0)
        approved, reason = self.manager.revalidate_in_regular_session(
            iem_cd="005930",
            open_price=72500.0,
            prev_close=70000.0,
            current_price=72800.0,
            vwap=72600.0,
            rvol=2.1,
            setup_pass=True,
            setup_name="INT_VWAP_PULLBACK",
            ml_approved=True,
            edge_approved=True,
            risk_approved=True,
            now=reg_time
        )

        self.assertTrue(approved)
        self.assertIn("REGULAR_REVALIDATION_PASSED", reason)

        # 상태 전이 검증: ACTIVE -> CONSUMED
        updated = self.manager.watchlist["005930"]
        self.assertEqual(updated.status, WatchlistStatus.CONSUMED.value)
        self.assertFalse(updated.next_session_watchlist)
        self.assertEqual(updated.next_day_decision, "BUY_APPROVED")

        # DB 반영 검증
        with self.manager._get_conn() as conn:
            row = conn.execute("SELECT * FROM after_hours_candidates WHERE iem_cd = '005930'").fetchone()
            self.assertEqual(row["status"], WatchlistStatus.CONSUMED.value)
            self.assertEqual(row["next_session_watchlist"], 0)

        # 활성 워치리스트 조회 시 제외되었는지 검증
        active_list = self.manager.get_active_watchlist()
        self.assertEqual(len(active_list), 0, "CONSUMED 종목은 get_active_watchlist에서 제외되어야 함")

    def test_06_revalidation_failure_transitions_active_to_rejected(self):
        """
        Test 6: 익일 정규장 재검증 실패 (GAP 과열/소진 또는 Setup 미충족) 시
        - ACTIVE -> REJECTED 상태 전이
        - next_session_watchlist: False
        - get_active_watchlist() 쿼리에서 즉시 제외
        """
        ah_time = datetime(2026, 9, 15, 16, 20, 0)
        cand = self.manager.detect_after_hours_spike(
            iem_cd="035720",
            name="카카오",
            regular_close=40000.0,
            current_price=42400.0,  # +6.0%
            volume=15000,
            dt=ah_time
        )
        self.assertEqual(cand.status, WatchlistStatus.ACTIVE.value)

        # 익일 09:05 재검증: 시초 과도 갭상승 + 거래량 고갈 (Exhaustion Gap)
        reg_time = datetime(2026, 9, 16, 9, 5, 0)
        approved, reason = self.manager.revalidate_in_regular_session(
            iem_cd="035720",
            open_price=45000.0,   # +12.5% 극단적 갭상승
            prev_close=40000.0,
            current_price=44500.0,
            vwap=44800.0,        # VWAP 하회
            rvol=0.6,            # 거래량 고갈
            setup_pass=True,
            setup_name="INT_BREAKOUT",
            ml_approved=True,
            edge_approved=True,
            risk_approved=True,
            is_selling_pressure=True,
            now=reg_time
        )

        self.assertFalse(approved)
        self.assertIn("OVEREXTENDED_GAP_EXHAUSTION", reason)

        # 상태 전이 검증: ACTIVE -> REJECTED
        updated = self.manager.watchlist["035720"]
        self.assertEqual(updated.status, WatchlistStatus.REJECTED.value)
        self.assertFalse(updated.next_session_watchlist)
        self.assertEqual(updated.next_day_decision, "NO_TRADE")

        # DB 반영 검증
        with self.manager._get_conn() as conn:
            row = conn.execute("SELECT * FROM after_hours_candidates WHERE iem_cd = '035720'").fetchone()
            self.assertEqual(row["status"], WatchlistStatus.REJECTED.value)
            self.assertEqual(row["next_session_watchlist"], 0)

        # 활성 워치리스트 조회 시 제외되었는지 검증
        active_list = self.manager.get_active_watchlist()
        self.assertEqual(len(active_list), 0, "REJECTED 종목은 get_active_watchlist에서 제외되어야 함")

    def test_07_terminal_or_next_date_allows_new_event_registration(self):
        """
        Test 7: 종료/만료 상태 종목의 다음 거래일 신규 이벤트 재등록 허용 검증
        - 1일차: 시간외 급등 -> ACTIVE -> 익일 재검증으로 CONSUMED 완료
        - 2일차: 새로운 시간외 세션에서 동일 종목이 다시 급등 발생 시
        - 신규 등록 허용: 새 candidate 생성 및 ACTIVE로 재등록
        """
        day1_time = datetime(2026, 9, 14, 16, 30, 0)
        cand1 = self.manager.detect_after_hours_spike(
            iem_cd="000660",
            name="SK하이닉스",
            regular_close=100000.0,
            current_price=106000.0,  # +6.0%
            volume=20000,
            dt=day1_time
        )
        self.assertIsNotNone(cand1)
        self.assertEqual(cand1.status, WatchlistStatus.ACTIVE.value)

        # 1일차 종목 CONSUMED로 처리
        self.manager.mark_consumed("000660", "DAY1_BOUGHT")
        self.assertEqual(self.manager.watchlist["000660"].status, WatchlistStatus.CONSUMED.value)
        self.assertEqual(len(self.manager.get_active_watchlist()), 0)

        # 2일차 새로운 시간외 급등 발생 (정규장 종가 110,000 -> 시간외 115,000: +4.55%)
        day2_time = datetime(2026, 9, 15, 16, 45, 0)
        cand2 = self.manager.detect_after_hours_spike(
            iem_cd="000660",
            name="SK하이닉스",
            regular_close=110000.0,
            current_price=115000.0,  # +4.55%
            volume=35000,
            dt=day2_time
        )

        self.assertIsNotNone(cand2)
        self.assertEqual(cand2.status, WatchlistStatus.ACTIVE.value)
        self.assertTrue(cand2.next_session_watchlist)
        self.assertEqual(cand2.regular_close, 110000.0)
        self.assertEqual(cand2.after_hours_price, 115000.0)
        self.assertEqual(cand2.after_hours_return, 4.55)

        # 활성 워치리스트에 새 이벤트로 등록되어 있어야 함
        active_list = self.manager.get_active_watchlist()
        self.assertEqual(len(active_list), 1)
        self.assertEqual(active_list[0].iem_cd, "000660")
        self.assertEqual(active_list[0].status, WatchlistStatus.ACTIVE.value)


    def test_08_resolve_d0_regular_close_fallback_and_data_unavailable_defense(self):
        """
        D0 정규장 종가 획득 실패 시 방어 로직 검증:
        1. sym.regular_close가 이미 존재하면 해당 값 사용
        2. sym.regular_close 부재 시 quote_client.get_daily_candles 당일 일봉 참조
        3. 일봉 데이터 부재 또는 과거 일봉인 경우 None 반환 (DATA_UNAVAILABLE -> sym.prev_close 대용 절대 차단)
        """
        from execution.live_quant_trader import LiveQuantTrader
        trader = LiveQuantTrader.__new__(LiveQuantTrader)

        now = datetime(2026, 9, 15, 16, 30, 0)
        sym = SymbolInfo(iem_cd="005930", name="삼성전자", price=72000, prev_close=70000)

        # Case 1: sym.regular_close가 없는 경우 -> daily candles 조회
        mock_client = MagicMock()
        mock_client.get_daily_candles.return_value = [
            {"date": "20260915", "open": 70500, "close": 71800, "volume": 1000000}
        ]
        resolved = trader._resolve_d0_regular_close(sym, mock_client, now)
        self.assertEqual(resolved, 71800.0)
        self.assertEqual(sym.regular_close, 71800.0)

        # Case 2: sym.regular_close가 이미 존재하는 경우 -> 캐시된 정규장 종가 즉시 반환
        mock_client.reset_mock()
        resolved2 = trader._resolve_d0_regular_close(sym, mock_client, now)
        self.assertEqual(resolved2, 71800.0)
        mock_client.get_daily_candles.assert_not_called()

        # Case 3: 일봉 조회 실패/과거 일봉인 경우 -> None 반환 (DATA_UNAVAILABLE 방어)
        sym_unknown = SymbolInfo(iem_cd="999999", name="미확인", price=5000, prev_close=4500)
        mock_client.get_daily_candles.return_value = [
            {"date": "20260914", "open": 4600, "close": 4500, "volume": 50000}  # 어제 일봉
        ]
        resolved3 = trader._resolve_d0_regular_close(sym_unknown, mock_client, now)
        self.assertIsNone(resolved3, "당일 일봉이 없으면 sym.prev_close를 대용하지 않고 None을 반환해야 함")

        # detect_after_hours_spike에 0 또는 None 전달 시 즉시 거부
        cand = self.manager.detect_after_hours_spike(
            iem_cd="999999",
            name="미확인",
            regular_close=0.0,
            current_price=5000.0,
            dt=now
        )
        self.assertIsNone(cand)


if __name__ == "__main__":
    unittest.main()
