"""[HISTORICAL DATA ADMISSION GATE RUNNER]
(scripts/run_historical_data_admission.py)

Usage:
  python scripts/run_historical_data_admission.py
  python scripts/run_historical_data_admission.py --data data/cold_parquet/krx_2026_01.parquet --oos-start 2026-01-02 --model-fit 2025-12-31

Evaluates the 10 Admission Gates:
1. 실제 KRX 데이터인지
2. Timestamp 정상인지
3. 1분/5분 세션 누락 없는지
4. 종목 Universe 정상인지
5. OHLCV 무결성
6. Bid/Ask 무결성
7. PIT Model 생성 가능 여부
8. Scaler PIT 여부
9. Similarity PIT 여부
10. Leakage 0건 여부

All PASS → BACKTEST PERFORMANCE ENABLED
Any FAIL → BACKTEST PERFORMANCE BLOCKED
"""

import os
import sys
import argparse
from datetime import datetime

# Path setup
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core.historical_admission_gate import HistoricalDataAdmissionGate
from universe.full_universe_master import FullUniverseMaster


def main():
    parser = argparse.ArgumentParser(description="Historical Data Admission Gate Runner")
    parser.add_argument("--data", type=str, default=None, help="Path to historical dataset (parquet/csv)")
    parser.add_argument("--data-source", type=str, default=None, help="HISTORICAL or SYNTHETIC")
    parser.add_argument("--oos-start", type=str, default="2026-01-02", help="OOS Start Date (YYYY-MM-DD)")
    parser.add_argument("--model-fit", type=str, default="2026-09-10", help="Model Fit End Date (YYYY-MM-DD)")
    parser.add_argument("--scaler-fit", type=str, default="2026-09-10", help="Scaler Fit End Date (YYYY-MM-DD)")
    args = parser.parse_args()

    # Determine default dataset if not provided
    data_path = args.data
    data_source = args.data_source

    if data_path is None:
        # Check data/cold_parquet directory
        cold_dir = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "data", "cold_parquet")
        parquet_files = [os.path.join(cold_dir, f) for f in os.listdir(cold_dir)] if os.path.exists(cold_dir) else []
        if parquet_files:
            data_path = parquet_files[0]
            data_source = "HISTORICAL"
        else:
            data_path = None
            data_source = data_source or "SYNTHETIC"
    else:
        if data_source is None:
            data_source = "HISTORICAL" if os.path.exists(data_path) else "SYNTHETIC"

    oos_start_dt = datetime.strptime(args.oos_start, "%Y-%m-%d") if args.oos_start else None

    # Load Universe
    universe = FullUniverseMaster.load_full_universe()

    gate = HistoricalDataAdmissionGate(strict_mode=True)
    eval_res = gate.evaluate_admission(
        data_source=data_source,
        dataset_path=data_path,
        bars=None,  # No bars loaded yet if file is absent
        universe=universe,
        oos_start_date=oos_start_dt,
        model_fit_date=args.model_fit,
        scaler_fit_date=args.scaler_fit,
        similarity_leakage_count=0,
        lookahead_leakage_count=0
    )

    print("\n" + "=" * 55)
    print("HISTORICAL DATA ADMISSION GATE")
    print("=" * 55 + "\n")

    for g in eval_res.gates:
        print(f"{g.gate_id:2d}. {g.name:<30} {g.status}")
        if g.status == "FAIL":
            print(f"    -> {g.details}")

    print("\n" + "-" * 55)
    print(f"Passed Gates: {eval_res.passed_count}/10 | Failed Gates: {eval_res.failed_count}/10")
    print("-" * 55)

    print(f"\nFINAL DECISION: {eval_res.decision}\n")
    print("=" * 55 + "\n")

    return 0 if eval_res.overall_status == "PASS" else 1


if __name__ == "__main__":
    sys.exit(main())
