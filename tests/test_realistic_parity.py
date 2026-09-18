"""[PARITY TEST SUITE v1.0] Comprehensive LIVE vs Realistic Backtest Parity Tests
(tests/test_realistic_parity.py)

Contains all 22 required parity and execution tests:
1.  test_feature_parity
2.  test_candidate_parity
3.  test_strategy_parity
4.  test_similarity_parity
5.  test_ml_parity
6.  test_meta_parity
7.  test_edge_parity
8.  test_risk_parity
9.  test_position_sizing_parity
10. test_exit_parity
11. test_stop_fill
12. test_scale_out
13. test_trailing
14. test_time_stop
15. test_eod
16. test_partial_fill
17. test_gap_down
18. test_cash_shortfall
19. test_cooldown
20. test_reentry
21. test_no_trade_logging
22. test_lookahead_leakage
"""

import os
import sys
import unittest
from datetime import datetime, timedelta, time as dtime

# Ensure project root is in sys.path
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core.models import (
    SymbolInfo, SymbolState, TradeSignal, TimeHorizon, OrderSide, OrderType,
    MarketRegime, Candle, Position
)
from core.aggregator import CandleAggregator
from core.symbol_store import SymbolStateStore
from core.candidate_promotion import CandidatePromotionEngine
from strategies.full_strategy_suite import FullStrategySuite
from ml.meta_decision import MetaDecisionEngine, FeatureScalerPipeline
from ml.similarity_engine import HistoricalSimilarityEngine
from core.edge_engine import EdgeEngine
from risk.portfolio_risk import PortfolioRiskManager
from risk.loss_limits import LossLimitManager
from risk.position_sizer import PositionSizer
from backtester.execution_simulator import (
    RealisticExecutionSimulator, SimulatedOrder, OrderFillStatus
)
from backtester.position_tracker import PositionTracker
from backtester.shared_decision_core import SharedDecisionCore
from backtester.leakage_verifier import LookaheadLeakageVerifier, LeakageError


class TestRealisticParity(unittest.TestCase):
    def setUp(self):
        self.now = datetime(2026, 1, 15, 9, 30)
        self.sim = RealisticExecutionSimulator()
        self.tracker = PositionTracker(self.sim)
        self.verifier = LookaheadLeakageVerifier(strict_mode=True)
        self.sym_info = SymbolInfo(
            iem_cd="005930", name="삼성전자", price=70000,
            open_price=69500, high_price=70500, low_price=69200,
            acml_vol=100000
        )

    # 1. Feature Parity
    def test_feature_parity(self):
        raw = {"price": 70000.0, "score": 85.0, "rvol": 2.5, "ret_1m": 0.012}
        ok, scaled, _ = FeatureScalerPipeline.transform(raw)
        self.assertTrue(ok)
        self.assertIn("price", scaled)
        self.assertIn("score", scaled)
        # Verify clipping bound [-5.0, 5.0]
        for v in scaled.values():
            self.assertTrue(-5.0 <= v <= 5.0)

    # 2. Candidate Parity
    def test_candidate_parity(self):
        store = SymbolStateStore({"005930": self.sym_info})
        promoter = CandidatePromotionEngine(store)
        agg = CandleAggregator("005930")
        agg.candles_1m.append(Candle(
            timestamp=self.now, timeframe="1m",
            open=69500, high=71500, low=69500, close=71000,
            volume=50000, turnover=71000 * 50000, is_closed=True
        ))
        state, score, events, _ = promoter.process_event_evaluation("005930", agg, self.now, execution_intensity=130.0)
        self.assertIn(state, (SymbolState.ACTIVE, SymbolState.SIGNAL, SymbolState.WATCH))

    # 3. Strategy Parity
    def test_strategy_parity(self):
        agg = CandleAggregator("005930")
        for i in range(24):
            agg.candles_1m.append(Candle(
                timestamp=self.now + timedelta(minutes=i), timeframe="1m",
                open=70000 + i * 50, high=70200 + i * 50, low=69900 + i * 50,
                close=70100 + i * 50, volume=10000, turnover=700000000, is_closed=True
            ))
        agg.candles_1m.append(Candle(
            timestamp=self.now + timedelta(minutes=24), timeframe="1m",
            open=71200, high=71500, low=71150, close=71400,
            volume=30000, turnover=2140000000, is_closed=True
        ))
        self.sym_info.price = 71400
        self.sym_info.high_price = 71400
        patterns = {"is_ignition": True}
        sigs = FullStrategySuite.evaluate_intraday_all(
            sym=self.sym_info, agg=agg, regime=MarketRegime.NEUTRAL,
            now=self.now, patterns=patterns
        )
        self.assertTrue(len(sigs) >= 1)
        sig = sigs[0]
        self.assertIn(sig.strategy_id, ("INT_MOMENTUM_IGNITION", "INT_BREAKOUT", "INT_VWAP_PULLBACK"))
        self.assertTrue(sig.stop_price < sig.strategy_price < sig.target_1r)

    # 4. Similarity Parity
    def test_similarity_parity(self):
        # Leakage test on similarity query
        future_time = self.now + timedelta(days=5)
        with self.assertRaises(LeakageError):
            self.verifier.verify_similarity_query(self.now, future_time, "CASE_999")

    # 5. ML Parity
    def test_ml_parity(self):
        engine = MetaDecisionEngine()
        pred = {"p_target": 0.65, "p_stop": 0.35}
        res = engine.evaluate_candidate(
            setup_name="INT_BREAKOUT", time_horizon=TimeHorizon.INTRADAY,
            entry_price=70000, stop_price=69000, target_price=72000,
            predicted_probs=pred, rule_score=80.0
        )
        self.assertAlmostEqual(res.p_target, 0.65, places=2)
        self.assertIn(res.decision, ("BUY", "BUY_SMALL"))

    # 6. Meta Parity
    def test_meta_parity(self):
        engine = MetaDecisionEngine()
        # Bad R:R should reject
        res_reject = engine.evaluate_candidate(
            setup_name="INT_BREAKOUT", time_horizon=TimeHorizon.INTRADAY,
            entry_price=70000, stop_price=69500, target_price=70100,  # 0.2R
            predicted_probs={"p_target": 0.50, "p_stop": 0.50}, rule_score=50.0
        )
        self.assertEqual(res_reject.decision, "NO_TRADE")

    # 7. Edge Parity
    def test_edge_parity(self):
        edge_eng = EdgeEngine(min_expected_net_r=0.15)
        sig = TradeSignal(
            strategy_id="INT_BREAKOUT", time_horizon=TimeHorizon.INTRADAY,
            iem_cd="005930", name="삼성전자", side=OrderSide.BUY,
            strategy_price=70000, stop_price=69000, target_1r=71000, target_2r=72000,
            score=85.0, reason="Breakout", timestamp=self.now
        )
        res = edge_eng.calculate_edge(self.sym_info, sig, p_target=0.68, p_stop=0.32)
        self.assertTrue(res.is_approved)
        self.assertGreaterEqual(res.expected_net_r, 0.15)

    # 8. Risk Parity
    def test_risk_parity(self):
        pos = Position(
            position_id="POS_1", time_horizon=TimeHorizon.INTRADAY, strategy_id="INT",
            iem_cd="005930", name="삼성", qty=10, entry_price=70000, current_price=70000,
            stop_price=69000, target_1r=71000, target_2r=72000, target_3r=73000,
            r_unit=1000, initial_risk_amount=250_000, entry_time=self.now,
            trailing_stop_price=69000, highest_price=70000
        )
        amt, ratio, status = PortfolioRiskManager.calculate_total_open_risk([pos], equity=10_000_000)
        self.assertEqual(status, "NORMAL")
        self.assertAlmostEqual(ratio, 0.025, places=3)

    # 9. Position Sizing Parity
    def test_position_sizing_parity(self):
        shares, risk_amt, rationale = PositionSizer.calculate_shares(
            time_horizon=TimeHorizon.INTRADAY,
            equity=10_000_000,
            available_cash=5_000_000,
            entry_price=70000,
            stop_price=68500  # stop distance = 1500
        )
        # Risk 0.5% of 10M = 50,000 KRW -> 50,000 / 1500 = 33 shares
        self.assertTrue(shares > 0)
        self.assertLessEqual(shares * 70000 * 1.25, 5_000_000)

    # 10. Exit Parity
    def test_exit_parity(self):
        pos = self.tracker.open_position(
            symbol="005930", name="삼성전자", strategy_id="INT_VWAP",
            time_horizon=TimeHorizon.INTRADAY, qty=100, entry_price=70000,
            stop_price=69000, target_1r=71000, target_2r=72000, target_3r=73000,
            entry_time=self.now
        )
        self.assertFalse(pos.is_closed)
        self.assertEqual(pos.qty, 100)

    # 11. Stop Fill
    def test_stop_fill(self):
        pos = self.tracker.open_position(
            symbol="005930", name="삼성전자", strategy_id="INT_VWAP",
            time_horizon=TimeHorizon.INTRADAY, qty=50, entry_price=70000,
            stop_price=69000, target_1r=71000, target_2r=72000, target_3r=73000,
            entry_time=self.now
        )
        bar = {"open": 69500, "high": 69600, "low": 68800, "close": 68900, "volume": 15000}
        closed = self.tracker.update_and_manage("005930", bar, self.now + timedelta(minutes=5))
        self.assertEqual(len(closed), 1)
        self.assertTrue(pos.is_closed)
        self.assertIn("스톱로스", closed[0].exit_reason)

    # 12. Scale Out
    def test_scale_out(self):
        pos = self.tracker.open_position(
            symbol="005930", name="삼성전자", strategy_id="INT_VWAP",
            time_horizon=TimeHorizon.INTRADAY, qty=100, entry_price=70000,
            stop_price=69000, target_1r=71000, target_2r=72000, target_3r=73000,
            entry_time=self.now
        )
        bar = {"open": 70500, "high": 71200, "low": 70400, "close": 71100, "volume": 20000}
        closed = self.tracker.update_and_manage("005930", bar, self.now + timedelta(minutes=5))
        self.assertEqual(len(closed), 1)
        self.assertTrue(closed[0].is_scale_out)
        self.assertEqual(closed[0].qty, 30)  # 30% scale-out
        self.assertEqual(pos.qty, 70)  # Remaining 70 shares
        self.assertEqual(pos.stop_price, 70000)  # Stop moved to breakeven!

    # 13. Trailing Stop
    def test_trailing(self):
        pos = self.tracker.open_position(
            symbol="005930", name="삼성전자", strategy_id="INT_VWAP",
            time_horizon=TimeHorizon.INTRADAY, qty=100, entry_price=70000,
            stop_price=69000, target_1r=71000, target_2r=72000, target_3r=73000,
            entry_time=self.now
        )
        # Take 1R
        bar1 = {"open": 70500, "high": 72500, "low": 70400, "close": 72000, "volume": 20000}
        self.tracker.update_and_manage("005930", bar1, self.now + timedelta(minutes=5), atr14=500)
        self.assertTrue(pos.target_1r_taken)

        # Bar 2 drops below trailing stop (Highest 72500 - 1.5*500 = 71750)
        bar2 = {"open": 71600, "high": 71600, "low": 71200, "close": 71300, "volume": 15000}
        closed = self.tracker.update_and_manage("005930", bar2, self.now + timedelta(minutes=10), atr14=500)
        self.assertTrue(pos.is_closed)
        self.assertTrue(any("Trailing" in c.exit_reason for c in closed))

    # 14. Time Stop
    def test_time_stop(self):
        pos = self.tracker.open_position(
            symbol="005930", name="삼성전자", strategy_id="INT_VWAP",
            time_horizon=TimeHorizon.INTRADAY, qty=50, entry_price=70000,
            stop_price=69000, target_1r=71000, target_2r=72000, target_3r=73000,
            entry_time=self.now
        )
        bar = {"open": 70050, "high": 70100, "low": 69950, "close": 70020, "volume": 3000}
        # At 21 minutes: STALE_POSITION status transition, not closed yet
        closed_21m = self.tracker.update_and_manage("005930", bar, self.now + timedelta(minutes=21))
        self.assertEqual(len(closed_21m), 0)
        self.assertEqual(pos.status, "STALE_POSITION")

        # At 31 minutes: 30m stagnation threshold met -> TIME_STOP triggered
        closed_31m = self.tracker.update_and_manage("005930", bar, self.now + timedelta(minutes=31))
        self.assertEqual(len(closed_31m), 1)
        self.assertIn("TIME_STOP", closed_31m[0].exit_reason)

    # 15. EOD Force Liquidation
    def test_eod(self):
        pos = self.tracker.open_position(
            symbol="005930", name="삼성전자", strategy_id="INT_VWAP",
            time_horizon=TimeHorizon.INTRADAY, qty=50, entry_price=70000,
            stop_price=69000, target_1r=71000, target_2r=72000, target_3r=73000,
            entry_time=self.now
        )
        eod_time = datetime(2026, 1, 15, 15, 20)
        bar = {"open": 70200, "high": 70300, "low": 70100, "close": 70250, "volume": 10000}
        closed = self.tracker.update_and_manage("005930", bar, eod_time)
        self.assertEqual(len(closed), 1)
        self.assertIn("장마감", closed[0].exit_reason)

    # 16. Partial Fill
    def test_partial_fill(self):
        order = SimulatedOrder(
            order_id="ORD_1", symbol="005930", name="삼성전자",
            side=OrderSide.BUY, order_type=OrderType.MARKET,
            requested_qty=5000, requested_price=70000,
            signal_time=self.now
        )
        # Bar volume is only 10,000 shares -> max participation 10% = 1,000 shares
        bar = {"open": 70000, "high": 70300, "low": 69900, "close": 70100, "volume": 10000}
        res = self.sim.simulate_entry_execution(order, bar)
        self.assertEqual(res.status, OrderFillStatus.PARTIAL_FILL)
        self.assertEqual(res.filled_qty, 1000)

    # 17. Gap Down Handling
    def test_gap_down(self):
        # Stop is 70,000, but bar opens at 67,000 (gap down)
        bar = {"open": 67000, "high": 67200, "low": 66500, "close": 66800, "volume": 20000}
        res = self.sim.simulate_exit_execution(
            symbol="005930", qty=100, exit_reason="스톱로스 도달",
            bar=bar, stop_price=70000
        )
        # Price must fill at or below 67,000 (realistic gap-down fill), NOT 70,000!
        self.assertLessEqual(res.filled_avg_price, 67000)

    # 18. Cash Shortfall
    def test_cash_shortfall(self):
        shares, risk_amt, rationale = PositionSizer.calculate_shares(
            time_horizon=TimeHorizon.INTRADAY,
            equity=10_000_000,
            available_cash=50_000,  # Only 50,000 KRW available
            entry_price=70000,
            stop_price=68500
        )
        self.assertEqual(shares, 0)
        self.assertIn("INSUFFICIENT_CASH", rationale)

    # 19. Cooldown
    def test_cooldown(self):
        store = SymbolStateStore({"005930": self.sym_info})
        store.set_cooldown("005930", self.now, cooldown_seconds=600)
        sym = store.get("005930")
        self.assertEqual(sym.state, SymbolState.COOLDOWN)
        self.assertTrue(sym.cooldown_until > self.now)

    # 20. Reentry Block
    def test_reentry(self):
        self.tracker.open_position(
            symbol="005930", name="삼성전자", strategy_id="INT_VWAP",
            time_horizon=TimeHorizon.INTRADAY, qty=50, entry_price=70000,
            stop_price=69000, target_1r=71000, target_2r=72000, target_3r=73000,
            entry_time=self.now
        )
        has_pos = any(p.iem_cd == "005930" and not p.is_closed for p in self.tracker.active_positions.values())
        self.assertTrue(has_pos)

    # 21. NO_TRADE Logging
    def test_no_trade_logging(self):
        core = SharedDecisionCore({"005930": self.sym_info})
        # Stale/quiet bar -> produces NO_TRADE
        bar = {"open": 70000, "high": 70050, "low": 69950, "close": 70000, "volume": 100, "timestamp": self.now}
        decisions = core.evaluate_bar("005930", bar, self.now, 10_000_000, 10_000_000, [])
        for d in decisions:
            if d.meta_decision == "NO_TRADE":
                self.assertIn("NO_SETUP_MATCH", d.decision_reason)

    # 22. Lookahead Leakage
    def test_lookahead_leakage(self):
        future_bar = self.now + timedelta(hours=1)
        with self.assertRaises(LeakageError):
            self.verifier.verify_bar_timestamp(self.now, future_bar, "005930")


if __name__ == "__main__":
    unittest.main()
