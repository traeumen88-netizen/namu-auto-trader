"""Breakout Safety Gate Comprehensive Test Suite
(tests/test_breakout_safety_gate.py)

Verifies the 8 critical safety conditions confirmed during 2026-09-16 LIVE audit:
Test 1: SWG_HH60_BREAKOUT with RVOL < 1.5 -> Reject
Test 2: SWG_HH20_BREAKOUT with RVOL < 1.5 -> Reject
Test 3: SWG_HH60 daily red bar (today_ret < 0, falling knife) -> Reject
Test 4: INT_COMPRESSION_BREAKOUT in morning (10~40 candles) without volume expansion -> Reject
Test 5: Negative 3m momentum (momentum_3m_pct < 0) -> Reject
Test 6: Valid green candle + RVOL >= 1.5 + positive 3m momentum + near high -> Approved
Test 7: RVOL None / NaN / 0 -> Reject
Test 8: is_acceleration=True cannot bypass validate_breakout_entry
"""

import sys
import os
import math
import unittest
from datetime import datetime, timedelta

# Add project root to sys.path
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core.models import SymbolInfo, Candle, MarketRegime, SetupType
from core.aggregator import CandleAggregator
from core.event_detector import EventDetector
from core.setup_detector import SetupDetector
from strategies.breakout_gate import validate_breakout_entry
from strategies.full_strategy_suite import FullStrategySuite
from config.settings import BREAKOUT_MIN_RVOL


class TestBreakoutSafetyGate(unittest.TestCase):
    def setUp(self):
        self.now = datetime(2026, 9, 16, 10, 30, 0)

    def _create_agg_with_candles(self, prices_and_vols, symbol="000000"):
        """Helper to create aggregator populated with 1m candles"""
        agg = CandleAggregator(symbol)
        base_time = self.now - timedelta(minutes=len(prices_and_vols) + 1)
        for i, (p, v) in enumerate(prices_and_vols):
            c_time = base_time + timedelta(minutes=i)
            c = Candle(
                timestamp=c_time,
                timeframe="1m",
                open=int(p),
                high=int(p * 1.002),
                low=int(p * 0.998),
                close=int(p),
                volume=int(v),
                turnover=int(p * v)
            )
            agg.candles_1m.append(c)
        if agg.candles_1m:
            last = agg.candles_1m[-1]
            agg.current_1m = Candle(
                timestamp=self.now,
                timeframe="1m",
                open=last.open,
                high=last.high,
                low=last.low,
                close=last.close,
                volume=last.volume,
                turnover=last.turnover
            )
        return agg

    def test_1_swg_hh60_breakout_low_rvol_reject(self):
        """Test 1: SWG_HH60_BREAKOUT with RVOL < 1.5 -> Reject"""
        sym = SymbolInfo(
            iem_cd="000001",
            name="이뮨온시아모방",
            market="KOSDAQ",
            price=11000,
            open_price=10800,
            high_price=11000,
            low_price=10500,
            prev_close=10700,
            is_tradable=True
        )
        # Today is closes[0] = 11000, past 60 days had max 10500
        daily_candles = [{"open": 10800, "high": 11000, "low": 10500, "close": 11000, "volume": 50000}]
        for _ in range(65):
            daily_candles.append({"open": 10000, "high": 10500, "low": 9500, "close": 10000, "volume": 50000})

        agg = self._create_agg_with_candles([
            (10900, 100), (10950, 100), (10980, 100), (11000, 100)
        ], symbol="000001")
        # Low RVOL = 0.45 (< 1.5)
        agg.calculate_rvol = lambda timeframe="1m", lookback=20: 0.45

        signals = FullStrategySuite.evaluate_swing_all(
            sym=sym,
            daily_candles=daily_candles,
            regime=MarketRegime.BULL,
            now=self.now,
            agg=agg
        )
        hh60_signals = [s for s in signals if s.strategy_id == "SWG_HH60_BREAKOUT"]
        self.assertEqual(len(hh60_signals), 0, "SWG_HH60_BREAKOUT must be rejected when RVOL < 1.5")

    def test_2_swg_hh20_breakout_low_rvol_reject(self):
        """Test 2: SWG_HH20_BREAKOUT with RVOL < 1.5 -> Reject"""
        sym = SymbolInfo(
            iem_cd="000002",
            name="한텍모방",
            market="KOSDAQ",
            price=11000,
            open_price=10800,
            high_price=11000,
            low_price=10500,
            prev_close=10700,
            is_tradable=True
        )
        # 20-day breakout setup (len 65 to pass minimum daily candle count 60)
        daily_candles = [{"open": 10800, "high": 11000, "low": 10500, "close": 11000, "volume": 50000}]
        for _ in range(65):
            daily_candles.append({"open": 10000, "high": 10500, "low": 9500, "close": 10000, "volume": 50000})

        agg = self._create_agg_with_candles([
            (10900, 100), (10950, 100), (10980, 100), (11000, 100)
        ], symbol="000002")
        # Low RVOL = 1.1 (< 1.5)
        agg.calculate_rvol = lambda timeframe="1m", lookback=20: 1.1

        signals = FullStrategySuite.evaluate_swing_all(
            sym=sym,
            daily_candles=daily_candles,
            regime=MarketRegime.BULL,
            now=self.now,
            agg=agg
        )
        hh20_signals = [s for s in signals if s.strategy_id == "SWG_HH20_BREAKOUT"]
        self.assertEqual(len(hh20_signals), 0, "SWG_HH20_BREAKOUT must be rejected when RVOL < 1.5")

    def test_3_swg_hh60_daily_red_bar_reject(self):
        """Test 3: SWG_HH60 daily red bar (today_ret < 0, e.g. -5.6% falling knife) -> Reject"""
        sym = SymbolInfo(
            iem_cd="000003",
            name="이뮨온시아음봉",
            market="KOSDAQ",
            price=10850,
            open_price=11500,  # Open higher, now falling -> Red bar -5.65%
            high_price=11600,
            low_price=10700,
            prev_close=11000,
            is_tradable=True
        )
        # Today opened at 11500, currently 10850 (-5.65%). 60-day prior high was 10500.
        daily_candles = [{"open": 11500, "high": 11600, "low": 10700, "close": 10850, "volume": 100000}]
        for _ in range(65):
            daily_candles.append({"open": 10000, "high": 10500, "low": 9500, "close": 10000, "volume": 50000})

        agg = self._create_agg_with_candles([
            (11000, 1000), (10950, 1000), (10900, 1000), (10850, 1000)
        ], symbol="000003")
        # Even if RVOL is high!
        agg.calculate_rvol = lambda timeframe="1m", lookback=20: 2.5

        signals = FullStrategySuite.evaluate_swing_all(
            sym=sym,
            daily_candles=daily_candles,
            regime=MarketRegime.BULL,
            now=self.now,
            agg=agg
        )
        hh60_signals = [s for s in signals if s.strategy_id == "SWG_HH60_BREAKOUT"]
        self.assertEqual(len(hh60_signals), 0, "SWG_HH60_BREAKOUT must reject daily red candle (falling knife)")

    def test_4_compression_breakout_morning_no_volume_reject(self):
        """Test 4: INT_COMPRESSION_BREAKOUT in morning (10~40 candles) without volume expansion -> Reject"""
        agg = CandleAggregator("000004")
        base_time = self.now - timedelta(minutes=25)
        for i in range(25):
            c_time = base_time + timedelta(minutes=i)
            c = Candle(
                timestamp=c_time,
                timeframe="1m",
                open=10000,
                high=10020,
                low=9980,
                close=10010,
                volume=100,
                turnover=1000000
            )
            agg.candles_1m.append(c)

        # 26th candle breaks out slightly to 10050, but volume is only 110 (no expansion!)
        last_candle = Candle(
            timestamp=self.now,
            timeframe="1m",
            open=10010,
            high=10050,
            low=10005,
            close=10050,
            volume=110,
            turnover=1100000
        )
        agg.candles_1m.append(last_candle)
        agg.calculate_rvol = lambda timeframe="1m", lookback=20: 0.8  # RVOL low

        sym = SymbolInfo(iem_cd="000004", name="압축테스트", market="KOSDAQ", price=10050, open_price=10000, high_price=10050, is_tradable=True)
        _, _, _, patterns = EventDetector.evaluate_events(sym=sym, agg=agg, now=self.now)
        self.assertFalse(patterns.get("is_compression_breakout", False),
                         "Compression breakout in morning without volume surge must return False")

    def test_5_negative_3m_momentum_reject(self):
        """Test 5: Negative 3m momentum (momentum_3m_pct < 0) -> Reject"""
        passed, reason, _ = validate_breakout_entry(
            curr_price=10000.0,
            breakout_threshold_price=9950.0,
            rvol=2.0,
            current_1m_open=9990.0,   # 1m is green (+0.1%)
            price_3m_ago=10100.0,     # 3m ago was higher -> 3m momentum is -0.99%
            high_price=10020.0,
            today_open=9800.0
        )
        self.assertFalse(passed, "Negative 3m momentum must be rejected")
        self.assertIn("NEGATIVE_MOMENTUM_3M", reason)

    def test_6_valid_breakout_approved(self):
        """Test 6: Valid green candle + RVOL >= 1.5 + positive 3m momentum + near high -> Approved"""
        passed, reason, _ = validate_breakout_entry(
            curr_price=10500.0,
            breakout_threshold_price=10400.0,
            rvol=2.8,                 # High RVOL
            current_1m_open=10450.0,  # 1m is green (+0.48%)
            price_3m_ago=10300.0,     # 3m is positive (+1.94%)
            high_price=10520.0,       # Retrace only 0.19% (< 2.0%)
            today_open=10000.0,       # Daily green (+5.0%)
            min_rvol=1.5
        )
        self.assertTrue(passed, f"Valid breakout should be approved, got reason: {reason}")
        self.assertEqual(reason, "BREAKOUT_APPROVED")

    def test_7_rvol_none_nan_zero_reject(self):
        """Test 7: RVOL None / NaN / 0 -> Reject"""
        # Test None
        p1, r1, _ = validate_breakout_entry(10000, 9950, None, 9900, 9850, 10000, 1.5)
        self.assertFalse(p1)
        self.assertIn("INVALID_RVOL", r1)

        # Test NaN
        p2, r2, _ = validate_breakout_entry(10000, 9950, float("nan"), 9900, 9850, 10000, 1.5)
        self.assertFalse(p2)
        self.assertIn("INVALID_RVOL", r2)

        # Test 0.0
        p3, r3, _ = validate_breakout_entry(10000, 9950, 0.0, 9900, 9850, 10000, 1.5)
        self.assertFalse(p3)
        self.assertIn("INVALID_RVOL", r3)

        # Test negative
        p4, r4, _ = validate_breakout_entry(10000, 9950, -1.5, 9900, 9850, 10000, 1.5)
        self.assertFalse(p4)
        self.assertIn("INVALID_RVOL", r4)

    def test_8_is_acceleration_cannot_bypass_safety_gate(self):
        """Test 8: is_acceleration=True cannot bypass validate_breakout_entry in SetupDetector & FullStrategySuite"""
        sym = SymbolInfo(
            iem_cd="999999",
            name="가속테스트",
            market="KOSDAQ",
            price=10000,
            open_price=9800,
            high_price=10050,
            low_price=9700,
            prev_close=9800,
            is_tradable=True
        )
        agg = CandleAggregator("999999")
        base_time = self.now - timedelta(minutes=10)
        # Price dropping from 10200 to 10000
        for i in range(10):
            c_time = base_time + timedelta(minutes=i)
            p = 10200 - (i * 20)
            c = Candle(
                timestamp=c_time,
                timeframe="1m",
                open=p + 10,
                high=p + 20,
                low=p - 10,
                close=p,
                volume=1000,
                turnover=p * 1000
            )
            agg.candles_1m.append(c)

        agg.calculate_rvol = lambda timeframe="1m", lookback=20: 0.5  # RVOL low!

        detector = SetupDetector()
        inspections, signals = detector.evaluate_setups(
            sym=sym,
            agg=agg,
            now=self.now,
            market_return=0.001,
            regime=MarketRegime.BULL,
            patterns={"is_acceleration": True, "is_compression_breakout": True}
        )

        bo_inspections = [i for i in inspections if i.candidate_type == SetupType.BREAKOUT.value]
        for item in bo_inspections:
            self.assertFalse(item.buy_approved, "Breakout setup must NOT be approved if gate fails, even with is_acceleration=True")


if __name__ == "__main__":
    unittest.main()
