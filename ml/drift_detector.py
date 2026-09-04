"""v7.0 Concept Drift & Feature Shift Detector (ml/drift_detector.py)
Monitors feature distribution shifts (PSI), calibration decay, and performance degradation.
Triggers RETRAINING_REQUEST when model assumptions deteriorate.
"""

import numpy as np
from typing import Dict, Any, List, Optional
from dataclasses import dataclass, field
from datetime import datetime


@dataclass
class DriftReport:
    timestamp: str
    is_drift_detected: bool
    retraining_needed: bool
    psi_scores: Dict[str, float]
    max_psi: float
    performance_decay: bool
    rolling_win_rate: float
    rolling_profit_factor: float
    rolling_brier_score: float
    reasons: List[str] = field(default_factory=list)


class DriftDetector:
    """
    Detects Population Stability Index (PSI) shifts on feature distributions,
    and performance degradation on live trade outcomes.
    """

    def __init__(
        self,
        psi_threshold: float = 0.25,        # PSI >= 0.25 indicates significant shift
        min_rolling_win_rate: float = 0.45, # Win rate < 45% triggers alert
        min_rolling_pf: float = 1.0,        # Profit Factor < 1.0 triggers alert
        max_brier_score: float = 0.25       # Brier Score > 0.25 triggers calibration alert
    ):
        self.psi_threshold = psi_threshold
        self.min_rolling_win_rate = min_rolling_win_rate
        self.min_rolling_pf = min_rolling_pf
        self.max_brier_score = max_brier_score

    @staticmethod
    def calculate_psi(baseline: np.ndarray, target: np.ndarray, num_bins: int = 10) -> float:
        """
        Calculates Population Stability Index (PSI) between baseline and target distributions.
        PSI = sum((Actual% - Expected%) * ln(Actual% / Expected%))
        """
        baseline = np.asarray(baseline, dtype=float)
        target = np.asarray(target, dtype=float)

        if len(baseline) == 0 or len(target) == 0:
            return 0.0

        # Create quantile bins from baseline
        quantiles = np.linspace(0, 100, num_bins + 1)
        try:
            bins = np.percentile(baseline, quantiles)
            bins[0] -= 1e-5
            bins[-1] += 1e-5
            # Ensure bins are strictly increasing
            bins = np.unique(bins)
            if len(bins) < 2:
                return 0.0
        except Exception:
            return 0.0

        # Frequency counts
        b_counts, _ = np.histogram(baseline, bins=bins)
        t_counts, _ = np.histogram(target, bins=bins)

        # Probabilities with Laplace smoothing to avoid division by zero
        b_probs = (b_counts + 1e-4) / (len(baseline) + 1e-4 * len(b_counts))
        t_probs = (t_counts + 1e-4) / (len(target) + 1e-4 * len(t_counts))

        psi = np.sum((t_probs - b_probs) * np.log(t_probs / b_probs))
        return float(np.clip(psi, 0.0, 10.0))

    def evaluate_feature_drift(
        self,
        baseline_features: Dict[str, List[float]],
        recent_features: Dict[str, List[float]]
    ) -> Dict[str, float]:
        """
        Evaluates PSI for each feature in the dictionaries.
        """
        psi_dict = {}
        for feat_name, base_vals in baseline_features.items():
            if feat_name in recent_features:
                rec_vals = recent_features[feat_name]
                if len(base_vals) >= 20 and len(rec_vals) >= 20:
                    psi_val = self.calculate_psi(np.array(base_vals), np.array(rec_vals))
                    psi_dict[feat_name] = round(psi_val, 4)
        return psi_dict

    def evaluate(
        self,
        baseline_features: Optional[Dict[str, List[float]]] = None,
        recent_features: Optional[Dict[str, List[float]]] = None,
        recent_trades: Optional[List[Dict[str, Any]]] = None
    ) -> DriftReport:
        """
        Comprehensive drift check: PSI + Rolling trade metrics.
        """
        psi_scores = {}
        max_psi = 0.0
        reasons = []

        if baseline_features and recent_features:
            psi_scores = self.evaluate_feature_drift(baseline_features, recent_features)
            if psi_scores:
                max_psi = max(psi_scores.values())
                if max_psi >= self.psi_threshold:
                    high_psi_feats = [k for k, v in psi_scores.items() if v >= self.psi_threshold]
                    reasons.append(f"Significant feature drift detected in: {', '.join(high_psi_feats)} (Max PSI: {max_psi:.3f})")

        # Performance decay check on recent trades
        perf_decay = False
        rolling_wr = 0.50
        rolling_pf = 1.50
        rolling_brier = 0.15

        if recent_trades and len(recent_trades) >= 10:
            wins = [t for t in recent_trades if t.get("pnl", 0) > 0]
            losses = [t for t in recent_trades if t.get("pnl", 0) <= 0]
            rolling_wr = len(wins) / len(recent_trades)

            gross_profit = sum(t.get("pnl", 0) for t in wins)
            gross_loss = abs(sum(t.get("pnl", 0) for t in losses))
            rolling_pf = (gross_profit / gross_loss) if gross_loss > 0 else 999.0

            # Brier score
            brier_vals = [
                (t.get("p_target_pred", 0.5) - (1.0 if t.get("pnl", 0) > 0 else 0.0)) ** 2
                for t in recent_trades
            ]
            rolling_brier = sum(brier_vals) / len(brier_vals)

            if rolling_wr < self.min_rolling_win_rate:
                perf_decay = True
                reasons.append(f"Rolling Win Rate {rolling_wr:.1%} < {self.min_rolling_win_rate:.1%}")
            if rolling_pf < self.min_rolling_pf:
                perf_decay = True
                reasons.append(f"Rolling Profit Factor {rolling_pf:.2f} < {self.min_rolling_pf:.2f}")
            if rolling_brier > self.max_brier_score:
                perf_decay = True
                reasons.append(f"Brier score decay {rolling_brier:.3f} > {self.max_brier_score:.3f}")

        is_drift = max_psi >= self.psi_threshold or perf_decay
        retraining_needed = is_drift

        return DriftReport(
            timestamp=datetime.now().isoformat(),
            is_drift_detected=is_drift,
            retraining_needed=retraining_needed,
            psi_scores=psi_scores,
            max_psi=round(max_psi, 4),
            performance_decay=perf_decay,
            rolling_win_rate=round(rolling_wr, 4),
            rolling_profit_factor=round(rolling_pf, 2),
            rolling_brier_score=round(rolling_brier, 4),
            reasons=reasons
        )
