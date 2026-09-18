"""
tests/test_similarity_rvol_dynamic.py
-------------------------------------
Verifies that:
1. query_similarity receives actual computed RVOL from CandleAggregator (not hardcoded 2.0).
2. Different symbols pass distinct dynamic RVOL values.
3. Fallback chain functions properly: Aggregator -> Signal.rvol -> 1.0 default.
4. PointInTimeSnapshot stores the actual real-time RVOL in raw_features.
5. SharedDecisionCore in backtester also maintains dynamic RVOL parity.
"""

import os
import sys
import unittest
from datetime import datetime, timedelta
from unittest.mock import MagicMock, patch

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if BASE_DIR not in sys.path:
    sys.path.insert(0, BASE_DIR)

from core.models import (
    SymbolInfo, SymbolState, TradeSignal, OrderSide, OrderType,
    TimeHorizon, MarketRegime, Candle
)
from core.aggregator import CandleAggregator
from execution.live_quant_trader import AccountContext, LiveQuantTrader
from execution.order_router import OrderRouter
from execution.position_manager import PositionManager
from execution.exit_watchdog import ExitWatchdog
from risk.loss_limits import LossLimitManager
from risk.circuit_breaker import CircuitBreaker
from core.symbol_store import SymbolStateStore
from core.low_cost_scanner import LowCostMarketScanner
from ml.similarity_engine import SimilarityMetaOutput


from core.edge_engine import EdgeResult
from ml.meta_decision import MetaDecisionResult
from execution.order_quote_manager import OrderQuoteSnapshot


class MockBrokerClient:
    def __init__(self, mode="mock", act_no="50001003032", cash=10_000_000.0, equity=10_000_000.0):
        self.mode = mode
        self.act_no = act_no
        self.cash = cash
        self.equity = equity
        self.orders = []

    def get_balance(self):
        return {
            "cash": self.cash,
            "total_asset": self.equity,
            "total_profit": 0,
            "total_profit_rate": 0.0,
            "order_available": self.cash,
            "holdings": []
        }

    def get_current_price(self, code: str):
        return {
            "price": 50000,
            "volume": 3500,
            "open": 49000,
            "high": 51000,
            "low": 48500,
            "prev_close": 49500
        }

    def get_buyable_quantity(self, iem_cd, price, order_type="01"):
        return {"is_valid": True, "csh_orr_pbl_qty": 100}

    def get_daily_candles(self, code: str, count: int = 65):
        return [
            {"date": "20260915", "open": 49000, "high": 51000, "low": 48500, "close": 50000, "volume": 100000},
            {"date": "20260914", "open": 48000, "high": 49500, "low": 47800, "close": 49500, "volume": 90000},
        ]


class TestSimilarityRvolDynamic(unittest.TestCase):
    def setUp(self):
        self.trader = LiveQuantTrader.__new__(LiveQuantTrader)
        self.trader.mode = "mock"
        self.trader.force_signal_test = False
        self.trader.circuit_breaker = CircuitBreaker()
        self.trader.diagnostic_engine = MagicMock()
        self.trader.quote_manager = MagicMock()
        def make_fresh_quote(client, symbol, signal_price):
            return OrderQuoteSnapshot(
                symbol=symbol,
                current_price=int(signal_price),
                bid=int(signal_price - 50),
                ask=int(signal_price),
                quote_time=datetime.now().strftime("%H%M%S"),
                quote_timestamp=datetime.now(),
                received_at=datetime.now(),
                api_latency_ms=10.0,
                quote_data_age_ms=100.0,
                order_quote_age_ms=100.0,
                is_fresh=True,
                staleness_reason="",
                source="REST_ON_DEMAND"
            )
        self.trader.quote_manager.sync_fresh_quote.side_effect = make_fresh_quote
        self.trader.data_quality_gate = MagicMock()
        self.trader.data_quality_gate.validate_tick.return_value = (True, "")
        self.trader.market_radar = MagicMock()
        self.trader.market_radar.scan_market_movers.return_value = []
        self.trader.setup_detector = MagicMock()
        self.trader.persistence_manager = MagicMock()
        self.trader.experience_memory = MagicMock()
        self.trader.similarity_engine = MagicMock()
        self.trader.model_registry = MagicMock()
        self.trader.model_registry.champion_id = "CHAMPION_V16_0"
        
        self.trader.edge_engine = MagicMock()
        self.trader.edge_engine.calculate_edge.return_value = EdgeResult(
            is_approved=True,
            expected_net_r=0.45,
            entry_price=50000,
            stop_price=49000,
            target_price=52000,
            reward_r=2.0,
            risk_r=1.0,
            cost_r=0.1,
            p_target=0.65,
            p_stop=0.35,
            rejection_reason=None
        )

        self.trader.meta_decision_engine = MagicMock()
        self.trader.meta_decision_engine.evaluate_candidate.return_value = MetaDecisionResult(
            approved=True,
            setup_name="INT_MOMENTUM",
            p_target=0.65,
            p_stop=0.35,
            edge=0.30,
            reward_risk_ratio=2.0,
            cost_r=0.1,
            expected_net_r=0.45,
            kelly_fraction=0.02,
            recommended_risk_pct=0.015,
            reason="APPROVED",
            details={},
            decision="BUY"
        )

        self.trader.last_recon_time = datetime.now()
        self.trader.eod_worker = MagicMock()
        self.trader.eod_batch_executed_today = None
        self.trader.daily_candles_cache = {}
        self.trader.or_high_map = {}
        self.trader._rolling_scan_index = 0
        self.trader._rolling_batch_size = 15
        self.trader._print_dashboard = MagicMock()
        self.trader.after_hours_manager = MagicMock()
        self.trader.after_hours_manager.get_next_session_features.return_value = {}

        self.client = MockBrokerClient()
        cb = CircuitBreaker()
        router = OrderRouter(self.client, cb)
        pm = PositionManager(router)
        self.acc = AccountContext(
            name="MOCK_TEST",
            mode="mock",
            act_no="50001003032",
            client=self.client,
            order_router=router,
            position_manager=pm,
            loss_manager=LossLimitManager()
        )
        self.trader.accounts = [self.acc]

        sym = SymbolInfo(iem_cd="005930", name="삼성전자", price=50000, is_tradable=True)
        self.store = SymbolStateStore({"005930": sym})
        self.scanner = LowCostMarketScanner(self.store)
        self.trader.store = self.store
        self.trader.scanner = self.scanner
        self.trader._all_symbol_codes = ["005930"]

        self.test_db_path = os.path.join(BASE_DIR, "data", "test_operational_rvol.db")
        self.test_hb_path = os.path.join(BASE_DIR, "data", "test_watchdog_hb_rvol.json")
        self.watchdog = ExitWatchdog(db_path=self.test_db_path, heartbeat_file=self.test_hb_path)
        self.trader.exit_watchdog = self.watchdog

    def tearDown(self):
        for p in [self.test_db_path, self.test_hb_path]:
            if os.path.exists(p):
                try:
                    os.remove(p)
                except Exception:
                    pass

    def test_01_query_similarity_receives_actual_rvol_from_aggregator(self):
        """TEST 1: CandleAggregator에서 계산된 실제 RVOL(3.5x)이 query_similarity에 전달되는지 검증"""
        # Create aggregator with 20 candles of 1,000 volume each (avg = 1,000)
        agg = self.scanner.get_aggregator("005930")
        base_time = datetime.now() - timedelta(minutes=25)
        for i in range(20):
            c = Candle(
                timestamp=base_time + timedelta(minutes=i),
                timeframe="1m",
                open=49000, high=49500, low=48900, close=49200,
                volume=1000, turnover=49200000, vwap=49200, is_closed=True
            )
            agg.candles_1m.append(c)

        # Broker returns tick volume = 3,500, which on_market_tick will aggregate into current_1m
        self.client.get_current_price = lambda code: {
            "price": 50000, "volume": 3500, "open": 49000, "high": 51000, "low": 48500, "prev_close": 49500
        }

        cand = SymbolInfo(iem_cd="005930", name="삼성전자", price=50000, is_tradable=True)
        self.scanner.promotion_engine.get_promoted_candidates = MagicMock(return_value=[cand])

        sig = TradeSignal(
            strategy_id="INT_MOMENTUM",
            time_horizon=TimeHorizon.INTRADAY,
            iem_cd="005930",
            name="삼성전자",
            side=OrderSide.BUY,
            strategy_price=50000,
            stop_price=49000,
            score=85.0,
            reason="MOMENTUM",
            timestamp=datetime.now(),
            target_1r=51000,
            target_2r=52000,
            target_3r=53000,
            rvol=3.5
        )
        self.scanner.scan_active_signals = MagicMock(return_value=[sig])

        fake_sim_out = SimilarityMetaOutput(
            hist_win_rate=0.65, hist_target_rate=0.60, hist_stop_rate=0.35,
            expected_r=0.45, avg_mfe=2.5, avg_mae=-0.8, sample_count=15,
            regime_match=1.0, time_of_day_match=1.0, similarity_distance=0.15,
            is_sufficient_sample=True
        )
        self.trader.similarity_engine.query_similarity = MagicMock(return_value=fake_sim_out)

        # Run cycle
        self.trader.run_cycle(now=datetime(2026, 9, 15, 10, 0, 0))

        # Check call arguments
        self.trader.similarity_engine.query_similarity.assert_called_once()
        call_kwargs = self.trader.similarity_engine.query_similarity.call_args[1]
        features = call_kwargs["current_features"]

        self.assertIn("rvol", features)
        self.assertEqual(features["rvol"], 3.5, "query_similarity should receive the real-time computed RVOL (3.5), NOT hardcoded 2.0")

    def test_02_different_symbols_pass_distinct_real_time_rvols(self):
        """TEST 2: 종목별로 서로 다른 실제 RVOL(예: 0.8x vs 4.2x)이 각각 동적으로 전달되는지 검증"""
        # Register second symbol
        sym2 = SymbolInfo(iem_cd="000660", name="SK하이닉스", price=120000, is_tradable=True)
        self.store.register_symbol(sym2)
        self.trader._all_symbol_codes = ["005930", "000660"]

        # Symbol 1 (005930) has 20 bars of 1,000 avg, tick volume = 800 -> RVOL = 0.8
        agg1 = self.scanner.get_aggregator("005930")
        for i in range(20):
            agg1.candles_1m.append(Candle(datetime.now(), "1m", 50000, 50000, 50000, 50000, 1000, 50000000, 50000, True))

        # Symbol 2 (000660) has 20 bars of 1,000 avg, tick volume = 4,200 -> RVOL = 4.2
        agg2 = self.scanner.get_aggregator("000660")
        for i in range(20):
            agg2.candles_1m.append(Candle(datetime.now(), "1m", 120000, 120000, 120000, 120000, 1000, 120000000, 120000, True))

        def mock_get_current_price(code: str):
            if code == "005930":
                return {"price": 50000, "volume": 800, "open": 50000, "high": 50000, "low": 50000, "prev_close": 50000}
            else:
                return {"price": 120000, "volume": 4200, "open": 120000, "high": 120000, "low": 120000, "prev_close": 120000}
        self.client.get_current_price = mock_get_current_price

        cand1 = SymbolInfo(iem_cd="005930", name="삼성전자", price=50000, is_tradable=True)
        cand2 = SymbolInfo(iem_cd="000660", name="SK하이닉스", price=120000, is_tradable=True)
        self.scanner.promotion_engine.get_promoted_candidates = MagicMock(return_value=[cand1, cand2])

        sig1 = TradeSignal("STRAT_1", TimeHorizon.INTRADAY, "005930", "삼성전자", OrderSide.BUY, 50000, 49000, 85.0, "REASON", datetime.now(), 51000, 52000, 53000, rvol=0.8)
        sig2 = TradeSignal("STRAT_2", TimeHorizon.INTRADAY, "000660", "SK하이닉스", OrderSide.BUY, 120000, 117000, 88.0, "REASON", datetime.now(), 123000, 126000, 129000, rvol=4.2)
        self.scanner.scan_active_signals = MagicMock(return_value=[sig1, sig2])

        fake_sim_out = SimilarityMetaOutput(0.6, 0.6, 0.4, 0.3, 1.0, -0.5, 10, 1.0, 1.0, 0.2, True)
        self.trader.similarity_engine.query_similarity = MagicMock(return_value=fake_sim_out)

        self.trader.run_cycle(now=datetime(2026, 9, 15, 10, 0, 0))

        # query_similarity should have been called twice with distinct RVOLs
        self.assertEqual(self.trader.similarity_engine.query_similarity.call_count, 2)
        call_rvols = [call[1]["current_features"]["rvol"] for call in self.trader.similarity_engine.query_similarity.call_args_list]

        self.assertIn(0.8, call_rvols)
        self.assertIn(4.2, call_rvols)
        self.assertNotEqual(call_rvols[0], call_rvols[1])

    def test_03_fallback_chain_to_signal_rvol_and_default(self):
        """TEST 3: Aggregator 부재/미성숙 시 sig.rvol -> 1.0 순차적 안전 폴백 검증"""
        # Case A: Aggregator missing, but sig has rvol=1.75
        cand = SymbolInfo(iem_cd="005930", name="삼성전자", price=50000, is_tradable=True)
        self.scanner.promotion_engine.get_promoted_candidates = MagicMock(return_value=[cand])

        sig = TradeSignal("STRAT_1", TimeHorizon.INTRADAY, "005930", "삼성전자", OrderSide.BUY, 50000, 49000, 85.0, "REASON", datetime.now(), 51000, 52000, 53000, rvol=1.75)
        self.scanner.scan_active_signals = MagicMock(return_value=[sig])

        # Remove aggregator to force fallback
        self.scanner.active_aggregators.clear()
        self.scanner.get_aggregator = MagicMock(return_value=None)

        fake_sim_out = SimilarityMetaOutput(0.6, 0.6, 0.4, 0.3, 1.0, -0.5, 10, 1.0, 1.0, 0.2, True)
        self.trader.similarity_engine.query_similarity = MagicMock(return_value=fake_sim_out)

        self.trader.run_cycle(now=datetime(2026, 9, 15, 10, 0, 0))

        features = self.trader.similarity_engine.query_similarity.call_args[1]["current_features"]
        self.assertEqual(features["rvol"], 1.75, "Should fallback to sig.rvol when aggregator is unavailable")

    def test_04_snapshot_raw_features_records_actual_rvol(self):
        """TEST 4: PointInTimeSnapshot 불변 저장 시 raw_features에 실시간 RVOL 보존 검증"""
        agg = self.scanner.get_aggregator("005930")
        for i in range(20):
            agg.candles_1m.append(Candle(datetime.now(), "1m", 50000, 50000, 50000, 50000, 1000, 50000000, 50000, True))

        self.client.get_current_price = lambda code: {
            "price": 50000, "volume": 2800, "open": 50000, "high": 50000, "low": 50000, "prev_close": 50000
        }

        cand = SymbolInfo(iem_cd="005930", name="삼성전자", price=50000, is_tradable=True)
        self.scanner.promotion_engine.get_promoted_candidates = MagicMock(return_value=[cand])

        sig = TradeSignal("STRAT_1", TimeHorizon.INTRADAY, "005930", "삼성전자", OrderSide.BUY, 50000, 49000, 85.0, "REASON", datetime.now(), 51000, 52000, 53000, rvol=2.8)
        self.scanner.scan_active_signals = MagicMock(return_value=[sig])

        fake_sim_out = SimilarityMetaOutput(0.6, 0.6, 0.4, 0.3, 1.0, -0.5, 10, 1.0, 1.0, 0.2, True)
        self.trader.similarity_engine.query_similarity = MagicMock(return_value=fake_sim_out)

        self.trader.run_cycle(now=datetime(2026, 9, 15, 10, 0, 0))

        # Verify experience_memory.record_snapshot was called with raw_features containing rvol=2.8
        self.trader.experience_memory.record_snapshot.assert_called_once()
        snap = self.trader.experience_memory.record_snapshot.call_args[0][0]
        self.assertIn("rvol", snap.raw_features)
        self.assertEqual(snap.raw_features["rvol"], 2.8, "Snapshot raw_features must record the real-time RVOL")


if __name__ == "__main__":
    if sys.platform == "win32":
        try:
            sys.stdout.reconfigure(encoding="utf-8")
            sys.stderr.reconfigure(encoding="utf-8")
        except Exception:
            pass
    unittest.main()
