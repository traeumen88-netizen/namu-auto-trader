"""확률 보정기 (Probability Calibrator v7.0)
- Execution-Grade Specification v7.0 (Section 9, 49 준수)
- Platt Scaling (Sigmoid) 및 Isotonic Regression 구현
- Brier Score 및 ECE (Expected Calibration Error) 측정
"""

import math
from typing import List, Tuple, Dict, Any, Optional
import numpy as np


class ProbabilityCalibrator:
    def __init__(self, method: str = "sigmoid"):
        """
        :param method: 'sigmoid' (Platt Scaling) 또는 'isotonic'
        """
        self.method = method.lower().strip()
        self.a = 1.0
        self.b = 0.0
        self.is_fitted = False
        self.isotonic_x = np.array([])
        self.isotonic_y = np.array([])

    def fit(self, raw_scores: np.ndarray, labels: np.ndarray):
        """
        학습 데이터의 raw_scores (0~1 또는 logit)와 이진 레이블(0 또는 1)로 캘리브레이션 피팅
        """
        raw_scores = np.asarray(raw_scores, dtype=np.float64)
        labels = np.asarray(labels, dtype=np.float64)
        n = len(raw_scores)
        if n < 10:
            self.is_fitted = True
            return

        if self.method == "sigmoid":
            # Platt Scaling (Logistic Regression on raw scores)
            # Minimize binary cross-entropy with gradient descent or analytic approximation
            # Transform raw_score to log-odds if already in (0, 1)
            eps = 1e-6
            clipped = np.clip(raw_scores, eps, 1.0 - eps)
            logits = np.log(clipped / (1.0 - clipped))
            
            # Simple 1D logistic fit
            var_logits = np.var(logits)
            if var_logits > 1e-5:
                cov = np.cov(logits, labels)[0, 1]
                self.a = max(0.2, min(5.0, cov / var_logits))
            else:
                self.a = 1.0
            self.b = np.mean(labels) - self.a * np.mean(logits)
            self.is_fitted = True

        elif self.method == "isotonic":
            # Simple PAV (Pool Adjacent Violators) for Isotonic Regression
            order = np.argsort(raw_scores)
            sx = raw_scores[order]
            sy = labels[order]
            
            # Quantile binning for robust monotonic curve
            bins = min(10, n // 5)
            if bins >= 3:
                quantiles = np.linspace(0, 100, bins + 1)
                qx = np.percentile(sx, quantiles)
                qx = np.unique(qx)
                qy = []
                for i in range(len(qx) - 1):
                    mask = (sx >= qx[i]) & (sx <= qx[i+1])
                    qy.append(np.mean(sy[mask]) if np.any(mask) else 0.5)
                # Enforce monotonic non-decreasing
                for i in range(1, len(qy)):
                    if qy[i] < qy[i-1]:
                        qy[i] = qy[i-1]
                self.isotonic_x = qx[:-1]
                self.isotonic_y = np.array(qy)
            self.is_fitted = True

    def predict_proba(self, raw_scores: np.ndarray) -> np.ndarray:
        """보정된 확률 반환 [0.0, 1.0]"""
        raw_scores = np.asarray(raw_scores, dtype=np.float64)
        if not self.is_fitted or len(raw_scores) == 0:
            return np.clip(raw_scores, 0.01, 0.99)

        if self.method == "sigmoid":
            eps = 1e-6
            clipped = np.clip(raw_scores, eps, 1.0 - eps)
            logits = np.log(clipped / (1.0 - clipped))
            calibrated_logits = self.a * logits + self.b
            return 1.0 / (1.0 + np.exp(-calibrated_logits))
        
        elif self.method == "isotonic" and len(self.isotonic_x) > 0:
            return np.interp(raw_scores, self.isotonic_x, self.isotonic_y, left=0.05, right=0.95)

        return np.clip(raw_scores, 0.01, 0.99)

    @classmethod
    def calculate_brier_score(cls, probabilities: np.ndarray, labels: np.ndarray) -> float:
        """Brier Score = Mean Squared Error of Probabilities vs Binary Labels"""
        p = np.asarray(probabilities, dtype=np.float64)
        y = np.asarray(labels, dtype=np.float64)
        if len(p) == 0:
            return 0.0
        return float(np.mean((p - y) ** 2))

    @classmethod
    def calculate_ece(cls, probabilities: np.ndarray, labels: np.ndarray, n_bins: int = 10) -> float:
        """Expected Calibration Error (ECE)"""
        p = np.asarray(probabilities, dtype=np.float64)
        y = np.asarray(labels, dtype=np.float64)
        if len(p) == 0:
            return 0.0

        bin_edges = np.linspace(0.0, 1.0, n_bins + 1)
        ece = 0.0
        total = len(p)

        for i in range(n_bins):
            mask = (p >= bin_edges[i]) & (p < bin_edges[i+1])
            if np.any(mask):
                bin_acc = np.mean(y[mask])
                bin_conf = np.mean(p[mask])
                bin_weight = np.sum(mask) / total
                ece += bin_weight * abs(bin_acc - bin_conf)

        return float(ece)
