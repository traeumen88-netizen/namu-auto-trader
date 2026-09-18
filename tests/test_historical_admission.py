"""[TEST HISTORICAL ADMISSION GATE v1.0]
(tests/test_historical_admission.py)

Comprehensive unit tests for the 10-Point Historical Data Admission Gatekeeper:
1. test_all_gates_pass (BACKTEST PERFORMANCE ENABLED)
2. test_synthetic_data_blocked (Gate 1 FAIL)
3. test_timestamp_anomaly_blocked (Gate 2 FAIL)
4. test_session_truncation_blocked (Gate 3 FAIL)
5. test_invalid_universe_blocked (Gate 4 FAIL)
6. test_corrupted_ohlcv_blocked (Gate 5 FAIL)
7. test_inverted_quote_blocked (Gate 6 FAIL)
8. test_model_timing_inversion_blocked (Gate 7 FAIL)
9. test_scaler_timing_inversion_blocked (Gate 8 FAIL)
10. test_similarity_leakage_blocked (Gate 9 FAIL)
11. test_lookahead_leakage_blocked (Gate 10 FAIL)
"""

import os
import sys
import tempfile
import unittest
from datetime import datetime, timedelta

# Ensure project root is in sys.path
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core.historical_admission_gate import HistoricalDataAdmissionGate


class TestHistoricalAdmissionGate(unittest.TestCase):
    def setUp(self):
        self.gate = HistoricalDataAdmissionGate(strict_mode=True)
        self.oos_start = datetime(2026, 1, 15, 9, 0)
        self.model_fit = "2025-12-31"   # Strictly < 2026-01-15
        self.scaler_fit = "2025-12-31"  # Strictly < 2026-01-15

        # Create valid universe
        self.valid_universe = {
            "005930": {"iem_cd": "005930", "name": "삼성전자"},
            "000660": {"iem_cd": "000660", "name": "SK하이닉스"},
            "035420": {"iem_cd": "035420", "name": "NAVER"},
            "068270": {"iem_cd": "068270", "name": "셀트리온"},
            "005380": {"iem_cd": "005380", "name": "현대차"}
        }

        # Create a temp clean parquet/csv file
        self.temp_file = tempfile.NamedTemporaryFile(suffix=".parquet", delete=False)
        self.temp_file.write(b"PARQUET_TEST_HEADER")
        self.temp_file.close()

        # Create 390 clean 1m bars (09:00:00 to 15:29:00)
        self.clean_bars = []
        base_time = datetime(2026, 1, 15, 9, 0, 0)
        for i in range(390):
            ts = base_time + timedelta(minutes=i)
            p = 70000 + (i % 20) * 50
            self.clean_bars.append({
                "timestamp": ts,
                "symbol": "005930",
                "open": p,
                "high": p + 100,
                "low": p - 50,
                "close": p + 50,
                "volume": 10000 + i * 10,
                "bid": p + 40,
                "ask": p + 50
            })

    def tearDown(self):
        if os.path.exists(self.temp_file.name):
            try:
                os.remove(self.temp_file.name)
            except Exception:
                pass

    # 1. All Gates Pass -> ENABLED
    def test_all_gates_pass(self):
        eval_res = self.gate.evaluate_admission(
            data_source="HISTORICAL",
            dataset_path=self.temp_file.name,
            bars=self.clean_bars,
            universe=self.valid_universe,
            oos_start_date=self.oos_start,
            model_fit_date=self.model_fit,
            scaler_fit_date=self.scaler_fit,
            similarity_leakage_count=0,
            lookahead_leakage_count=0
        )
        self.assertEqual(eval_res.overall_status, "PASS")
        self.assertEqual(eval_res.decision, "BACKTEST PERFORMANCE ENABLED")
        self.assertEqual(eval_res.passed_count, 10)
        self.assertEqual(eval_res.failed_count, 0)

    # 2. Synthetic Data Blocked (Gate 1 FAIL)
    def test_synthetic_data_blocked(self):
        eval_res = self.gate.evaluate_admission(
            data_source="SYNTHETIC",  # Synthetic source
            dataset_path=None,
            bars=self.clean_bars,
            universe=self.valid_universe,
            oos_start_date=self.oos_start,
            model_fit_date=self.model_fit,
            scaler_fit_date=self.scaler_fit
        )
        self.assertEqual(eval_res.overall_status, "FAIL")
        self.assertEqual(eval_res.decision, "BACKTEST PERFORMANCE BLOCKED")
        self.assertIn("실제 KRX 데이터인지", eval_res.failure_reasons)

    # 3. Timestamp Anomaly Blocked (Gate 2 FAIL)
    def test_timestamp_anomaly_blocked(self):
        corrupt_bars = [b.copy() for b in self.clean_bars]
        # Inject duplicate and outside hours
        corrupt_bars[5]["timestamp"] = corrupt_bars[4]["timestamp"]  # duplicate
        corrupt_bars[10]["timestamp"] = datetime(2026, 1, 15, 8, 30, 0)  # outside hours

        eval_res = self.gate.evaluate_admission(
            data_source="HISTORICAL",
            dataset_path=self.temp_file.name,
            bars=corrupt_bars,
            universe=self.valid_universe,
            oos_start_date=self.oos_start,
            model_fit_date=self.model_fit,
            scaler_fit_date=self.scaler_fit
        )
        self.assertEqual(eval_res.overall_status, "FAIL")
        self.assertEqual(eval_res.decision, "BACKTEST PERFORMANCE BLOCKED")
        self.assertIn("Timestamp 정상인지", eval_res.failure_reasons)

    # 4. Session Truncation Blocked (Gate 3 FAIL)
    def test_session_truncation_blocked(self):
        # Only 20 bars instead of 390
        truncated_bars = self.clean_bars[:20]
        eval_res = self.gate.evaluate_admission(
            data_source="HISTORICAL",
            dataset_path=self.temp_file.name,
            bars=truncated_bars,
            universe=self.valid_universe,
            oos_start_date=self.oos_start,
            model_fit_date=self.model_fit,
            scaler_fit_date=self.scaler_fit
        )
        self.assertEqual(eval_res.overall_status, "FAIL")
        self.assertEqual(eval_res.decision, "BACKTEST PERFORMANCE BLOCKED")
        self.assertIn("1분/5분 세션 누락 없는지", eval_res.failure_reasons)

    # 5. Invalid Universe Blocked (Gate 4 FAIL)
    def test_invalid_universe_blocked(self):
        bad_universe = {"INVALID_TICKER": {"name": "잘못된종목"}}
        eval_res = self.gate.evaluate_admission(
            data_source="HISTORICAL",
            dataset_path=self.temp_file.name,
            bars=self.clean_bars,
            universe=bad_universe,
            oos_start_date=self.oos_start,
            model_fit_date=self.model_fit,
            scaler_fit_date=self.scaler_fit
        )
        self.assertEqual(eval_res.overall_status, "FAIL")
        self.assertEqual(eval_res.decision, "BACKTEST PERFORMANCE BLOCKED")
        self.assertIn("종목 Universe 정상인지", eval_res.failure_reasons)

    # 6. Corrupted OHLCV Blocked (Gate 5 FAIL)
    def test_corrupted_ohlcv_blocked(self):
        corrupt_bars = [b.copy() for b in self.clean_bars]
        corrupt_bars[0]["high"] = 50000  # high < low (low is 69950)
        eval_res = self.gate.evaluate_admission(
            data_source="HISTORICAL",
            dataset_path=self.temp_file.name,
            bars=corrupt_bars,
            universe=self.valid_universe,
            oos_start_date=self.oos_start,
            model_fit_date=self.model_fit,
            scaler_fit_date=self.scaler_fit
        )
        self.assertEqual(eval_res.overall_status, "FAIL")
        self.assertEqual(eval_res.decision, "BACKTEST PERFORMANCE BLOCKED")
        self.assertIn("OHLCV 무결성", eval_res.failure_reasons)

    # 7. Inverted Quote Blocked (Gate 6 FAIL)
    def test_inverted_quote_blocked(self):
        corrupt_bars = [b.copy() for b in self.clean_bars]
        corrupt_bars[0]["bid"] = 72000
        corrupt_bars[0]["ask"] = 70000  # bid > ask
        eval_res = self.gate.evaluate_admission(
            data_source="HISTORICAL",
            dataset_path=self.temp_file.name,
            bars=corrupt_bars,
            universe=self.valid_universe,
            oos_start_date=self.oos_start,
            model_fit_date=self.model_fit,
            scaler_fit_date=self.scaler_fit
        )
        self.assertEqual(eval_res.overall_status, "FAIL")
        self.assertEqual(eval_res.decision, "BACKTEST PERFORMANCE BLOCKED")
        self.assertIn("Bid/Ask 무결성", eval_res.failure_reasons)

    # 8. Model Timing Inversion Blocked (Gate 7 FAIL)
    def test_model_timing_inversion_blocked(self):
        # Model fit date in the future relative to OOS start
        eval_res = self.gate.evaluate_admission(
            data_source="HISTORICAL",
            dataset_path=self.temp_file.name,
            bars=self.clean_bars,
            universe=self.valid_universe,
            oos_start_date=datetime(2026, 1, 15),
            model_fit_date="2026-09-10",  # Future!
            scaler_fit_date=self.scaler_fit
        )
        self.assertEqual(eval_res.overall_status, "FAIL")
        self.assertEqual(eval_res.decision, "BACKTEST PERFORMANCE BLOCKED")
        self.assertIn("PIT Model 생성 가능 여부", eval_res.failure_reasons)

    # 9. Scaler Timing Inversion Blocked (Gate 8 FAIL)
    def test_scaler_timing_inversion_blocked(self):
        eval_res = self.gate.evaluate_admission(
            data_source="HISTORICAL",
            dataset_path=self.temp_file.name,
            bars=self.clean_bars,
            universe=self.valid_universe,
            oos_start_date=datetime(2026, 1, 15),
            model_fit_date=self.model_fit,
            scaler_fit_date="2026-09-10"  # Future!
        )
        self.assertEqual(eval_res.overall_status, "FAIL")
        self.assertEqual(eval_res.decision, "BACKTEST PERFORMANCE BLOCKED")
        self.assertIn("Scaler PIT 여부", eval_res.failure_reasons)

    # 10. Similarity Leakage Blocked (Gate 9 FAIL)
    def test_similarity_leakage_blocked(self):
        eval_res = self.gate.evaluate_admission(
            data_source="HISTORICAL",
            dataset_path=self.temp_file.name,
            bars=self.clean_bars,
            universe=self.valid_universe,
            oos_start_date=self.oos_start,
            model_fit_date=self.model_fit,
            scaler_fit_date=self.scaler_fit,
            similarity_leakage_count=3  # 3 leakage events!
        )
        self.assertEqual(eval_res.overall_status, "FAIL")
        self.assertEqual(eval_res.decision, "BACKTEST PERFORMANCE BLOCKED")
        self.assertIn("Similarity PIT 여부", eval_res.failure_reasons)

    # 11. Lookahead Leakage Blocked (Gate 10 FAIL)
    def test_lookahead_leakage_blocked(self):
        eval_res = self.gate.evaluate_admission(
            data_source="HISTORICAL",
            dataset_path=self.temp_file.name,
            bars=self.clean_bars,
            universe=self.valid_universe,
            oos_start_date=self.oos_start,
            model_fit_date=self.model_fit,
            scaler_fit_date=self.scaler_fit,
            lookahead_leakage_count=1  # 1 leakage event!
        )
        self.assertEqual(eval_res.overall_status, "FAIL")
        self.assertEqual(eval_res.decision, "BACKTEST PERFORMANCE BLOCKED")
        self.assertIn("Leakage 0건 여부", eval_res.failure_reasons)


if __name__ == "__main__":
    unittest.main()
