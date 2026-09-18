"""v12.3 Historical Similarity Engine (ml/similarity_engine.py)
[FINAL PATCH v12.3] Section 7, 8, 13, 14, 15, 16

Key Responsibilities:
1. Historical Similarity as Meta Feature:
   - Does NOT directly decide BUY!
   - Provides meta features only:
     hist_win_rate, hist_target_rate, hist_stop_rate, expected_r, avg_mfe, avg_mae,
     sample_count, regime_match, time_of_day_match, similarity_distance.
2. Composite Similarity:
   Feature Similarity * Market Regime Match * Time-of-Day Match * Liquidity Match.
3. Recent Data Weighting:
   < 3 months: 1.5x, 3~12 months: 1.0x, > 12 months: 0.5x.
4. Correlation, Ablation, and Leakage Prevention:
   Detects feature redundancy with ML features and verifies OOS improvement.
"""

import math
from datetime import datetime, timedelta
from typing import Dict, Any, List, Optional, Tuple
from dataclasses import dataclass
import numpy as np
from ml.experience_memory import ExperienceMemory


@dataclass
class SimilarityMetaOutput:
    """
    Section 8: Meta Model에 제공하는 과거 유사도 메타 지표 (직접 BUY 결정 금지)
    """
    hist_win_rate: float        # 과거 유사 패턴의 승률 [0.0, 1.0]
    hist_target_rate: float     # 목표가 도달 비율 [0.0, 1.0]
    hist_stop_rate: float       # 손절가 도달 비율 [0.0, 1.0]
    expected_r: float           # 과거 유사 패턴의 평균 실현 순기대값 (R)
    avg_mfe: float              # 평균 최대 유리 진행 (%)
    avg_mae: float              # 평균 최대 불리 역행 (%)
    sample_count: int           # 매칭된 유효 과거 표본 수
    regime_match: float         # 시장 국면 일치도 [0.0, 1.0]
    time_of_day_match: float    # 시간대 일치도 [0.0, 1.0]
    similarity_distance: float  # 가중 평균 거리 (낮을수록 유사함)
    is_sufficient_sample: bool  # 통계적 유의미 표본 확보 여부 (N >= 5)


class HistoricalSimilarityEngine:
    """
    과거 경험 메모리 기반 국면/시간대/유동성 다중 복합 유사도 계산 및 메타 피처 추출기
    """

    CORE_FEATURE_KEYS = [
        "return_1m", "return_3m", "return_5m", "rvol",
        "vwap_dist", "rsi", "execution_intensity", "spread"
    ]

    def __init__(self, memory: ExperienceMemory, min_samples: int = 5):
        self.memory = memory
        self.min_samples = min_samples
        self._cached_history: Optional[List[Dict[str, Any]]] = None
        self._query_cache: Dict[Tuple, SimilarityMetaOutput] = {}

    def clear_cache(self):
        """Clears both historical snapshot cache and query result cache."""
        self._cached_history = None
        if hasattr(self, "_query_cache"):
            self._query_cache.clear()

    @classmethod
    def get_time_of_day_bucket(cls, dt: datetime) -> str:
        return ExperienceMemory.get_time_of_day_bucket(dt)

    @classmethod
    def calculate_recency_weight(cls, event_time: datetime, now: datetime) -> float:
        """
        Section 13: 최근 데이터 가중치 (Recent Data Weight)
        최근 3개월: 1.5, 3~12개월: 1.0, 12개월 초과: 0.5
        """
        days_diff = (now - event_time).total_seconds() / 86400.0
        if days_diff <= 90:
            return 1.5
        elif days_diff <= 365:
            return 1.0
        else:
            return 0.5

    @classmethod
    def calculate_regime_match(cls, current_regime: str, past_regime: str) -> float:
        """
        Section 14: 시장 국면 일치도 가중치 (Regime-Specific Memory)
        """
        if current_regime == past_regime:
            return 1.0
        # 유사 국면 (BULL vs STRONG_BULL, BEAR vs PANIC 등)
        bullish = {"BULL", "STRONG_BULL"}
        bearish = {"BEAR", "PANIC"}
        if (current_regime in bullish and past_regime in bullish) or (current_regime in bearish and past_regime in bearish):
            return 0.7
        return 0.35  # 상반된 국면은 대폭 감점

    @classmethod
    def calculate_tod_match(cls, current_tod: str, past_tod: str) -> float:
        """
        Section 15: 시간대 일치도 가중치 (Time-of-Day Memory)
        """
        if current_tod == past_tod:
            return 1.0
        ordered = ["09:00-09:30", "09:30-11:30", "11:30-13:00", "13:00-15:30"]
        if current_tod in ordered and past_tod in ordered:
            idx1 = ordered.index(current_tod)
            idx2 = ordered.index(past_tod)
            diff = abs(idx1 - idx2)
            if diff == 1:
                return 0.6
            elif diff == 2:
                return 0.3
        return 0.15

    def query_similarity(
        self,
        current_features: Dict[str, Any],
        current_regime: str,
        now: datetime,
        top_k: int = 20
    ) -> SimilarityMetaOutput:
        """
        Section 8 & 16: 복합 유사도 계산 (Composite Similarity)
        Similarity = Feature Sim * Regime Match * Time-of-Day Match * Liquidity Match
        """
        matches: List[Dict[str, Any]] = []

        regime_str = str(current_regime.value if hasattr(current_regime, "value") else current_regime or "NEUTRAL")
        current_tod = self.get_time_of_day_bucket(now)

        # 0. Query-level Cache Check (CACHE HIT / MISS)
        cache_key = (
            round(float(current_features.get("price", 0.0)), -2),
            round(float(current_features.get("score", 0.0)), 1),
            round(float(current_features.get("rvol", 1.0)), 2),
            regime_str,
            current_tod,
            now.strftime("%Y-%m-%d %H:%M"),
            top_k
        )

        if hasattr(self, "_query_cache") and cache_key in self._query_cache:
            return self._query_cache[cache_key]

        # 1. Experience Memory Load with safe exception handling (DB Error defense)
        try:
            if not hasattr(self, "_cached_history") or self._cached_history is None:
                self._cached_history = self.memory.get_all_labeled_experiences()
            labeled_history = self._cached_history or []
        except Exception:
            out = SimilarityMetaOutput(
                hist_win_rate=0.50,
                hist_target_rate=0.50,
                hist_stop_rate=0.50,
                expected_r=0.0,
                avg_mfe=0.0,
                avg_mae=0.0,
                sample_count=0,
                regime_match=1.0,
                time_of_day_match=1.0,
                similarity_distance=1.0,
                is_sufficient_sample=False
            )
            return out

        if not labeled_history:
            # 과거 표본이 전혀 없을 때 중립 폴백
            out = SimilarityMetaOutput(
                hist_win_rate=0.50,
                hist_target_rate=0.50,
                hist_stop_rate=0.50,
                expected_r=0.0,
                avg_mfe=0.0,
                avg_mae=0.0,
                sample_count=0,
                regime_match=1.0,
                time_of_day_match=1.0,
                similarity_distance=1.0,
                is_sufficient_sample=False
            )
            if hasattr(self, "_query_cache"):
                self._query_cache[cache_key] = out
            return out

        # 2. 현재 수치 벡터 생성
        cur_vec = []
        for k in self.CORE_FEATURE_KEYS:
            cur_vec.append(float(current_features.get(k, 0.0)))
        cur_arr = np.array(cur_vec, dtype=np.float32)
        norm_cur = np.linalg.norm(cur_arr) + 1e-6

        # 3. PIT Guarantee (Section 6 & 13): Filter cases strictly <= now, and take recent 200 cases for optimal latency
        now_iso = now.isoformat()
        pit_history = [exp for exp in labeled_history if exp.get("event_time", "") <= now_iso]
        candidate_history = pit_history[-200:] if len(pit_history) > 200 else pit_history

        if not candidate_history:
            out = SimilarityMetaOutput(
                hist_win_rate=0.50, hist_target_rate=0.50, hist_stop_rate=0.50,
                expected_r=0.0, avg_mfe=0.0, avg_mae=0.0, sample_count=0,
                regime_match=1.0, time_of_day_match=1.0, similarity_distance=1.0,
                is_sufficient_sample=False
            )
            if hasattr(self, "_query_cache"):
                self._query_cache[cache_key] = out
            return out

        # 4. Search and populate matches
        for exp in candidate_history:
            past_feats = exp.get("raw_features", {})
            past_vec = []
            for k in self.CORE_FEATURE_KEYS:
                past_vec.append(float(past_feats.get(k, 0.0)))
            past_arr = np.array(past_vec, dtype=np.float32)
            norm_past = np.linalg.norm(past_arr) + 1e-6

            # 1. Feature Distance (Cosine Distance normalized to [0, 1])
            dot_prod = np.dot(cur_arr, past_arr)
            cos_sim = max(0.0, min(1.0, float(dot_prod / (norm_cur * norm_past))))
            feat_dist = 1.0 - cos_sim

            # 2. Regime Match
            reg_match = self.calculate_regime_match(regime_str, exp.get("regime", "NEUTRAL"))

            # 3. Time-of-Day Match
            tod_match = self.calculate_tod_match(current_tod, exp.get("time_of_day_bucket", "09:30-11:30"))

            # 4. Liquidity Match (RVOL / Volume diff)
            cur_rvol = float(current_features.get("rvol", 1.0))
            past_rvol = float(past_feats.get("rvol", 1.0))
            rvol_ratio = min(cur_rvol, past_rvol) / max(cur_rvol, past_rvol, 0.01)
            liq_match = max(0.2, min(1.0, rvol_ratio))

            # 5. Recency Weight (Section 13)
            try:
                dt_past = datetime.fromisoformat(exp.get("event_time", ""))
            except Exception:
                dt_past = now - timedelta(days=30)
            rec_weight = self.calculate_recency_weight(dt_past, now)

            # Section 16: Composite Similarity Score
            # 복합 일치도 = cos_sim * reg_match * tod_match * liq_match * rec_weight
            composite_score = cos_sim * reg_match * tod_match * liq_match * rec_weight
            composite_dist = 1.0 / (1.0 + composite_score)

            matches.append({
                "composite_score": composite_score,
                "composite_dist": composite_dist,
                "reg_match": reg_match,
                "tod_match": tod_match,
                "target_hit_first": exp.get("target_hit_first", False),
                "outcome_cat": exp.get("outcome_category", ""),
                "realized_net_r": exp.get("realized_net_r", 0.0),
                "max_mfe": exp.get("max_mfe_pct", 0.0),
                "max_mae": exp.get("max_mae_pct", 0.0),
                "weight": rec_weight
            })

        if not matches:
            out = SimilarityMetaOutput(
                hist_win_rate=0.50, hist_target_rate=0.50, hist_stop_rate=0.50,
                expected_r=0.0, avg_mfe=0.0, avg_mae=0.0, sample_count=0,
                regime_match=1.0, time_of_day_match=1.0, similarity_distance=1.0,
                is_sufficient_sample=False
            )
            if hasattr(self, "_query_cache"):
                self._query_cache[cache_key] = out
            return out

        # 복합 점수 상위 top_k 선택
        matches.sort(key=lambda m: m["composite_score"], reverse=True)
        top_matches = matches[:top_k]
        n_samples = len(top_matches)

        total_weight = sum(m["weight"] for m in top_matches) or 1.0
        weighted_win = sum(m["weight"] for m in top_matches if m["target_hit_first"]) / total_weight
        weighted_r = sum(m["weight"] * m["realized_net_r"] for m in top_matches) / total_weight
        weighted_mfe = sum(m["weight"] * m["max_mfe"] for m in top_matches) / total_weight
        weighted_mae = sum(m["weight"] * m["max_mae"] for m in top_matches) / total_weight
        weighted_regime = sum(m["weight"] * m["reg_match"] for m in top_matches) / total_weight
        weighted_tod = sum(m["weight"] * m["tod_match"] for m in top_matches) / total_weight
        avg_dist = sum(m["composite_dist"] for m in top_matches) / n_samples

        is_sufficient = n_samples >= self.min_samples

        output = SimilarityMetaOutput(
            hist_win_rate=round(weighted_win, 4),
            hist_target_rate=round(weighted_win, 4),
            hist_stop_rate=round(1.0 - weighted_win, 4),
            expected_r=round(weighted_r, 4),
            avg_mfe=round(weighted_mfe, 4),
            avg_mae=round(weighted_mae, 4),
            sample_count=n_samples,
            regime_match=round(weighted_regime, 4),
            time_of_day_match=round(weighted_tod, 4),
            similarity_distance=round(avg_dist, 4),
            is_sufficient_sample=is_sufficient
        )

        # 5. Cache result (with max size 1000)
        if hasattr(self, "_query_cache"):
            if len(self._query_cache) >= 1000:
                self._query_cache.clear()
            self._query_cache[cache_key] = output

        return output

    @classmethod
    def check_feature_correlation(
        cls,
        base_features: List[Dict[str, float]],
        similarity_values: List[float],
        threshold: float = 0.85
    ) -> Dict[str, Any]:
        """
        Section 7: Historical Similarity Feature Correlation & Redundancy Test
        ML Feature와 과도한 상관관계(>0.85)가 발견되면 중복 경고
        """
        if len(base_features) < 10 or len(similarity_values) < 10:
            return {"has_leakage": False, "high_corr_features": [], "max_corr": 0.0}

        sim_arr = np.array(similarity_values, dtype=np.float64)
        sim_std = np.std(sim_arr)
        if sim_std < 1e-6:
            return {"has_leakage": False, "high_corr_features": [], "max_corr": 0.0}

        high_corr = []
        max_corr = 0.0
        keys = list(base_features[0].keys())

        for k in keys:
            col = np.array([f.get(k, 0.0) for f in base_features], dtype=np.float64)
            c_std = np.std(col)
            if c_std < 1e-6:
                continue
            corr = abs(float(np.corrcoef(col, sim_arr)[0, 1]))
            if not math.isnan(corr):
                max_corr = max(max_corr, corr)
                if corr >= threshold:
                    high_corr.append((k, round(corr, 4)))

        return {
            "has_leakage": len(high_corr) > 0,
            "high_corr_features": high_corr,
            "max_corr": round(max_corr, 4)
        }
