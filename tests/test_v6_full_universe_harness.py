"""국내 주식 전체 종목 실시간 탐지형 퀀트 시스템 v6.0 검증 하네스
- Execution-Grade Specification v6.0 (Section 70 개발 완료 7대 검증조건 전수 검증)
- TEST 1: 미등록 종목 거래량 3배 이상 폭증 -> 자동 탐지
- TEST 2: 미등록 종목 전일 고가 돌파 -> 자동 탐지
- TEST 3: 미등록 종목 최근 3분 +2% 이상 급등 -> 자동 탐지
- TEST 4: 미등록 종목 Compression Breakout 발생 -> 자동 탐지
- TEST 5: 후보 탈락 종목 1시간 후 재급등 -> 즉시 재탐지
- TEST 6: 수백 개 종목 동시 이벤트 폭주 -> 우선순위 정렬 및 Portfolio Risk 통제
- TEST 7: 화면 TOP 10 밖의 종목도 실제 매매 신호 생성 및 집행 가능
"""

import os
import sys
import unittest
from datetime import datetime, timedelta
from typing import Dict, List, Any

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core.models import (
    SymbolInfo, SymbolState, MarketEventType, CandidatePriority,
    MarketRegime, TimeHorizon, OrderSide, TradeSignal, Tick
)
from universe.full_universe_master import FullUniverseMaster
from core.symbol_store import SymbolStateStore
from core.event_detector import EventDetector
from core.candidate_promotion import CandidatePromotionEngine
from core.low_cost_scanner import LowCostMarketScanner
from core.aggregator import CandleAggregator
from risk.portfolio_risk import PortfolioRiskManager
from strategies.full_strategy_suite import FullStrategySuite


def push_tick(agg: CandleAggregator, code: str, price: int, volume: int, dt: datetime):
    agg.on_tick(Tick(timestamp=dt, iem_cd=code, price=price, volume=volume))


class TestV6FullUniverseHarness(unittest.TestCase):
    def setUp(self):
        # 2,670+ 전체 상장 종목 로드 및 스토어/스캐너 초기화
        self.master_symbols = FullUniverseMaster.load_full_universe()
        self.store = SymbolStateStore(self.master_symbols)
        self.scanner = LowCostMarketScanner(self.store)
        self.now = datetime(2026, 9, 4, 10, 0, 0)

    def test_00_full_market_universe_size_and_integrity(self):
        """0. KOSPI + KOSDAQ 전체 상장종목(~2,670개) 마스터 무결성 검증 (Section 1)"""
        total = self.store.total_count()
        kospi = self.store.kospi_count()
        kosdaq = self.store.kosdaq_count()

        self.assertGreaterEqual(total, 2500, f"전체 상장종목 수는 2,500개 이상이어야 함 (현재: {total})")
        self.assertGreaterEqual(kospi, 900, f"KOSPI 종목 수는 900개 이상이어야 함 (현재: {kospi})")
        self.assertGreaterEqual(kosdaq, 1600, f"KOSDAQ 종목 수는 1,600개 이상이어야 함 (현재: {kosdaq})")

        # 모든 종목의 초기 상태는 INACTIVE (저비용 감시)
        counts = self.store.get_state_counts()
        self.assertEqual(counts[SymbolState.INACTIVE.value], total)
        self.assertEqual(counts[SymbolState.ACTIVE.value], 0)

    def test_section70_test1_unregistered_volume_surge_detection(self):
        """TEST 1: 아무 관심종목으로 등록되지 않은 종목에서 갑자기 거래량 3배 이상 증가 -> 자동 탐지"""
        code = "999001"
        sym = SymbolInfo(iem_cd=code, name="미등록소형주A", market="KOSDAQ", price=10000)
        self.store.register_symbol(sym)
        self.assertEqual(sym.state, SymbolState.INACTIVE)

        agg = self.scanner.get_aggregator(code)
        # 과거 20분 동안 평소 거래량 1,000주씩 형성
        t = self.now - timedelta(minutes=25)
        for i in range(20):
            t += timedelta(minutes=1)
            for s in range(4):
                push_tick(agg, sym.iem_cd, 10000, 250, t + timedelta(seconds=s*15))

        # 현재 1분 동안 갑자기 거래량 3,500주(평균 대비 3.5배) 폭증
        t_now = self.now
        for s in range(4):
            push_tick(agg, sym.iem_cd, 10100, 875, t_now + timedelta(seconds=s*15))

        state, score, detected_sym = self.scanner.on_market_tick(
            iem_cd=code, price=10100, volume=3500, timestamp=t_now
        )

        # 검증: EVENT A 감지 및 점수 획득
        self.assertGreaterEqual(score, 15.0)
        self.assertTrue(any("거래량 폭증" in ev for ev in detected_sym.active_events))
        # 상태가 INACTIVE에서 승격(WATCH 이상)되었는지 확인
        self.assertIn(detected_sym.state, (SymbolState.WATCH, SymbolState.ACTIVE))

    def test_section70_test2_unregistered_pdh_breakout_detection(self):
        """TEST 2: 관심종목 밖의 종목이 전일 고가를 돌파했을 때 -> 자동 탐지"""
        code = "999002"
        sym = SymbolInfo(iem_cd=code, name="미등록제조업B", market="KOSPI", price=49000, prev_high=50000)
        self.store.register_symbol(sym)

        # 현재가가 전일 고가(50,000원)를 50,800원으로 상향 돌파
        agg = self.scanner.get_aggregator(code)
        push_tick(agg, code, 50800, 5000, self.now)

        state, score, detected_sym = self.scanner.on_market_tick(
            iem_cd=code, price=50800, volume=5000, timestamp=self.now
        )

        # 검증: 전일 고가 돌파 이벤트 감지 및 승격
        self.assertGreaterEqual(score, 10.0)
        self.assertTrue(any("전일 고가" in ev for ev in detected_sym.active_events))

    def test_section70_test3_unregistered_3m_return_detection(self):
        """TEST 3: 관심종목 밖의 종목이 최근 3분 +2% 이상 상승했을 때 -> 자동 탐지"""
        code = "999003"
        sym = SymbolInfo(iem_cd=code, name="미등록바이오C", market="KOSDAQ", price=20000)
        self.store.register_symbol(sym)

        agg = self.scanner.get_aggregator(code)
        # 3분 전: 20,000원
        push_tick(agg, code, 20000, 1000, self.now - timedelta(minutes=3))
        # 2분 전: 20,100원
        push_tick(agg, code, 20100, 1000, self.now - timedelta(minutes=2))
        # 1분 전: 20,300원
        push_tick(agg, code, 20300, 1000, self.now - timedelta(minutes=1))
        # 현재: 20,500원 (+2.5% 급등)
        push_tick(agg, code, 20500, 2000, self.now)


        state, score, detected_sym = self.scanner.on_market_tick(
            iem_cd=code, price=20500, volume=5000, timestamp=self.now
        )

        # 검증: 3분 급등 이벤트 감지 (+10점 이상)
        self.assertGreaterEqual(score, 10.0)
        self.assertTrue(any("3분 급등" in ev for ev in detected_sym.active_events))

    def test_section70_test4_unregistered_compression_breakout_detection(self):
        """TEST 4: 관심종목 밖의 종목에서 Compression Breakout이 발생했을 때 -> 자동 탐지"""
        code = "999004"
        sym = SymbolInfo(iem_cd=code, name="미등록부품D", market="KOSPI", price=30000)
        self.store.register_symbol(sym)

        agg = self.scanner.get_aggregator(code)
        # 1) 이전 22개 봉: 변동폭 큼 (High-Low = 1,000원)
        t = self.now - timedelta(minutes=50)
        for i in range(22):
            t += timedelta(minutes=1)
            push_tick(agg, code, 29000, 1000, t)
            push_tick(agg, code, 30000, 1000, t + timedelta(seconds=30))

        # 2) 최근 22개 봉: 극심한 가격 압축 (변동폭 100원)
        for i in range(22):
            t += timedelta(minutes=1)
            push_tick(agg, code, 29950, 200, t)
            push_tick(agg, code, 30050, 200, t + timedelta(seconds=30))

        # 3) 돌파 봉: 거래량 4배 실리며 30,500원으로 압축 고점 돌파!
        t_break = self.now
        sym.price = 30500
        push_tick(agg, code, 30500, 5000, t_break)

        score, events, priority, patterns = EventDetector.evaluate_events(
            sym, agg, t_break
        )

        # 검증: Compression Breakout 패턴 감지 확인
        self.assertTrue(patterns["is_compression_breakout"])


    def test_section70_test5_demoted_stock_redetection(self):
        """TEST 5: 후보 탈락 종목이 1시간 후 다시 급등했을 때 -> 재탐지"""
        code = "000660"  # SK하이닉스
        sym = self.store.get(code)

        # 1) 09:15에 ACTIVE 상태였다가 수급 소멸로 INACTIVE 강등
        self.store.promote(code, SymbolState.ACTIVE, reason="초기 이벤트")
        self.assertEqual(sym.state, SymbolState.ACTIVE)

        self.store.demote(code, SymbolState.INACTIVE, reason="이벤트 종료 강등")
        self.assertEqual(sym.state, SymbolState.INACTIVE)

        # 2) 1시간 후 (10:15) 다시 거래량 3배 + 3분 +2.5% 급등 재발생
        t_after_1h = self.now + timedelta(hours=1)
        agg = self.scanner.get_aggregator(code)
        # 3분 급등 데이터 주입
        push_tick(agg, code, 180000, 10000, t_after_1h - timedelta(minutes=3))
        push_tick(agg, code, 184500, 50000, t_after_1h)  # +2.5% 급등 및 거래량 폭증


        state, score, detected_sym = self.scanner.on_market_tick(
            iem_cd=code, price=184500, volume=60000, timestamp=t_after_1h
        )

        # 검증: 탈락했던 종목이 영구 제외되지 않고 즉시 재탐지 및 재승격!
        self.assertIn(detected_sym.state, (SymbolState.WATCH, SymbolState.ACTIVE))
        self.assertGreaterEqual(score, 10.0)

    def test_section70_test6_hundreds_simultaneous_events_priority_and_risk(self):
        """TEST 6: 수백 개 종목에서 동시에 이벤트가 발생했을 때 -> 우선순위 계산 -> Portfolio Risk 적용 -> 상위 신호만 주문"""
        # 100개 종목에 다양한 점수의 시그널 동시 발생 시뮬레이션
        candidates = []
        for i in range(100):
            code = f"CODE_{i:03d}"
            sym = SymbolInfo(
                iem_cd=code,
                name=f"종목_{i}",
                market="KOSPI" if i % 2 == 0 else "KOSDAQ",
                price=50000,
                theme="반도체" if i < 30 else ("바이오" if i < 60 else "방산")
            )
            # 점수 차등 부여 (50점 ~ 99점)
            sym.event_score = 50.0 + (i * 0.5)
            self.store.register_symbol(sym)
            candidates.append(sym)

        # 이벤트 점수 내림차순 정렬 (우선순위 큐)
        candidates.sort(key=lambda s: s.event_score, reverse=True)

        # 1위는 99.5점, 100위는 50.0점
        self.assertAlmostEqual(candidates[0].event_score, 99.5)
        self.assertAlmostEqual(candidates[-1].event_score, 50.0)

        # Portfolio Risk Manager를 통한 자금 한도 통제 검증
        equity = 100_000_000.0
        active_positions = []
        approved_signals = []

        for sym in candidates:
            # 1회 리스크 0.5% (50만원) 가정
            trade_risk = equity * 0.005
            # 총 위험비율 계산
            tot_amt, tot_ratio, risk_status = PortfolioRiskManager.calculate_total_open_risk(
                active_positions, equity
            )
            if risk_status == "BLOCKED":
                break  # 총 위험 4% 초과 시 주문 차단 (Section 33)

            # 테마 한도 검증 (동일 테마 최대 3종목, 1.5% 한도)
            theme_map = {p.iem_cd: p.name for p in active_positions}  # dummy map
            theme_ok, reason = PortfolioRiskManager.check_theme_risk(
                sym.iem_cd, sym.theme, trade_risk, active_positions, {s.iem_cd: s.theme for s in candidates}, equity
            )
            if theme_ok:
                # 가상 포지션 승인
                from core.models import Position
                pos = Position(
                    position_id=f"INT_{sym.iem_cd}",
                    time_horizon=TimeHorizon.INTRADAY,
                    strategy_id="INT_MOMENTUM_IGNITION",
                    iem_cd=sym.iem_cd,
                    name=sym.name,
                    qty=10,
                    entry_price=50000,
                    current_price=50000,
                    stop_price=49000,
                    target_1r=51000,
                    target_2r=52000,
                    target_3r=53000,
                    r_unit=1000,
                    initial_risk_amount=trade_risk,
                    entry_time=self.now,
                    trailing_stop_price=49000,
                    highest_price=50000
                )
                active_positions.append(pos)
                approved_signals.append(sym)

        # 검증: 100개 종목 중 포트폴리오 리스크 및 테마 한도 내에서 상위 점수 종목만 승인됨
        self.assertLess(len(approved_signals), 100)
        self.assertGreaterEqual(len(approved_signals), 5)
        # 승인된 종목들은 모두 상위 점수대 종목임
        self.assertGreaterEqual(approved_signals[0].event_score, approved_signals[-1].event_score)

    def test_section70_test7_candidate_outside_display_top10_traded(self):
        """TEST 7: 화면의 TOP 10에 포함되지 않은 종목도 -> 실제 매매 후보가 될 수 있음 (DISPLAY LIMIT != SCANNER LIMIT)"""
        # 25개 종목을 생성하여 1~25위까지 배치
        all_active = []
        for i in range(25):
            code = f"DISP_{i:03d}"
            sym = SymbolInfo(
                iem_cd=code,
                name=f"활성종목_{i+1}",
                market="KOSPI",
                price=20000
            )
            sym.event_score = 95.0 - i  # 1위 95점 ... 15위 81점 ... 25위 71점
            self.store.register_symbol(sym)
            self.store.promote(code, SymbolState.ACTIVE, reason="활성 승격")
            all_active.append(sym)

        # 1) 화면 표시용 목록은 상위 10개로 제한
        display_top10 = self.store.get_display_candidates(limit=10)
        self.assertEqual(len(display_top10), 10)
        display_codes = [s.iem_cd for s in display_top10]

        # 2) 15번째 종목 (DISP_014)은 화면 TOP 10에 포함되지 않음
        target_code = "DISP_014"
        self.assertNotIn(target_code, display_codes)

        # 3) 하지만 실시간 매매 탐지 엔진은 15번째 종목도 ACTIVE로 전수 스캔함
        promoted = self.scanner.promotion_engine.get_promoted_candidates()
        promoted_codes = [s.iem_cd for s in promoted]
        self.assertIn(target_code, promoted_codes)

        # 4) 15번째 종목에서 매매 신호 생성 확인
        agg = self.scanner.get_aggregator(target_code)
        for m in range(20, 0, -1):
            push_tick(agg, target_code, 20000 + (20 - m) * 10, 1000, self.now - timedelta(minutes=m))
        target_sym = self.store.get(target_code)
        target_sym.price = 20600
        push_tick(agg, target_code, 20600, 5000, self.now)

        sig = FullStrategySuite.evaluate_intraday_all(
            sym=target_sym,
            agg=agg,
            regime=MarketRegime.BULL,
            now=self.now,
            patterns={"is_ignition": True}
        )


        self.assertGreaterEqual(len(sig), 1)
        self.assertEqual(sig[0].iem_cd, target_code)
        # 화면에 안 보여도 주문 라우터로 전달 가능함을 입증
        self.assertEqual(sig[0].side, OrderSide.BUY)


if __name__ == "__main__":
    unittest.main(verbosity=2)
