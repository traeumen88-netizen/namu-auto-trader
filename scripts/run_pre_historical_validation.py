"""[PRE-HISTORICAL VALIDATION RUNNER]
(scripts/run_pre_historical_validation.py)

Executes all 6 pre-historical integrity gates and prints the exact validation block:
==================================================
REALISTIC BACKTEST PRE-HISTORICAL VALIDATION
==================================================

Stop Width Parity        PASS / FAIL
Shadow Replay Parity     PASS / FAIL
Data Quality Test        PASS / FAIL
Order Recovery           PASS / FAIL
Idempotency              PASS / FAIL
Position Recovery        PASS / FAIL

OVERALL PRECHECK         PASS / FAIL
==================================================
"""

import os
import sys
import inspect
from datetime import datetime

# Path setup
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core.models import SymbolInfo, OrderSide, OrderType, OrderStatus, TimeHorizon, TradeSignal, Position
from core.data_quality_gate import DataQualityGate
from backtester.shared_decision_core import SharedDecisionCore
from execution.order_router import OrderRouter
from execution.position_manager import PositionManager
from backtester.position_tracker import PositionTracker
from backtester.execution_simulator import RealisticExecutionSimulator
from tests.test_shadow_replay import run_shadow_replay_simulation
from tests.test_chaos_and_recovery import (
    test_data_quality_chaos,
    test_order_idempotency,
    test_retry_no_duplicate,
    test_delayed_ack,
    test_partial_fill_after_retry,
    test_position_recovery_lifecycle_states,
    test_backtester_position_tracker_recovery
)


def verify_stop_width_parity() -> bool:
    """Verifies that no artificial 1.2% stop width lower bound exists in Backtest or LIVE."""
    from strategies import full_strategy_suite
    from backtester import position_tracker
    from core import setup_detector

    fss_src = inspect.getsource(full_strategy_suite.FullStrategySuite.evaluate_intraday_all)
    pt_src = inspect.getsource(position_tracker.PositionTracker.open_position)
    sd_src = inspect.getsource(setup_detector.SetupDetector.detect_setups)

    # Neither LIVE nor Backtest should have 0.012 artificial clamp
    if "0.012" in fss_src or "0.012" in pt_src or "0.012" in sd_src:
        return False
    return True


def run_all_pre_historical_checks():
    results = {}

    # 1. Stop Width Parity
    try:
        results["Stop Width Parity"] = "PASS" if verify_stop_width_parity() else "FAIL"
    except Exception as e:
        results["Stop Width Parity"] = f"FAIL ({e})"

    # 2. Shadow Replay Parity
    try:
        diffs = run_shadow_replay_simulation()
        results["Shadow Replay Parity"] = "PASS" if len(diffs) == 0 else "FAIL"
    except Exception as e:
        results["Shadow Replay Parity"] = f"FAIL ({e})"

    # 3. Data Quality Test
    try:
        test_data_quality_chaos()
        results["Data Quality Test"] = "PASS"
    except Exception as e:
        results["Data Quality Test"] = f"FAIL ({e})"

    # 4. Order Recovery
    try:
        test_delayed_ack()
        results["Order Recovery"] = "PASS"
    except Exception as e:
        results["Order Recovery"] = f"FAIL ({e})"

    # 5. Idempotency
    try:
        test_order_idempotency()
        test_retry_no_duplicate()
        test_partial_fill_after_retry()
        results["Idempotency"] = "PASS"
    except Exception as e:
        results["Idempotency"] = f"FAIL ({e})"

    # 6. Position Recovery
    try:
        test_position_recovery_lifecycle_states()
        test_backtester_position_tracker_recovery()
        results["Position Recovery"] = "PASS"
    except Exception as e:
        results["Position Recovery"] = f"FAIL ({e})"

    # Overall Precheck: ALL must be PASS
    all_passed = all(status == "PASS" for status in results.values())
    overall_status = "PASS" if all_passed else "FAIL"

    # Print exact required block
    print("\n==================================================")
    print("REALISTIC BACKTEST PRE-HISTORICAL VALIDATION")
    print("==================================================")
    print()
    for name, status in results.items():
        print(f"{name:<25}{status}")
    print()
    print(f"OVERALL PRECHECK         {overall_status}")
    print("==================================================")

    return overall_status == "PASS"


if __name__ == "__main__":
    success = run_all_pre_historical_checks()
    sys.exit(0 if success else 1)
