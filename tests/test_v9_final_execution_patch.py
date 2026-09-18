"""[FINAL PATCH v9.3] 단위 및 통합 회귀 테스트 스위트
- 12단계 전체 파이프라인 검증 (UNIVERSE -> FILL)
- 호가창(Ask 1) 기반 Edge Engine & 기대값(Expected Net R >= +0.15R) 검증
- ML Fail-Safe 무중단 대체 확률 검증
- 상대강도(RS) 및 채점 엔진 등급 체계 검증
- 좀비 주문(ACK 지연 > 3000ms) 탐지 및 계좌 대조 정합성(Reconciliation) 검증
- 정밀 시간 동기화(TimeSync) 및 스테이지 지연시간(Stage Latency) 검증
- 전략 독립성(Strategy Independence: No AND Coupling) 및 하드게이트 스프레드(0.30%) 검증
"""

import unittest
from datetime import datetime, timedelta
from core.models import (
    SymbolInfo, SymbolState, TradeSignal, Order, OrderSide, OrderType,
    OrderStatus, TimeHorizon, MarketRegime, Candle
)
from core.edge_engine import EdgeEngine, EdgeResult
from core.time_sync import TimeSync, StageTimer
from strategies.scoring_engine import ScoringEngine
from strategies.full_strategy_suite import FullStrategySuite
from execution.order_router import OrderRouter
from execution.diagnostic_engine import DiagnosticEngine
from core.aggregator import CandleAggregator


class DummyCircuitBreaker:
    is_tripped = False
    trip_reason = ""
    def check_data_staleness(self, dt): return False
    def record_order_success(self): pass
    def record_order_failure(self, err): pass


class TestV9FinalExecutionPatch(unittest.TestCase):
    def setUp(self):
        self.diagnostic = DiagnosticEngine()
        self.edge_engine = EdgeEngine(min_expected_net_r=0.15)
        self.order_router = OrderRouter(namu_client=None, circuit_breaker=DummyCircuitBreaker())

    def test_01_12_stage_pipeline_telemetry(self):
        """1. 12단계 매수 파이프라인 실시간 카운터 및 텔레메트리 정상 기록 검증"""
        diag = self.diagnostic
        diag.record_universe(3136)
        diag.record_event_detected(100)
        diag.record_candidate(45)
        diag.record_setup_match(20)
        diag.record_hard_gate_passed(18)
        diag.record_score_passed(15)
        diag.record_edge_passed(12)
        diag.record_risk_passed(10)
        diag.record_execution_passed(8)
        diag.record_buy_approved(8)
        diag.record_order_sent(8)
        diag.record_fill(7)

        t = diag.telemetry
        self.assertEqual(t.universe_count, 3136)
        self.assertEqual(t.event_detected, 100)
        self.assertEqual(t.candidates, 45)
        self.assertEqual(t.setup_matches, 20)
        self.assertEqual(t.hard_gate_passed, 18)
        self.assertEqual(t.score_passed, 15)
        self.assertEqual(t.edge_passed, 12)
        self.assertEqual(t.risk_passed, 10)
        self.assertEqual(t.execution_passed, 8)
        self.assertEqual(t.buy_approved, 8)
        self.assertEqual(t.orders_sent, 8)
        self.assertEqual(t.filled, 7)

    def test_02_stage_latency_tracking(self):
        """2. StageTimer 및 Avg, P95, Max 밀리초 지연시간 산출 검증"""
        diag = self.diagnostic
        for lat in [5.0, 10.0, 15.0, 20.0, 100.0]:
            diag.record_stage_latency("edge", lat)

        lats = diag.telemetry.stage_latencies.get("edge", {})
        self.assertIn("avg", lats)
        self.assertIn("p95", lats)
        self.assertIn("max", lats)
        self.assertEqual(lats["max"], 100.0)
        self.assertGreater(lats["avg"], 0.0)

        with StageTimer() as timer:
            x = sum(i for i in range(10000))
        self.assertGreater(timer.elapsed_ms, 0.0)

    def test_03_edge_engine_ask1_and_positive_net_r(self):
        """3. Ask 1 진입가 기준 Expected Net R 계산 및 +0.15R 이상 승인 검증"""
        sym = SymbolInfo(
            iem_cd="005930",
            name="삼성전자",
            price=70000,
            ask1_price=70100,
            bid1_price=70000,
            is_tradable=True
        )
        sig = TradeSignal(
            strategy_id="INT_BREAKOUT",
            time_horizon=TimeHorizon.INTRADAY,
            iem_cd="005930",
            name="삼성전자",
            side=OrderSide.BUY,
            strategy_price=70000,
            stop_price=69000,
            target_1r=72000,
            score=85.0,
            reason="Breakout Test",
            timestamp=datetime.now()
        )
        # Ask 1 가격(70,100원)을 진입 가격으로 채택 확인
        res = self.edge_engine.calculate_edge(sym=sym, signal=sig)
        self.assertEqual(res.entry_price, 70100)
        self.assertTrue(res.is_approved)
        self.assertGreaterEqual(res.expected_net_r, 0.15)

    def test_04_edge_engine_rejection_low_net_r(self):
        """4. 기대값 부족 (< +0.15R) 종목에 대한 정확한 필터링 및 반려 사유 검증"""
        sym = SymbolInfo(iem_cd="000660", name="SK하이닉스", price=100000, ask1_price=100000, is_tradable=True)
        # 목표가가 진입가와 거의 동일하여 보상비율이 극도로 낮은 경우
        sig = TradeSignal(
            strategy_id="INT_MOMENTUM",
            time_horizon=TimeHorizon.INTRADAY,
            iem_cd="000660",
            name="SK하이닉스",
            side=OrderSide.BUY,
            strategy_price=100000,
            stop_price=99000,
            target_1r=100200,  # +200원 목표 vs -1,000원 손절 -> 리워드 0.2R에 불과
            score=60.0,
            reason="Weak Target",
            timestamp=datetime.now()
        )
        res = self.edge_engine.calculate_edge(sym=sym, signal=sig, p_target=0.50, p_stop=0.50)
        self.assertFalse(res.is_approved)
        self.assertLess(res.expected_net_r, 0.15)
        self.assertIn("LOW_EXPECTED_EDGE", res.rejection_reason)

    def test_05_ml_fail_safe_fallback(self):
        """5. ML 모델 예외/장애 시 무중단 Rule Fallback 확률 적용 검증"""
        class BrokenMLModel:
            def predict_prob(self, sym, sig):
                raise RuntimeError("ML GPU Out Of Memory")

        sym = SymbolInfo(iem_cd="035420", name="NAVER", price=200000, is_tradable=True)
        sig = TradeSignal(
            strategy_id="INT_ORB",
            time_horizon=TimeHorizon.INTRADAY,
            iem_cd="035420",
            name="NAVER",
            side=OrderSide.BUY,
            strategy_price=200000,
            stop_price=196000,
            target_1r=208000,
            score=80.0,
            reason="ML Fail-Safe Test",
            timestamp=datetime.now()
        )
        # ML 모델이 예외를 발생시켜도 시스템이 다운되지 않고 Rule 기반 확률로 계산
        res = self.edge_engine.calculate_edge(sym=sym, signal=sig, ml_model=BrokenMLModel())
        self.assertTrue(res.is_approved)
        # score=80 -> p_target = 0.50 + (80-60)*0.005 = 0.60
        self.assertAlmostEqual(res.p_target, 0.60, places=2)
        self.assertAlmostEqual(res.p_stop, 0.40, places=2)

    def test_06_scoring_engine_relative_strength_and_grades(self):
        """6. 상대강도(RS) 가산점 및 점수대별 등급 체계 검증"""
        # A+ 등급 검증 (점수 >= 90.0)
        score_a_plus, grade_a_plus = ScoringEngine.score_intraday(
            market_regime=MarketRegime.STRONG_BULL,
            rvol=3.5,
            price_above_vwap=True,
            vwap_rising=True,
            ema_aligned=True,
            rsi=65.0,
            breakout_type=["ORB", "PDH"],
            relative_strength=0.035,   # RS +3.5% -> +10점
            turnover_ratio=3.2,         # 거래대금 3.2배 -> +15점
            is_breakout_20=True
        )
        self.assertGreaterEqual(score_a_plus, 90.0)
        self.assertEqual(grade_a_plus, "A+")

        # B+ 등급 (BUY 승인 가능, 60.0 <= score < 80.0)
        score_b, grade_b = ScoringEngine.score_intraday(
            market_regime=MarketRegime.NEUTRAL,
            rvol=1.8,
            price_above_vwap=True,
            vwap_rising=False,
            ema_aligned=False,
            rsi=52.0,
            breakout_type="NONE",
            relative_strength=0.012,   # RS +1.2% -> +6점
            turnover_ratio=1.5
        )
        self.assertGreaterEqual(score_b, 60.0)
        self.assertLess(score_b, 80.0)
        self.assertEqual(grade_b, "B+")

        # WATCH 등급 (50.0 <= score < 60.0)
        score_watch, grade_watch = ScoringEngine.score_intraday(
            market_regime=MarketRegime.NEUTRAL,
            rvol=1.5,
            price_above_vwap=False,
            vwap_rising=True,
            ema_aligned=False,
            rsi=51.0,
            breakout_type="NONE",
            relative_strength=0.005,
            turnover_ratio=1.5
        )
        self.assertGreaterEqual(score_watch, 50.0)
        self.assertLess(score_watch, 60.0)
        self.assertEqual(grade_watch, "WATCH")

        # NO TRADE 등급 (score < 50.0)
        score_nt, grade_nt = ScoringEngine.score_intraday(
            market_regime=MarketRegime.BEAR,
            rvol=0.8,
            price_above_vwap=False,
            vwap_rising=False,
            ema_aligned=False,
            rsi=40.0,
            breakout_type="NONE"
        )
        self.assertLess(score_nt, 50.0)
        self.assertEqual(grade_nt, "NO TRADE")

    def test_07_zombie_order_detection_and_reconciliation(self):
        """7. 좀비 주문(ACK 지연 > 3000ms) 탐지 및 계좌 정합성(Reconciliation) 검증"""
        now = datetime.now()
        order_zombie = Order(
            client_order_id="ZOMBIE_TEST_101",
            iem_cd="005930",
            side=OrderSide.BUY,
            order_type=OrderType.MARKET,
            qty=20,
            price=70000,
            strategy_id="INT_BREAKOUT",
            time_horizon=TimeHorizon.INTRADAY,
            status=OrderStatus.PENDING,
            created_at=now - timedelta(seconds=4),
            sent_at=now - timedelta(seconds=4)
        )
        self.order_router.pending_orders[order_zombie.client_order_id] = order_zombie

        # 3000ms 초과 주문 자동 탐지
        zombies = self.order_router.check_zombie_orders(timeout_ms=3000)
        self.assertEqual(len(zombies), 1)
        self.assertTrue(order_zombie.is_zombie_suspected)

        # 브로커 잔고 대조를 통한 안전 정합성 확보 (임의 재전송 금지)
        class MockClient:
            def get_balance(self):
                return {"cash": 5000000, "holdings": [{"iem_cd": "005930", "qty": 20, "avg_price": 70000}]}

        recon_res = self.order_router.reconcile_orders(client=MockClient())
        self.assertEqual(recon_res["reconciled_count"], 1)
        self.assertEqual(recon_res["filled_count"], 1)
        self.assertEqual(order_zombie.status, OrderStatus.FILLED)
        self.assertTrue(order_zombie.reconciled)
        self.assertNotIn(order_zombie.client_order_id, self.order_router.pending_orders)

    def test_08_strategy_independence_no_and_coupling(self):
        """8. 개별 전략 독립성 (AND 결합 절대 배제: 단일 셋업만으로도 독립 신호 생성)"""
        now = datetime.now()
        sym = SymbolInfo(iem_cd="TEST_IND", name="독립전략테스트", market="KOSPI", price=10000, prev_high=9800, is_tradable=True)
        agg = CandleAggregator(iem_cd="TEST_IND")
        t_base = now - timedelta(minutes=5)
        for i in range(5):
            c = Candle(timestamp=t_base + timedelta(minutes=i), timeframe="1m", open=9700, high=9800, low=9650, close=9750, volume=2000, is_closed=True)
            agg.candles_1m.append(c)
        agg.current_1m = Candle(timestamp=now, timeframe="1m", open=9800, high=10100, low=9800, close=10000, volume=10000)
        agg.vwap = 9850.0

        # 다른 셋업이 꺼져 있어도 PDH 돌파 하나만으로 독립적 신호 발행
        signals = FullStrategySuite.evaluate_intraday_all(
            sym=sym,
            agg=agg,
            regime=MarketRegime.BULL,
            now=now,
            patterns={},
            spread_ratio=0.0015
        )
        self.assertGreater(len(signals), 0)
        strat_ids = [s.strategy_id for s in signals]
        self.assertIn("INT_PDH", strat_ids)

    def test_09_hard_gate_spread_ceiling(self):
        """9. 하드게이트 스프레드 한도 0.30% (0.0030) 검증"""
        now = datetime.now()
        sym = SymbolInfo(iem_cd="TEST_SPREAD", name="스프레드테스트", market="KOSPI", price=50000, prev_high=49000, is_tradable=True)
        agg = CandleAggregator(iem_cd="TEST_SPREAD")
        agg.current_1m = Candle(timestamp=now, timeframe="1m", open=49000, high=50200, low=49000, close=50000, volume=10000)
        agg.vwap = 49500.0

        # 스프레드 0.28% (0.0028 <= 0.0030) -> 통과
        signals_ok = FullStrategySuite.evaluate_intraday_all(
            sym=sym, agg=agg, regime=MarketRegime.BULL, now=now, patterns={}, spread_ratio=0.0028
        )
        self.assertGreater(len(signals_ok), 0)

        # 스프레드 0.35% (0.0035 > 0.0030) -> 차단
        sym_wide = SymbolInfo(iem_cd="TEST_WIDE", name="와이드스프레드", market="KOSPI", price=50000, prev_high=49000, is_tradable=True)
        signals_blocked = FullStrategySuite.evaluate_intraday_all(
            sym=sym_wide, agg=agg, regime=MarketRegime.BULL, now=now, patterns={}, spread_ratio=0.0035
        )
        self.assertEqual(len(signals_blocked), 0)
        self.assertIn("SPREAD_TOO_WIDE", sym_wide.buy_block_reasons)

    def test_10_time_sync_resilience(self):
        """10. NTP 시간 동기화 및 오프라인/방화벽 시 안전 폴백(0.0ms) 검증"""
        # 유효하지 않은 도메인으로 질의 시 크래시 없이 False 반환 및 offset 0.0ms 유지
        synced = TimeSync.sync_ntp(host="invalid.ntp.local.test", timeout=0.2)
        self.assertFalse(synced)
        self.assertEqual(TimeSync.get_offset_ms(), 0.0)
        precise_time = TimeSync.get_precise_time()
        self.assertIsInstance(precise_time, datetime)


if __name__ == "__main__":
    unittest.main()
