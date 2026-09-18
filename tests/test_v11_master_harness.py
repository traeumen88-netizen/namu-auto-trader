"""[FINAL MASTER v11.0] 국내 주식 FULL-MARKET 자기학습형 통합 퀀트 자동매매 시스템 하네스 검증
- Section 90: 개발자가 반드시 수행할 15대 필수 테스트 (TEST 1 ~ TEST 15) 전수 검증
- TEST 1: 관심종목에 없는 종목에서 거래량 5배 -> 자동 탐지
- TEST 2: 3분 +2% -> 자동 Candidate
- TEST 3: 전일고가 돌파 -> 자동 Setup
- TEST 4: VWAP 눌림 후 재돌파 -> Setup
- TEST 5: Compression Breakout -> Setup
- TEST 6: Candidate → Setup 정상 승격 및 탈락 이유 기록
- TEST 7: Setup → Entry Ready (Overextension/과열 분리)
- TEST 8: Entry Ready → BUY Approved (Expected Net R >= +0.15R)
- TEST 9: BUY Approved → Order Sent
- TEST 10: Order Sent → Fill
- TEST 11: API Timeout → Reconciliation
- TEST 12: 시장 급락 → Risk Firewall
- TEST 13: ML Model Failure → Rule Engine fallback
- TEST 14: 동시에 100개 이벤트 → Priority Queue
- TEST 15: 한 번 탈락한 종목의 재급등 → Dynamic Rediscovery
"""

import unittest
from datetime import datetime, timedelta

from core.models import (
    SymbolInfo, SymbolState, SetupType, SetupInspectionItem, TradeSignal,
    TimeHorizon, OrderSide, OrderType, OrderStatus, MarketRegime, Candle, Order
)
from core.symbol_store import SymbolStateStore
from core.event_detector import EventDetector
from core.setup_detector import SetupDetector
from core.priority_queue import EventPriorityQueue, EventDeduplicator
from core.edge_engine import EdgeEngine
from core.aggregator import CandleAggregator
from core.low_cost_scanner import LowCostMarketScanner
from strategies.full_strategy_suite import FullStrategySuite
from strategies.scoring_engine import ScoringEngine
from execution.order_router import OrderRouter
from execution.diagnostic_engine import DiagnosticEngine
from risk.loss_limits import LossLimitManager


class DummyCircuitBreaker:
    is_tripped = False
    trip_reason = ""
    def check_data_staleness(self, dt): return False
    def record_order_success(self): pass
    def record_order_failure(self, err): pass


class TestV11MasterHarness(unittest.TestCase):
    def setUp(self):
        self.store = SymbolStateStore()
        self.scanner = LowCostMarketScanner(self.store)
        self.cb = DummyCircuitBreaker()
        self.order_router = OrderRouter(namu_client=None, circuit_breaker=self.cb)
        self.diagnostic = DiagnosticEngine()
        self.setup_detector = SetupDetector()
        self.edge_engine = EdgeEngine(min_expected_net_r=0.15)
        self.now = datetime(2026, 9, 7, 9, 30, 0)

    def test_01_unregistered_volume_surge_5x_detection(self):
        """TEST 1: 관심종목에 없는 종목에서 거래량 5배 폭증 -> 자동 탐지"""
        code = "T01_SYM"
        sym = SymbolInfo(iem_cd=code, name="미등록5배", market="KOSPI", price=10500, open_price=10000, is_tradable=True)
        self.store.register_symbol(sym)
        agg = self.scanner.get_aggregator(code)
        for i in range(20):
            agg.candles_1m.append(Candle(timestamp=self.now - timedelta(minutes=20-i), timeframe="1m", open=10000, high=10050, low=9950, close=10000, volume=1000, is_closed=True))
        agg.vwap = 10000.0

        state, score, _ = self.scanner.on_market_tick(iem_cd=code, price=10500, volume=50000, timestamp=self.now, execution_intensity=130.0, rank_surged=True)
        self.assertIn(state, (SymbolState.WATCH, SymbolState.ACTIVE, SymbolState.CANDIDATE))
        self.assertGreaterEqual(score, 50.0)

    def test_02_return_3m_2pct_candidate_promotion(self):
        """TEST 2: 3분 +2% 급등 -> 자동 Candidate 승격"""
        code = "T02_SYM"
        sym = SymbolInfo(iem_cd=code, name="3분급등", market="KOSDAQ", price=20500, open_price=20000, is_tradable=True)
        self.store.register_symbol(sym)
        agg = self.scanner.get_aggregator(code)
        for i in range(5):
            agg.candles_1m.append(Candle(timestamp=self.now - timedelta(minutes=5-i), timeframe="1m", open=20000, high=20100, low=19900, close=20000, volume=3000, is_closed=True))
        agg.candles_1m[-3].open = 20000
        agg.current_1m = Candle(timestamp=self.now, timeframe="1m", open=20300, high=20600, low=20300, close=20500, volume=15000)

        score, evts, pri, _ = EventDetector.evaluate_events(sym, agg, self.now)
        has_ret3m = any(e.event_type.value == "RETURN_3M_2PCT" or "3분 급등" in e.description for e in evts)
        self.assertTrue(has_ret3m)
        self.assertGreaterEqual(score, 10.0)

    def test_03_pdh_breakout_setup(self):
        """TEST 3: 전일고가 돌파 -> 자동 Setup 성립"""
        code = "T03_SYM"
        sym = SymbolInfo(iem_cd=code, name="전일고가돌파", market="KOSPI", price=72000, prev_high=70000, open_price=69000, is_tradable=True)
        self.store.register_symbol(sym)
        agg = self.scanner.get_aggregator(code)
        for i in range(5):
            agg.candles_1m.append(Candle(timestamp=self.now - timedelta(minutes=5-i), timeframe="1m", open=69500, high=70000, low=69000, close=69800, volume=5000, is_closed=True))
        agg.current_1m = Candle(timestamp=self.now, timeframe="1m", open=70500, high=72500, low=70500, close=72000, volume=25000)
        agg.vwap = 70500.0

        insps, sigs = self.setup_detector.evaluate_setups(sym, agg, MarketRegime.BULL, self.now, {})
        pdh_items = [i for i in insps if i.strategy == "PDH_BREAKOUT"]
        self.assertTrue(len(pdh_items) > 0)
        self.assertTrue(pdh_items[0].is_valid_setup)
        self.assertGreaterEqual(pdh_items[0].score, 50.0)

    def test_04_vwap_pullback_setup(self):
        """TEST 4: VWAP 눌림 후 지지 반등 -> Setup 성립"""
        code = "T04_SYM"
        sym = SymbolInfo(iem_cd=code, name="VWAP눌림", market="KOSDAQ", price=20020, open_price=19500, is_tradable=True)
        self.store.register_symbol(sym)
        agg = self.scanner.get_aggregator(code)
        agg.vwap = 20000.0
        for i in range(5):
            agg.candles_1m.append(Candle(timestamp=self.now - timedelta(minutes=5-i), timeframe="1m", open=20000, high=20100, low=19980, close=20010, volume=4000, is_closed=True))
        agg.current_1m = Candle(timestamp=self.now, timeframe="1m", open=20000, high=20050, low=19990, close=20020, volume=8000)

        insps, _ = self.setup_detector.evaluate_setups(sym, agg, MarketRegime.BULL, self.now, {})
        vwap_items = [i for i in insps if i.strategy == "VWAP_PULLBACK"]
        self.assertTrue(len(vwap_items) > 0)
        self.assertTrue(vwap_items[0].is_valid_setup)

    def test_05_compression_breakout_setup(self):
        """TEST 5: 변동성/ATR 압축 후 확장 돌파 -> Setup 성립"""
        code = "T05_SYM"
        sym = SymbolInfo(iem_cd=code, name="압축돌파", market="KOSPI", price=35500, open_price=35000, is_tradable=True)
        self.store.register_symbol(sym)
        agg = self.scanner.get_aggregator(code)
        agg.current_1m = Candle(timestamp=self.now, timeframe="1m", open=35000, high=35600, low=35000, close=35500, volume=30000)
        agg.vwap = 35100.0

        insps, _ = self.setup_detector.evaluate_setups(sym, agg, MarketRegime.BULL, self.now, {"is_compression_breakout": True})
        comp_items = [i for i in insps if i.strategy == "COMPRESSION_BREAKOUT"]
        self.assertTrue(len(comp_items) > 0)
        self.assertTrue(comp_items[0].is_valid_setup)

    def test_06_candidate_to_setup_inspector_and_reasons(self):
        """TEST 6: Candidate -> Setup 정상 승격 및 탈락 이유 실시간 기록"""
        code = "T06_FAIL"
        sym = SymbolInfo(iem_cd=code, name="탈락종목", market="KOSPI", price=50000, is_tradable=True)
        self.store.register_symbol(sym)
        agg = self.scanner.get_aggregator(code)
        agg.current_1m = Candle(timestamp=self.now, timeframe="1m", open=50000, high=50000, low=49900, close=49950, volume=100)

        insps, _ = self.setup_detector.evaluate_setups(sym, agg, MarketRegime.BULL, self.now, {})
        self.assertTrue(len(insps) > 0)
        self.assertTrue(all(not i.is_valid_setup for i in insps))
        self.assertTrue(any(i.block_reason != "" for i in insps))

    def test_07_setup_vs_entry_ready_overextended_separation(self):
        """TEST 7: Setup 성립 후 Overextended 과열 진입 차단 (Setup != Entry Ready)"""
        code_late = "T07_LATE"
        sym_late = SymbolInfo(iem_cd=code_late, name="과열종목", market="KOSPI", price=54500, open_price=50000, is_tradable=True) # +9% LATE
        self.store.register_symbol(sym_late)
        agg_late = self.scanner.get_aggregator(code_late)
        agg_late.current_1m = Candle(timestamp=self.now, timeframe="1m", open=54000, high=55000, low=54000, close=54500, volume=40000)
        agg_late.vwap = 51000.0

        insps, _ = self.setup_detector.evaluate_setups(sym_late, agg_late, MarketRegime.BULL, self.now, {"is_ignition": True})
        mom_item = [i for i in insps if i.strategy == "MOMENTUM"][0]
        # Setup은 성립하지만 과열로 Entry Ready는 False
        self.assertTrue(mom_item.is_valid_setup)
        self.assertFalse(mom_item.is_entry_ready)
        self.assertIn("OVEREXTENDED", mom_item.block_reason)

    def test_08_entry_ready_to_buy_approved_edge_engine(self):
        """TEST 8: Entry Ready -> BUY Approved 기대값(+0.15R) 수학적 통과"""
        sym = SymbolInfo(iem_cd="T08_SYM", name="기대값우수", market="KOSPI", price=50000, ask1_price=50100, is_tradable=True)
        sig = TradeSignal(
            strategy_id="INT_BREAKOUT", time_horizon=TimeHorizon.INTRADAY, iem_cd="T08_SYM",
            name="기대값우수", side=OrderSide.BUY, strategy_price=50000, stop_price=49000,
            target_1r=52000, score=85.0, reason="High Edge", timestamp=self.now
        )
        edge_res = self.edge_engine.calculate_edge(sym=sym, signal=sig)
        self.assertTrue(edge_res.is_approved)
        self.assertGreaterEqual(edge_res.expected_net_r, 0.15)

    def test_09_buy_approved_to_order_sent(self):
        """TEST 9: BUY Approved -> Order Sent 주문 생성 및 발주"""
        sig = TradeSignal(
            strategy_id="INT_BREAKOUT", time_horizon=TimeHorizon.INTRADAY, iem_cd="T09_SYM",
            name="주문발주시험", side=OrderSide.BUY, strategy_price=50000, stop_price=49000,
            target_1r=52000, score=80.0, reason="Order Sent Test", timestamp=self.now,
            order_type=OrderType.MARKET
        )
        ord_res = self.order_router.submit_order(sig, shares=10, order_price=50000)
        self.assertIsNotNone(ord_res)
        self.assertEqual(ord_res.status, OrderStatus.PENDING)

    def test_10_order_sent_to_fill(self):
        """TEST 10: Order Sent -> Fill 체결 및 상태 전이"""
        sig = TradeSignal(
            strategy_id="INT_BREAKOUT", time_horizon=TimeHorizon.INTRADAY, iem_cd="T10_SYM",
            name="체결시험", side=OrderSide.BUY, strategy_price=50000, stop_price=49000,
            target_1r=52000, score=80.0, reason="Fill Test", timestamp=self.now,
            order_type=OrderType.MARKET
        )
        ord_res = self.order_router.submit_order(sig, shares=10, order_price=50000)
        self.order_router.on_fill(ord_res.client_order_id, filled_qty=10, fill_price=50000)
        self.assertEqual(ord_res.status, OrderStatus.FILLED)
        self.assertEqual(ord_res.filled_qty, 10)

    def test_11_api_timeout_zombie_reconciliation(self):
        """TEST 11: API Timeout (>3000ms) 좀비 주문 탐지 및 계좌 정합성 복구"""
        zombie = Order(
            client_order_id="ZOMBIE_T11", iem_cd="T11_SYM", side=OrderSide.BUY,
            order_type=OrderType.MARKET, qty=20, price=70000, strategy_id="INT_BREAKOUT",
            time_horizon=TimeHorizon.INTRADAY, status=OrderStatus.PENDING,
            created_at=self.now - timedelta(seconds=5), sent_at=self.now - timedelta(seconds=5)
        )
        self.order_router.pending_orders[zombie.client_order_id] = zombie
        zombies = self.order_router.check_zombie_orders(timeout_ms=3000)
        self.assertEqual(len(zombies), 1)

        class MockReconClient:
            def get_balance(self):
                return {"cash": 5000000, "holdings": [{"iem_cd": "T11_SYM", "qty": 20, "avg_price": 70000}]}
        recon = self.order_router.reconcile_orders(client=MockReconClient())
        self.assertEqual(recon["reconciled_count"], 1)
        self.assertEqual(zombie.status, OrderStatus.FILLED)

    def test_12_market_crash_risk_firewall(self):
        """TEST 12: 시장 급락 및 일일 손실 한도 초과 시 자동매매 전면 차단(Risk Firewall)"""
        loss_mgr = LossLimitManager()
        eval_loss = loss_mgr.evaluate_loss_limits(daily_pnl_ratio=-0.035, weekly_pnl_ratio=-0.05)
        self.assertFalse(eval_loss["can_trade_intraday"])
        self.assertFalse(eval_loss["can_trade_swing"])
        self.assertEqual(eval_loss["risk_multiplier"], 0.0)

    def test_13_ml_model_failure_rule_fallback(self):
        """TEST 13: ML 모델 장애 시 무중단 Rule Fallback 확률 적용 및 매매 지속"""
        class BrokenMLModel:
            def predict(self, x): raise RuntimeError("ML Service Down")
            def predict_proba(self, x): raise RuntimeError("ML Service Down")

        sym = SymbolInfo(iem_cd="T13_SYM", name="ML대체", market="KOSPI", price=100000, is_tradable=True)
        sig = TradeSignal(
            strategy_id="INT_BREAKOUT", time_horizon=TimeHorizon.INTRADAY, iem_cd="T13_SYM",
            name="ML대체", side=OrderSide.BUY, strategy_price=100000, stop_price=98000,
            target_1r=104000, score=80.0, reason="ML Failure Test", timestamp=self.now
        )
        edge_res = self.edge_engine.calculate_edge(sym=sym, signal=sig, ml_model=BrokenMLModel())
        self.assertTrue(edge_res.is_approved)
        self.assertEqual(edge_res.p_target, 0.60)

    def test_14_event_storm_priority_queue(self):
        """TEST 14: 동시 100개 Event Storm 시 기대값/점수 기반 Priority Queue 정렬"""
        pq = EventPriorityQueue()
        for i in range(100):
            net_r = 0.10 + (i * 0.01)
            score = 50.0 + (i * 0.4)
            pq.push(item_data=f"EVENT_{i}", expected_net_r=net_r, setup_score=score)

        top_event = pq.pop()
        self.assertEqual(top_event, "EVENT_99")
        self.assertEqual(len(pq), 99)

    def test_15_demoted_stock_rediscovery(self):
        """TEST 15: 탈락 종목 새 이벤트 발생 시 Dynamic Rediscovery 자동 재승격"""
        code = "T15_SYM"
        sym = SymbolInfo(iem_cd=code, name="재급등발굴", market="KOSDAQ", price=10000, open_price=10000, is_tradable=True)
        self.store.register_symbol(sym)
        self.store.transition_state(code, SymbolState.INACTIVE, reason="이전 이벤트 종료")
        self.assertEqual(self.store.get(code).state, SymbolState.INACTIVE)

        agg = self.scanner.get_aggregator(code)
        state, score, _ = self.scanner.on_market_tick(iem_cd=code, price=10500, volume=80000, timestamp=self.now + timedelta(hours=1), rank_surged=True)
        self.assertIn(state, (SymbolState.WATCH, SymbolState.ACTIVE, SymbolState.CANDIDATE))
        self.assertNotEqual(self.store.get(code).state, SymbolState.INACTIVE)

    def test_16_master_15_test_harness_full_suite(self):
        """TEST 16: DiagnosticEngine.run_master_15_test_harness() 15대 필수 테스트 전수 실행 검증"""
        res = self.diagnostic.run_master_15_test_harness(
            scanner=self.scanner,
            full_strategy_suite=FullStrategySuite,
            scoring_engine=ScoringEngine,
            order_router=self.order_router,
            symbol_store=self.store,
            now=self.now
        )
        self.assertTrue(res["all_passed"], f"Failures: {[k for k, v in res['scenarios'].items() if not v['passed']]}")
        self.assertEqual(len(res["scenarios"]), 15)


if __name__ == "__main__":
    unittest.main()
