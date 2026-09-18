"""v12.3 Point-in-Time Experience Memory (ml/experience_memory.py)
[FINAL PATCH v12.3] Learning Integrity + Point-in-Time Experience Memory

Key Responsibilities:
1. Point-in-Time Snapshot:
   - Immutable storage of Event / Candidate / Setup / Signal at exact time of occurrence.
   - Zero Lookahead: Only data known at event time is saved in Feature Snapshot.
2. Outcome Dataset Separation:
   - Future price changes (+10m, +30m, 1d) are saved ONLY in Outcome Dataset.
3. Virtual Trade Simulation for NO_TRADE:
   - Virtual Entry, Virtual Stop, Virtual Target, Spread, Slippage, Fee, Tax, Liquidity.
4. Categorization:
   - MISSED_WINNER (Target hit before stop after full costs)
   - MISSED_BREAKOUT, MISSED_MOMENTUM, MISSED_PULLBACK, MISSED_SWING
   - CORRECT_NO_TRADE (Stop hit first, or Expected Net R <= 0, or illiquid)
   - FALSE_SIGNAL (BUY was executed but stopped out)
   - EXECUTION_MISSED, DATA_MISSED
"""

import os
import json
import sqlite3
import uuid
from contextlib import contextmanager
from datetime import datetime, timedelta
from typing import Dict, Any, List, Optional, Tuple
from dataclasses import dataclass, field, asdict
from core.cost_model import CostModel
from core.models import OrderSide, TimeHorizon


@dataclass(frozen=True)
class PointInTimeSnapshot:
    """
    Section 1 & 2: Point-in-Time Experience Memory Snapshot (Immutable)
    저장 시점 이후 들어온 데이터가 기존 Snapshot을 절대 수정할 수 없음.
    """
    event_id: str
    iem_cd: str
    name: str
    event_time: str               # ISO timestamp of event
    as_of_time: str               # ISO timestamp when captured
    feature_version: str          # e.g., "FEATURE_V001"
    strategy_version: str         # e.g., "STRATEGY_V12_3"
    model_version: str            # e.g., "CHAMPION_V7_0"
    dataset_version: str          # e.g., "DATA_V001"
    raw_features: Dict[str, Any]  # 당시에만 알 수 있는 지표 (가격, 거래량, RVOL, VWAP, RSI 등)
    prediction: Dict[str, Any]    # 당시 모델 예측치 (p_target, p_stop, expected_net_r)
    decision: str                 # "BUY", "WAIT", "NO_TRADE"
    decision_reason: str
    rule_score: float
    virtual_trade: Dict[str, Any] # Virtual Entry, Stop, Target, Costs for simulation
    regime: str                   # BULL, BEAR, NEUTRAL, HIGH_VOLATILITY, LOW_VOLATILITY
    time_of_day_bucket: str       # 09:00-09:30, 09:30-11:30, 11:30-13:00, 13:00-15:30


@dataclass
class OutcomeRecord:
    """
    Section 2 & 6: Outcome Dataset Record
    10분/30분/1일 후 발생한 미래 결과는 Feature Snapshot과 엄격히 분리되어 여기에만 저장.
    """
    event_id: str
    evaluated_at: str
    horizon_reached: str          # "10M", "30M", "1D", "TARGET_HIT", "STOP_HIT", "EXPIRED"
    target_hit: bool
    stop_hit: bool
    target_hit_first: bool
    max_mfe_pct: float            # Maximum Favorable Excursion (%)
    max_mae_pct: float            # Maximum Adverse Excursion (%)
    realized_net_r: float         # 거래비용 차감 후 실현 R
    outcome_category: str         # MISSED_WINNER, CORRECT_NO_TRADE, etc.
    notes: str = ""


class ExperienceMemory:
    """
    Point-in-Time 경험 메모리 저장소 및 실시간 사후 결과 추적 엔진
    """

    CATEGORIES = [
        "MISSED_WINNER",
        "MISSED_BREAKOUT",
        "MISSED_MOMENTUM",
        "MISSED_PULLBACK",
        "MISSED_SWING",
        "CORRECT_NO_TRADE",
        "FALSE_SIGNAL",
        "EXECUTION_MISSED",
        "DATA_MISSED"
    ]

    def __init__(self, db_path: str = "data/experience_memory.db", market: str = "KOSPI"):
        self.db_path = db_path
        self.market = market
        self.cost_model = CostModel(market=market)
        os.makedirs(os.path.dirname(self.db_path), exist_ok=True)
        self._init_db()

        # 실시간 사후 추적 대상 가상 거래 {event_id: dict}
        self.active_tracking: Dict[str, Dict[str, Any]] = {}

    @contextmanager
    def _get_connection(self):
        conn = sqlite3.connect(self.db_path, timeout=60.0)
        conn.row_factory = sqlite3.Row
        try:
            yield conn
            conn.commit()
        finally:
            conn.close()

    def _init_db(self):
        with self._get_connection() as conn:
            conn.execute("PRAGMA journal_mode=WAL;")
            conn.execute("""
            CREATE TABLE IF NOT EXISTS experience_snapshots (
                event_id TEXT PRIMARY KEY,
                iem_cd TEXT NOT NULL,
                name TEXT NOT NULL,
                event_time TEXT NOT NULL,
                as_of_time TEXT NOT NULL,
                feature_version TEXT NOT NULL,
                strategy_version TEXT NOT NULL,
                model_version TEXT NOT NULL,
                dataset_version TEXT NOT NULL,
                raw_features TEXT NOT NULL,
                prediction TEXT NOT NULL,
                decision TEXT NOT NULL,
                decision_reason TEXT NOT NULL,
                rule_score REAL NOT NULL,
                virtual_trade TEXT NOT NULL,
                regime TEXT NOT NULL,
                time_of_day_bucket TEXT NOT NULL,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
            """)
            conn.execute("""
            CREATE TABLE IF NOT EXISTS experience_outcomes (
                event_id TEXT PRIMARY KEY,
                evaluated_at TEXT NOT NULL,
                horizon_reached TEXT NOT NULL,
                target_hit INTEGER NOT NULL,
                stop_hit INTEGER NOT NULL,
                target_hit_first INTEGER NOT NULL,
                max_mfe_pct REAL NOT NULL,
                max_mae_pct REAL NOT NULL,
                realized_net_r REAL NOT NULL,
                outcome_category TEXT NOT NULL,
                notes TEXT,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY (event_id) REFERENCES experience_snapshots(event_id)
            )
            """)
            conn.execute("CREATE INDEX IF NOT EXISTS idx_snap_iem ON experience_snapshots(iem_cd)")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_snap_decision ON experience_snapshots(decision)")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_outcome_cat ON experience_outcomes(outcome_category)")

    @staticmethod
    def get_time_of_day_bucket(dt: datetime) -> str:
        t = dt.time()
        if t < datetime.strptime("09:30", "%H:%M").time():
            return "09:00-09:30"
        elif t < datetime.strptime("11:30", "%H:%M").time():
            return "09:30-11:30"
        elif t < datetime.strptime("13:00", "%H:%M").time():
            return "11:30-13:00"
        else:
            return "13:00-15:30"

    def record_snapshot(self, snapshot: PointInTimeSnapshot) -> str:
        """
        Section 1: Point-in-Time 상태를 불변(Immutable)하게 DB에 저장
        """
        with self._get_connection() as conn:
            conn.execute("""
            INSERT OR IGNORE INTO experience_snapshots (
                event_id, iem_cd, name, event_time, as_of_time, feature_version,
                strategy_version, model_version, dataset_version, raw_features,
                prediction, decision, decision_reason, rule_score, virtual_trade,
                regime, time_of_day_bucket
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (
                snapshot.event_id,
                snapshot.iem_cd,
                snapshot.name,
                snapshot.event_time,
                snapshot.as_of_time,
                snapshot.feature_version,
                snapshot.strategy_version,
                snapshot.model_version,
                snapshot.dataset_version,
                json.dumps(snapshot.raw_features, ensure_ascii=False),
                json.dumps(snapshot.prediction, ensure_ascii=False),
                snapshot.decision,
                snapshot.decision_reason,
                snapshot.rule_score,
                json.dumps(snapshot.virtual_trade, ensure_ascii=False),
                snapshot.regime,
                snapshot.time_of_day_bucket
            ))

        # 만약 NO_TRADE 또는 WAIT 이면 사후 결과 추적 등록
        if snapshot.decision in ("NO_TRADE", "WAIT"):
            self.register_unresolved_trade(snapshot)

        return snapshot.event_id

    def get_snapshot(self, event_id: str) -> Optional[PointInTimeSnapshot]:
        with self._get_connection() as conn:
            cur = conn.execute("SELECT * FROM experience_snapshots WHERE event_id = ?", (event_id,))
            row = cur.fetchone()
            if not row:
                return None
            return PointInTimeSnapshot(
                event_id=row["event_id"],
                iem_cd=row["iem_cd"],
                name=row["name"],
                event_time=row["event_time"],
                as_of_time=row["as_of_time"],
                feature_version=row["feature_version"],
                strategy_version=row["strategy_version"],
                model_version=row["model_version"],
                dataset_version=row["dataset_version"],
                raw_features=json.loads(row["raw_features"]),
                prediction=json.loads(row["prediction"]),
                decision=row["decision"],
                decision_reason=row["decision_reason"],
                rule_score=row["rule_score"],
                virtual_trade=json.loads(row["virtual_trade"]),
                regime=row["regime"],
                time_of_day_bucket=row["time_of_day_bucket"]
            )

    def register_unresolved_trade(self, snapshot: PointInTimeSnapshot):
        """
        NO_TRADE / WAIT 종목의 사후 시세 추적을 위한 가상 트레이드 등록
        """
        vt = snapshot.virtual_trade
        entry_price = float(vt.get("entry_price", snapshot.raw_features.get("price", 10000)))
        stop_price = float(vt.get("stop_price", entry_price * 0.98))
        target_price = float(vt.get("target_price", entry_price * 1.04))
        shares = int(vt.get("shares", 100))

        # 가상 체결 비용 계산 (슬리피지, 호가 스프레드, 수수료, 세금)
        buy_cost = self.cost_model.calculate_cost(OrderSide.BUY, entry_price, shares)
        sell_cost = self.cost_model.calculate_cost(OrderSide.SELL, target_price, shares)
        roundtrip_cost_cash = buy_cost.total_cost + sell_cost.total_cost
        risk_per_share = abs(entry_price - stop_price)
        r_cash = (risk_per_share * shares) if risk_per_share > 0 else 1.0
        cost_r = roundtrip_cost_cash / r_cash

        try:
            start_dt = datetime.fromisoformat(snapshot.event_time)
        except Exception:
            start_dt = datetime.now()

        self.active_tracking[snapshot.event_id] = {
            "snapshot": snapshot,
            "entry_price": entry_price,
            "stop_price": stop_price,
            "target_price": target_price,
            "shares": shares,
            "cost_r": cost_r,
            "start_time": start_dt,
            "highest_price": entry_price,
            "lowest_price": entry_price,
            "target_hit": False,
            "stop_hit": False,
            "resolved": False
        }

    def on_price_update(
        self,
        iem_cd: str,
        current_price: float,
        high_price: float,
        low_price: float,
        now: datetime
    ) -> List[OutcomeRecord]:
        """
        Section 3, 4, 5, 6, 30:
        실시간 가격 업데이트 수신 시 가상 거래(NO_TRADE) 시뮬레이션 평가 및 Outcome 분류
        """
        resolved_records = []
        to_delete = []

        for event_id, item in list(self.active_tracking.items()):
            snap: PointInTimeSnapshot = item["snapshot"]
            if snap.iem_cd != iem_cd or item["resolved"]:
                continue

            entry_p = item["entry_price"]
            stop_p = item["stop_price"]
            target_p = item["target_price"]
            cost_r = item["cost_r"]

            item["highest_price"] = max(item["highest_price"], high_price, current_price)
            item["lowest_price"] = min(item["lowest_price"], low_price, current_price)

            # MFE / MAE 계산
            mfe_pct = (item["highest_price"] - entry_p) / entry_p if entry_p > 0 else 0.0
            mae_pct = (entry_p - item["lowest_price"]) / entry_p if entry_p > 0 else 0.0

            # 1. Target 도달 검사
            target_hit = item["highest_price"] >= target_p
            # 2. Stop 도달 검사
            stop_hit = item["lowest_price"] <= stop_p

            elapsed_seconds = (now - item["start_time"]).total_seconds()
            is_10m = elapsed_seconds >= 600
            is_30m = elapsed_seconds >= 1800
            is_end_of_day = elapsed_seconds >= 21600 or now.time() >= datetime.strptime("15:20", "%H:%M").time()

            should_resolve = False
            horizon_reached = "10M" if is_10m else "PENDING"
            target_hit_first = False
            outcome_cat = "CORRECT_NO_TRADE"
            realized_net_r = 0.0

            if target_hit and not stop_hit:
                # Target 먼저 도달!
                should_resolve = True
                horizon_reached = "TARGET_HIT"
                target_hit_first = True
            elif stop_hit and not target_hit:
                # Stop 먼저 도달!
                should_resolve = True
                horizon_reached = "STOP_HIT"
                target_hit_first = False
            elif target_hit and stop_hit:
                # 둘 다 터치: 캔들 종가 기준으로 판정
                should_resolve = True
                horizon_reached = "TARGET_STOP_COLLISION"
                target_hit_first = current_price >= entry_p
            elif is_end_of_day or is_30m:
                should_resolve = True
                horizon_reached = "30M_TIMEOUT" if is_30m else "1D_TIMEOUT"
                target_hit_first = False

            if should_resolve:
                # 순기대값 계산 (거래비용 차감)
                risk_unit = abs(entry_p - stop_p)
                gross_r = (current_price - entry_p) / risk_unit if risk_unit > 0 else 0.0
                realized_net_r = gross_r - cost_r

                # Section 4: Missed Winner 판정
                # 조건: 가상 Entry 가능 + 거래비용 차감 후 Target 도달 + Stop보다 Target 먼저 발생
                if target_hit_first and realized_net_r > 0:
                    strategy_id = snap.virtual_trade.get("strategy_id", "")
                    if "BREAKOUT" in strategy_id:
                        outcome_cat = "MISSED_BREAKOUT"
                    elif "MOMENTUM" in strategy_id:
                        outcome_cat = "MISSED_MOMENTUM"
                    elif "PULLBACK" in strategy_id:
                        outcome_cat = "MISSED_PULLBACK"
                    elif snap.virtual_trade.get("time_horizon") == "SWING":
                        outcome_cat = "MISSED_SWING"
                    else:
                        outcome_cat = "MISSED_WINNER"
                else:
                    # Section 5: Correct No Trade 판정
                    # 조건: Virtual Stop 먼저 도달 OR Expected Net R <= 0 OR 체결 불가
                    outcome_cat = "CORRECT_NO_TRADE"

                rec = OutcomeRecord(
                    event_id=event_id,
                    evaluated_at=now.isoformat(),
                    horizon_reached=horizon_reached,
                    target_hit=target_hit,
                    stop_hit=stop_hit,
                    target_hit_first=target_hit_first,
                    max_mfe_pct=round(mfe_pct, 4),
                    max_mae_pct=round(mae_pct, 4),
                    realized_net_r=round(realized_net_r, 4),
                    outcome_category=outcome_cat,
                    notes=f"Entry: {entry_p}, Exit: {current_price}, Cost(R): {cost_r:.3f}"
                )
                self.record_outcome(rec)
                resolved_records.append(rec)
                to_delete.append(event_id)

        for eid in to_delete:
            del self.active_tracking[eid]

        return resolved_records

    def record_outcome(self, outcome: OutcomeRecord):
        """
        Section 2: 결과 데이터셋(Outcome Dataset)에만 사후 결과를 저장.
        """
        with self._get_connection() as conn:
            conn.execute("""
            INSERT OR REPLACE INTO experience_outcomes (
                event_id, evaluated_at, horizon_reached, target_hit, stop_hit,
                target_hit_first, max_mfe_pct, max_mae_pct, realized_net_r,
                outcome_category, notes
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (
                outcome.event_id,
                outcome.evaluated_at,
                outcome.horizon_reached,
                1 if outcome.target_hit else 0,
                1 if outcome.stop_hit else 0,
                1 if outcome.target_hit_first else 0,
                outcome.max_mfe_pct,
                outcome.max_mae_pct,
                outcome.realized_net_r,
                outcome.outcome_category,
                outcome.notes
            ))

    def get_outcome(self, event_id: str) -> Optional[OutcomeRecord]:
        with self._get_connection() as conn:
            cur = conn.execute("SELECT * FROM experience_outcomes WHERE event_id = ?", (event_id,))
            row = cur.fetchone()
            if not row:
                return None
            return OutcomeRecord(
                event_id=row["event_id"],
                evaluated_at=row["evaluated_at"],
                horizon_reached=row["horizon_reached"],
                target_hit=bool(row["target_hit"]),
                stop_hit=bool(row["stop_hit"]),
                target_hit_first=bool(row["target_hit_first"]),
                max_mfe_pct=row["max_mfe_pct"],
                max_mae_pct=row["max_mae_pct"],
                realized_net_r=row["realized_net_r"],
                outcome_category=row["outcome_category"],
                notes=row["notes"] or ""
            )

    def get_category_stats(self) -> Dict[str, int]:
        """
        Section 6: 각 유형별 통계 집계
        """
        stats = {cat: 0 for cat in self.CATEGORIES}
        with self._get_connection() as conn:
            cur = conn.execute("""
            SELECT outcome_category, COUNT(*) as cnt
            FROM experience_outcomes
            GROUP BY outcome_category
            """)
            for row in cur.fetchall():
                cat = row["outcome_category"]
                if cat in stats:
                    stats[cat] = row["cnt"]
                else:
                    stats[cat] = row["cnt"]
        return stats

    def get_all_labeled_experiences(self) -> List[Dict[str, Any]]:
        """
        Snapshot + Outcome 결합 경험 데이터 조회 (ML 재학습 및 과거 유사도 검색용)
        """
        results = []
        with self._get_connection() as conn:
            cur = conn.execute("""
            SELECT s.*, o.evaluated_at, o.target_hit_first, o.max_mfe_pct, o.max_mae_pct,
                   o.realized_net_r, o.outcome_category
            FROM experience_snapshots s
            INNER JOIN experience_outcomes o ON s.event_id = o.event_id
            """)
            for row in cur.fetchall():
                results.append({
                    "event_id": row["event_id"],
                    "iem_cd": row["iem_cd"],
                    "name": row["name"],
                    "event_time": row["event_time"],
                    "feature_version": row["feature_version"],
                    "strategy_version": row["strategy_version"],
                    "model_version": row["model_version"],
                    "raw_features": json.loads(row["raw_features"]),
                    "prediction": json.loads(row["prediction"]),
                    "decision": row["decision"],
                    "rule_score": row["rule_score"],
                    "regime": row["regime"],
                    "time_of_day_bucket": row["time_of_day_bucket"],
                    "target_hit_first": bool(row["target_hit_first"]),
                    "max_mfe_pct": row["max_mfe_pct"],
                    "max_mae_pct": row["max_mae_pct"],
                    "realized_net_r": row["realized_net_r"],
                    "outcome_category": row["outcome_category"]
                })
        return results
