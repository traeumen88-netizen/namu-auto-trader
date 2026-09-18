"""[TEST SIMILARITY ENGINE v1.0] Regression and Verification Suite for HistoricalSimilarityEngine
(tests/test_similarity_engine.py)

Verifies:
1. test_similarity_query_matches_initialized (matches.append execution with non-empty candidate history)
2. test_similarity_query_cache_hit
3. test_similarity_query_cache_miss
4. test_similarity_query_empty_result
5. test_similarity_query_pit_filter (candidate_timestamp <= now)
6. test_similarity_query_db_error (fail-closed neutral fallback)
7. test_live_call_signature (exact LIVE call form)
"""

import os
import sys
import tempfile
import unittest
from datetime import datetime, timedelta

# Ensure project root is in sys.path
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from ml.experience_memory import ExperienceMemory, PointInTimeSnapshot, OutcomeRecord
from ml.similarity_engine import HistoricalSimilarityEngine, SimilarityMetaOutput


class TestSimilarityEngine(unittest.TestCase):
    def setUp(self):
        self.temp_db = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        self.temp_db.close()
        self.memory = ExperienceMemory(db_path=self.temp_db.name)
        self.engine = HistoricalSimilarityEngine(memory=self.memory, min_samples=3)
        self.now = datetime(2026, 1, 15, 10, 0, 0)

    def tearDown(self):
        if os.path.exists(self.temp_db.name):
            try:
                os.remove(self.temp_db.name)
            except Exception:
                pass

    def _insert_sample_experiences(self, count: int = 5, past_days: int = 5):
        """Helper to populate database with labeled experience pairs (snapshot + outcome)."""
        for i in range(count):
            eid = f"EVT_TEST_{i:03d}"
            ev_time = (self.now - timedelta(days=past_days, minutes=i * 10)).isoformat()
            snap = PointInTimeSnapshot(
                event_id=eid,
                iem_cd="005930",
                name="삼성전자",
                event_time=ev_time,
                as_of_time=ev_time,
                feature_version="FEATURE_V16_0",
                strategy_version="STRATEGY_V16_0",
                model_version="CHAMPION_V16_0",
                dataset_version="DATA_V16_0",
                raw_features={
                    "return_1m": 0.010,
                    "return_3m": 0.015,
                    "return_5m": 0.020,
                    "rvol": 2.0,
                    "vwap_dist": 0.005,
                    "rsi": 55.0,
                    "execution_intensity": 120.0,
                    "spread": 0.0015
                },
                prediction={"p_target": 0.65, "p_stop": 0.35, "expected_net_r": 0.25},
                decision="BUY",
                decision_reason="Test",
                rule_score=85.0,
                virtual_trade={"entry_price": 70000, "stop_price": 69000, "target_price": 72000, "shares": 10},
                regime="BULL",
                time_of_day_bucket="09:30-11:30"
            )
            self.memory.record_snapshot(snap)

            outcome = OutcomeRecord(
                event_id=eid,
                evaluated_at=ev_time,
                horizon_reached="TARGET_HIT",
                target_hit=True,
                stop_hit=False,
                target_hit_first=(i % 2 == 0),  # Alternating wins
                max_mfe_pct=0.035,
                max_mae_pct=-0.008,
                realized_net_r=1.5 if (i % 2 == 0) else -1.0,
                outcome_category="MISSED_WINNER" if (i % 2 == 0) else "FALSE_SIGNAL",
                notes="Regression test outcome"
            )
            self.memory.record_outcome(outcome)

        # Clear memory cache in engine so newly inserted data is loaded
        self.engine.clear_cache()

    # 1. Verification of matches.append() execution without NameError
    def test_similarity_query_matches_initialized(self):
        """Specifically verifies that when candidate_history has items, matches.append() executes cleanly."""
        self._insert_sample_experiences(count=5, past_days=2)

        cur_feats = {
            "return_1m": 0.012,
            "return_3m": 0.018,
            "return_5m": 0.022,
            "rvol": 2.2,
            "vwap_dist": 0.004,
            "rsi": 58.0,
            "execution_intensity": 125.0,
            "spread": 0.0015
        }

        # This call previously crashed with: NameError: name 'matches' is not defined
        res = self.engine.query_similarity(cur_feats, current_regime="BULL", now=self.now)
        self.assertIsInstance(res, SimilarityMetaOutput)
        self.assertEqual(res.sample_count, 5)
        self.assertTrue(res.is_sufficient_sample)
        self.assertGreater(res.hist_win_rate, 0.0)

    # 2. Cache Miss Test
    def test_similarity_query_cache_miss(self):
        """Verifies cache miss evaluates search and populates cache."""
        self._insert_sample_experiences(count=4, past_days=1)
        cur_feats = {"price": 70000, "score": 85.0, "rvol": 2.0}

        self.engine.clear_cache()
        self.assertEqual(len(self.engine._query_cache), 0)

        res = self.engine.query_similarity(cur_feats, current_regime="BULL", now=self.now)
        self.assertIsInstance(res, SimilarityMetaOutput)
        self.assertEqual(len(self.engine._query_cache), 1)

    # 3. Cache Hit Test
    def test_similarity_query_cache_hit(self):
        """Verifies second identical call retrieves from cache without re-querying."""
        self._insert_sample_experiences(count=4, past_days=1)
        cur_feats = {"price": 70000, "score": 85.0, "rvol": 2.0}

        res1 = self.engine.query_similarity(cur_feats, current_regime="BULL", now=self.now)
        cache_count_before = len(self.engine._query_cache)
        self.assertGreaterEqual(cache_count_before, 1)

        # Call again with identical parameters
        res2 = self.engine.query_similarity(cur_feats, current_regime="BULL", now=self.now)
        self.assertIs(res1, res2, "Cache hit must return the identical cached object instance")
        self.assertEqual(len(self.engine._query_cache), cache_count_before)

    # 4. Empty Result Test
    def test_similarity_query_empty_result(self):
        """Verifies empty database gracefully returns neutral fallback with sample_count=0."""
        # Database is empty
        cur_feats = {"price": 70000, "score": 85.0, "rvol": 2.0}
        res = self.engine.query_similarity(cur_feats, current_regime="BULL", now=self.now)
        self.assertIsInstance(res, SimilarityMetaOutput)
        self.assertEqual(res.sample_count, 0)
        self.assertEqual(res.hist_win_rate, 0.50)
        self.assertFalse(res.is_sufficient_sample)

    # 5. PIT Filter Test (candidate_timestamp <= now)
    def test_similarity_query_pit_filter(self):
        """Verifies that future events are strictly excluded by the PIT filter."""
        # 1 past event (2026-01-10)
        past_time = "2026-01-10T10:00:00"
        snap_past = PointInTimeSnapshot(
            event_id="EVT_PAST", iem_cd="005930", name="삼성",
            event_time=past_time, as_of_time=past_time,
            feature_version="F", strategy_version="S", model_version="M", dataset_version="D",
            raw_features={"rvol": 2.0}, prediction={}, decision="BUY", decision_reason="", rule_score=80.0,
            virtual_trade={"entry_price": 70000, "stop_price": 69000, "target_price": 72000, "shares": 10},
            regime="BULL", time_of_day_bucket="09:30-11:30"
        )
        self.memory.record_snapshot(snap_past)
        self.memory.record_outcome(OutcomeRecord(
            event_id="EVT_PAST", evaluated_at=past_time, horizon_reached="TARGET_HIT",
            target_hit=True, stop_hit=False, target_hit_first=True,
            max_mfe_pct=0.03, max_mae_pct=-0.01, realized_net_r=1.0,
            outcome_category="MISSED_WINNER"
        ))

        # 1 future event (2026-01-20 > 2026-01-15)
        future_time = "2026-01-20T10:00:00"
        snap_future = PointInTimeSnapshot(
            event_id="EVT_FUTURE", iem_cd="005930", name="삼성",
            event_time=future_time, as_of_time=future_time,
            feature_version="F", strategy_version="S", model_version="M", dataset_version="D",
            raw_features={"rvol": 2.0}, prediction={}, decision="BUY", decision_reason="", rule_score=80.0,
            virtual_trade={"entry_price": 70000, "stop_price": 69000, "target_price": 72000, "shares": 10},
            regime="BULL", time_of_day_bucket="09:30-11:30"
        )
        self.memory.record_snapshot(snap_future)
        self.memory.record_outcome(OutcomeRecord(
            event_id="EVT_FUTURE", evaluated_at=future_time, horizon_reached="TARGET_HIT",
            target_hit=True, stop_hit=False, target_hit_first=True,
            max_mfe_pct=0.03, max_mae_pct=-0.01, realized_net_r=1.0,
            outcome_category="MISSED_WINNER"
        ))

        self.engine.clear_cache()
        # Query at 2026-01-15: EVT_FUTURE must be excluded
        res = self.engine.query_similarity({"rvol": 2.0}, current_regime="BULL", now=datetime(2026, 1, 15, 10, 0))
        self.assertEqual(res.sample_count, 1, "Only past events (event_time <= now) should be matched")

    # 6. DB Error Test
    def test_similarity_query_db_error(self):
        """Verifies that SQLite DB exception does not crash and returns safe neutral output."""
        class BrokenMemory:
            def get_all_labeled_experiences(self):
                raise sqlite3.OperationalError("database is locked / corrupted")

        broken_engine = HistoricalSimilarityEngine(memory=BrokenMemory())
        res = broken_engine.query_similarity({"rvol": 2.0}, current_regime="BULL", now=self.now)
        self.assertIsInstance(res, SimilarityMetaOutput)
        self.assertEqual(res.sample_count, 0)
        self.assertEqual(res.hist_win_rate, 0.50)
        self.assertFalse(res.is_sufficient_sample)

    # 7. Exact LIVE Call Test
    def test_live_call_signature(self):
        """Verifies the exact invocation syntax used in live_quant_trader.py."""
        self._insert_sample_experiences(count=3, past_days=1)
        sig_strategy_price = 70000
        sig_score = 85.0
        current_regime = "BULL"

        res = self.engine.query_similarity(
            current_features={
                "price": sig_strategy_price,
                "score": sig_score,
                "rvol": 2.0
            },
            current_regime=current_regime,
            now=self.now
        )
        self.assertIsInstance(res, SimilarityMetaOutput)
        self.assertIsNotNone(res.hist_win_rate)
        self.assertIsNotNone(res.expected_r)
        self.assertIsNotNone(res.similarity_distance)


if __name__ == "__main__":
    unittest.main()
