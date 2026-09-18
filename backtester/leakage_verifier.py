"""[LEAKAGE VERIFIER v1.0] Point-in-Time (PIT) & Look-ahead Leakage Validator
(backtester/leakage_verifier.py)

Strictly verifies that no future information leaks into:
1. LOOKAHEAD_LEAKAGE_CHECK: Bar / Tick timestamps > T
2. TRAIN_DATA_CUTOFF_CHECK: Training data >= OOS Test Start
3. FEATURE_TIMESTAMP_CHECK: Feature calculation window > T
4. MODEL_TIMESTAMP_CHECK: Model training timestamp > T
5. SCALER_TIMESTAMP_CHECK: Scaler fitted on test data
6. SIMILARITY_TIMESTAMP_CHECK: Historical cases queried with timestamp > T
"""

import logging
from datetime import datetime
from typing import Dict, Any, List, Optional

logger = logging.getLogger("LeakageVerifier")


class LeakageError(Exception):
    """Raised when any lookahead or point-in-time data leakage is detected."""
    pass


class LookaheadLeakageVerifier:
    """
    Independent Auditor that intercepts and validates all data points,
    features, model queries, and similarity lookups against current timestamp T.
    """

    def __init__(self, strict_mode: bool = True):
        self.strict_mode = strict_mode
        self.audit_results: Dict[str, Dict[str, Any]] = {
            "LOOKAHEAD_LEAKAGE_CHECK": {"status": "PASS", "violations": 0, "details": []},
            "TRAIN_DATA_CUTOFF_CHECK": {"status": "PASS", "violations": 0, "details": []},
            "FEATURE_TIMESTAMP_CHECK": {"status": "PASS", "violations": 0, "details": []},
            "MODEL_TIMESTAMP_CHECK": {"status": "PASS", "violations": 0, "details": []},
            "SCALER_TIMESTAMP_CHECK": {"status": "PASS", "violations": 0, "details": []},
            "SIMILARITY_TIMESTAMP_CHECK": {"status": "PASS", "violations": 0, "details": []},
        }

    def _record_violation(self, check_name: str, message: str):
        self.audit_results[check_name]["status"] = "FAIL"
        self.audit_results[check_name]["violations"] += 1
        self.audit_results[check_name]["details"].append(message)
        logger.error(f"[LEAKAGE DETECTED] {check_name}: {message}")
        if self.strict_mode:
            raise LeakageError(f"[{check_name}] {message}")

    def verify_bar_timestamp(self, current_time: datetime, bar_timestamp: datetime, symbol: str):
        """Ensures that no bar being evaluated has a timestamp after current_time T."""
        if bar_timestamp > current_time:
            self._record_violation(
                "LOOKAHEAD_LEAKAGE_CHECK",
                f"Symbol {symbol} bar timestamp {bar_timestamp} > simulation time {current_time}"
            )

    def verify_historical_window(self, current_time: datetime, history_timestamps: List[datetime], symbol: str):
        """Ensures that the entire historical window strictly ends at or before current_time T."""
        for ts in history_timestamps:
            if ts > current_time:
                self._record_violation(
                    "LOOKAHEAD_LEAKAGE_CHECK",
                    f"Symbol {symbol} historical window contains future timestamp {ts} > {current_time}"
                )
                break

    def verify_train_test_cutoff(self, train_max_time: datetime, test_min_time: datetime):
        """Ensures training data does not overlap or extend into test/OOS period."""
        if train_max_time >= test_min_time:
            self._record_violation(
                "TRAIN_DATA_CUTOFF_CHECK",
                f"Train max time {train_max_time} >= Test min time {test_min_time} (Lookahead in training split)"
            )

    def verify_feature_timestamp(self, current_time: datetime, feature_as_of: datetime, symbol: str):
        """Ensures features are computed with as_of timestamp <= T."""
        if feature_as_of > current_time:
            self._record_violation(
                "FEATURE_TIMESTAMP_CHECK",
                f"Symbol {symbol} feature timestamp {feature_as_of} > simulation time {current_time}"
            )

    def verify_model_timestamp(self, current_time: datetime, model_trained_at: Optional[datetime], model_id: str):
        """Ensures ML model was trained on or before current simulation time T."""
        if model_trained_at and model_trained_at > current_time:
            self._record_violation(
                "MODEL_TIMESTAMP_CHECK",
                f"Model {model_id} was trained at {model_trained_at} > simulation time {current_time}"
            )

    def verify_scaler_fit(self, scaler_fitted_on_test: bool, scaler_version: str):
        """Ensures feature scaler was not fit on test data."""
        if scaler_fitted_on_test:
            self._record_violation(
                "SCALER_TIMESTAMP_CHECK",
                f"Scaler {scaler_version} was fitted on test data (Distribution leakage)"
            )

    def verify_similarity_query(self, current_time: datetime, queried_case_timestamp: datetime, case_id: str):
        """Ensures historical similarity engine only returns cases prior to current_time T."""
        if queried_case_timestamp > current_time:
            self._record_violation(
                "SIMILARITY_TIMESTAMP_CHECK",
                f"Similarity case {case_id} timestamp {queried_case_timestamp} > simulation time {current_time}"
            )

    def get_summary(self) -> Dict[str, Any]:
        overall_valid = all(v["status"] == "PASS" for v in self.audit_results.values())
        return {
            "overall_valid": overall_valid,
            "verdict": "VALID" if overall_valid else "INVALID",
            "checks": self.audit_results
        }
