"""v16.0 Retrospective Experience Persistence (database/persistence.py)
ExperienceDB & ExperienceRecord interfacing with:
- data/experience_memory.db (experience_snapshots, experience_outcomes, retrospective_labels)
- data/operational_v16.db (no_trade_records, orders, fills)
- data/trade_history_v7.db (trades)
"""

import os
import json
import sqlite3
import logging
from datetime import datetime, timedelta
from typing import Dict, Any, List, Optional, Tuple
from dataclasses import dataclass, field, asdict

logger = logging.getLogger("ExperienceDB")


@dataclass
class ExperienceRecord:
    event_id: str
    iem_cd: str
    decision: str  # BUY, NO_TRADE, WAIT, etc.
    decision_time: str  # ISO timestamp
    name: str = ""
    entry_price: float = 0.0
    raw_features: Dict[str, Any] = field(default_factory=dict)
    prediction: Dict[str, Any] = field(default_factory=dict)
    label: Optional[str] = None  # MISSED_WINNER, CORRECT_REJECT, WINNING_TRADE, FALSE_SIGNAL, NEUTRAL_REJECT
    future_return: Optional[float] = None
    metadata: Dict[str, Any] = field(default_factory=dict)


class ExperienceDB:
    def __init__(
        self,
        db_path: str = "data/experience_memory.db",
        operational_db: str = "data/operational_v16.db",
        trade_db: str = "data/trade_history_v7.db"
    ):
        self.db_path = db_path
        self.operational_db = operational_db
        self.trade_db = trade_db
        os.makedirs(os.path.dirname(self.db_path), exist_ok=True)
        self._init_tables()

    def _get_conn(self, path: str) -> sqlite3.Connection:
        conn = sqlite3.connect(path, timeout=30.0)
        conn.row_factory = sqlite3.Row
        return conn

    def _init_tables(self):
        try:
            with self._get_conn(self.db_path) as conn:
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
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                )
                """)
                conn.execute("""
                CREATE TABLE IF NOT EXISTS retrospective_labels (
                    event_id TEXT PRIMARY KEY,
                    iem_cd TEXT NOT NULL,
                    decision TEXT NOT NULL,
                    decision_time TEXT NOT NULL,
                    label TEXT NOT NULL,
                    future_return REAL NOT NULL,
                    raw_features TEXT,
                    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                )
                """)
                conn.execute("CREATE INDEX IF NOT EXISTS idx_snap_iem ON experience_snapshots(iem_cd)")
                conn.execute("CREATE INDEX IF NOT EXISTS idx_outcome_cat ON experience_outcomes(outcome_category)")
                conn.execute("CREATE INDEX IF NOT EXISTS idx_retro_iem ON retrospective_labels(iem_cd)")
                conn.execute("CREATE INDEX IF NOT EXISTS idx_retro_label ON retrospective_labels(label)")
                conn.execute("CREATE INDEX IF NOT EXISTS idx_retro_time ON retrospective_labels(decision_time)")
        except Exception as e:
            logger.error(f"Failed to init tables: {e}")

    def get_pending_labels(self, limit: int = 500) -> List[ExperienceRecord]:
        """
        라벨링이 아직 확정되지 않은 오늘/최근 의사결정(BUY, NO_TRADE 등) 레코드를 조회
        """
        records: List[ExperienceRecord] = []
        seen_event_ids = set()

        # 1. Check experience_snapshots that don't have retrospective_labels yet
        try:
            with self._get_conn(self.db_path) as conn:
                cur = conn.execute("""
                    SELECT s.event_id, s.iem_cd, s.name, s.event_time, s.decision,
                           s.raw_features, s.prediction, s.virtual_trade
                    FROM experience_snapshots s
                    LEFT JOIN retrospective_labels r ON s.event_id = r.event_id
                    WHERE r.event_id IS NULL
                    ORDER BY s.event_time DESC
                    LIMIT ?
                """, (limit,))
                rows = cur.fetchall()
                for row in rows:
                    eid = row["event_id"]
                    seen_event_ids.add(eid)
                    try:
                        raw_feat = json.loads(row["raw_features"]) if row["raw_features"] else {}
                    except Exception:
                        raw_feat = {}
                    try:
                        pred = json.loads(row["prediction"]) if row["prediction"] else {}
                    except Exception:
                        pred = {}
                    try:
                        vt = json.loads(row["virtual_trade"]) if row["virtual_trade"] else {}
                    except Exception:
                        vt = {}

                    entry_p = float(vt.get("entry_price", raw_feat.get("price", 0.0)))
                    records.append(ExperienceRecord(
                        event_id=eid,
                        iem_cd=row["iem_cd"],
                        decision=row["decision"],
                        decision_time=row["event_time"],
                        name=row["name"],
                        entry_price=entry_p,
                        raw_features=raw_feat,
                        prediction=pred,
                        label=None,
                        future_return=None,
                        metadata={"virtual_trade": vt}
                    ))
        except Exception as e:
            logger.warning(f"Error querying experience_snapshots: {e}")

        # 2. Check no_trade_records in operational_v16.db if fewer than limit
        if len(records) < limit and os.path.exists(self.operational_db):
            try:
                with self._get_conn(self.operational_db) as conn:
                    remaining = limit - len(records)
                    cur = conn.execute("""
                        SELECT record_id, iem_cd, name, timestamp, rule_score, ml_prob, expected_net_r, strategy_id
                        FROM no_trade_records
                        ORDER BY timestamp DESC
                        LIMIT ?
                    """, (remaining,))
                    rows = cur.fetchall()
                    for row in rows:
                        eid = f"OP_NT_{row['record_id']}"
                        if eid in seen_event_ids:
                            continue
                        seen_event_ids.add(eid)
                        records.append(ExperienceRecord(
                            event_id=eid,
                            iem_cd=row["iem_cd"],
                            decision="NO_TRADE",
                            decision_time=row["timestamp"],
                            name=row["name"] or "",
                            entry_price=0.0,
                            raw_features={
                                "rule_score": row["rule_score"],
                                "ml_prob": row["ml_prob"],
                                "expected_net_r": row["expected_net_r"]
                            },
                            prediction={
                                "ml_prob": row["ml_prob"],
                                "expected_net_r": row["expected_net_r"]
                            },
                            label=None,
                            future_return=None,
                            metadata={"strategy_id": row["strategy_id"]}
                        ))
            except Exception as e:
                logger.warning(f"Error querying operational_v16 no_trade_records: {e}")

        logger.info(f"Retrieved {len(records)} pending experience records for EOD labeling")
        return records

    def calculate_future_return(self, iem_cd: str, decision_time: str, horizon_minutes: int = 30) -> float:
        """
        의사결정 시점 이후(5분/30분/EOD) 실제 주가 수익률 계산
        """
        # 1. Check experience_outcomes for an existing outcome for this event / symbol
        try:
            with self._get_conn(self.db_path) as conn:
                cur = conn.execute("""
                    SELECT o.realized_net_r, o.max_mfe_pct, o.max_mae_pct, o.notes
                    FROM experience_outcomes o
                    JOIN experience_snapshots s ON o.event_id = s.event_id
                    WHERE s.iem_cd = ? AND s.event_time >= ?
                    ORDER BY s.event_time ASC
                    LIMIT 1
                """, (iem_cd, decision_time))
                row = cur.fetchone()
                if row:
                    # If max_mfe_pct or realized_net_r is available
                    if row["max_mfe_pct"] is not None and row["max_mfe_pct"] > 0:
                        return float(row["max_mfe_pct"])
                    if row["realized_net_r"] is not None:
                        # net R to return approximation: 1R ~ 2.0%
                        return float(row["realized_net_r"]) * 0.02

                # 2. Check subsequent snapshot of the same symbol
                cur2 = conn.execute("""
                    SELECT event_time, raw_features
                    FROM experience_snapshots
                    WHERE iem_cd = ? AND event_time > ?
                    ORDER BY event_time ASC
                    LIMIT 10
                """, (iem_cd, decision_time))
                sub_rows = cur2.fetchall()
                if sub_rows:
                    first_price = None
                    last_price = None
                    for sr in sub_rows:
                        try:
                            feat = json.loads(sr["raw_features"])
                            p = float(feat.get("price", feat.get("curr_price", 0)))
                            if p > 0:
                                if first_price is None:
                                    first_price = p
                                last_price = p
                        except Exception:
                            continue
                    if first_price and last_price and first_price > 0:
                        return (last_price - first_price) / first_price
        except Exception as e:
            logger.debug(f"calculate_future_return snapshot lookup error: {e}")

        # 3. Check trade_history_v7.db if this was a filled trade
        if os.path.exists(self.trade_db):
            try:
                with self._get_conn(self.trade_db) as conn:
                    cur = conn.execute("""
                        SELECT return_pct, pnl, exit_price, entry_price
                        FROM trades
                        WHERE symbol = ? AND entry_time >= ?
                        ORDER BY entry_time ASC
                        LIMIT 1
                    """, (iem_cd, decision_time[:10]))
                    t_row = cur.fetchone()
                    if t_row:
                        ret_pct = t_row["return_pct"]
                        if ret_pct is not None:
                            return float(ret_pct) / 100.0
                        if t_row["entry_price"] and t_row["entry_price"] > 0 and t_row["exit_price"]:
                            return (t_row["exit_price"] - t_row["entry_price"]) / t_row["entry_price"]
            except Exception as e:
                logger.debug(f"calculate_future_return trade lookup error: {e}")

        # Fallback: Default conservative return (0.005 = +0.5%)
        return 0.005

    def update_labels(self, records: List[ExperienceRecord]):
        """
        라벨링 완료된 레코드를 retrospective_labels 및 experience_outcomes에 영구 반영
        """
        if not records:
            return

        with self._get_conn(self.db_path) as conn:
            now_iso = datetime.now().isoformat()
            for r in records:
                if not r.label:
                    continue
                ret = float(r.future_return) if r.future_return is not None else 0.0

                # 1. Update retrospective_labels table
                conn.execute("""
                    INSERT OR REPLACE INTO retrospective_labels (
                        event_id, iem_cd, decision, decision_time, label, future_return, raw_features, updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """, (
                    r.event_id,
                    r.iem_cd,
                    r.decision,
                    r.decision_time,
                    r.label,
                    ret,
                    json.dumps(r.raw_features, ensure_ascii=False),
                    now_iso
                ))

                # 2. Update experience_outcomes table
                target_hit = 1 if ret >= 0.03 else 0
                stop_hit = 1 if ret <= -0.02 else 0
                target_hit_first = 1 if ret > 0 else 0
                max_mfe = max(ret, 0.0)
                max_mae = abs(min(ret, 0.0))
                realized_net_r = ret / 0.02

                conn.execute("""
                    INSERT OR REPLACE INTO experience_outcomes (
                        event_id, evaluated_at, horizon_reached, target_hit, stop_hit,
                        target_hit_first, max_mfe_pct, max_mae_pct, realized_net_r,
                        outcome_category, notes
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """, (
                    r.event_id,
                    now_iso,
                    "EOD_BATCH",
                    target_hit,
                    stop_hit,
                    target_hit_first,
                    round(max_mfe, 4),
                    round(max_mae, 4),
                    round(realized_net_r, 4),
                    r.label,
                    f"EOD Retrospective Label: {r.label}, Return: {ret:.4f}"
                ))

        logger.info(f"Successfully committed {len(records)} updated labels to database")

    def get_all_labeled_data(self, limit: int = 15000) -> List[Dict[str, Any]]:
        """
        전체 확정 라벨 데이터셋 반환 (Purged CV 및 재학습용)
        """
        labeled_dataset: List[Dict[str, Any]] = []

        try:
            with self._get_conn(self.db_path) as conn:
                # First query retrospective_labels
                cur = conn.execute("""
                    SELECT event_id, iem_cd, decision, decision_time, label, future_return, raw_features
                    FROM retrospective_labels
                    ORDER BY decision_time ASC
                    LIMIT ?
                """, (limit,))
                rows = cur.fetchall()
                for row in rows:
                    try:
                        rf = json.loads(row["raw_features"]) if row["raw_features"] else {}
                    except Exception:
                        rf = {}
                    lbl = row["label"]
                    ret = float(row["future_return"])
                    target = 1 if (lbl in ("WINNING_TRADE", "MISSED_WINNER") or ret >= 0.02) else 0
                    labeled_dataset.append({
                        "event_id": row["event_id"],
                        "iem_cd": row["iem_cd"],
                        "timestamp": row["decision_time"],
                        "decision": row["decision"],
                        "label": lbl,
                        "future_return": ret,
                        "target": target,
                        "features": rf
                    })

                # If retrospective_labels has fewer than limit, pull from existing experience_outcomes
                if len(labeled_dataset) < limit:
                    remaining = limit - len(labeled_dataset)
                    cur2 = conn.execute("""
                        SELECT o.event_id, s.iem_cd, s.event_time, s.decision, s.raw_features,
                               o.outcome_category, o.realized_net_r, o.max_mfe_pct, o.target_hit_first
                        FROM experience_outcomes o
                        JOIN experience_snapshots s ON o.event_id = s.event_id
                        ORDER BY s.event_time ASC
                        LIMIT ?
                    """, (remaining,))
                    rows2 = cur2.fetchall()
                    for row in rows2:
                        try:
                            rf = json.loads(row["raw_features"]) if row["raw_features"] else {}
                        except Exception:
                            rf = {}
                        cat = row["outcome_category"]
                        target = 1 if (row["target_hit_first"] == 1 or cat in ("MISSED_WINNER", "WINNING_TRADE")) else 0
                        ret = float(row["max_mfe_pct"] or (float(row["realized_net_r"] or 0) * 0.02))
                        labeled_dataset.append({
                            "event_id": row["event_id"],
                            "iem_cd": row["iem_cd"],
                            "timestamp": row["event_time"],
                            "decision": row["decision"],
                            "label": cat,
                            "future_return": ret,
                            "target": target,
                            "features": rf
                        })
        except Exception as e:
            logger.error(f"Error fetching labeled dataset: {e}")

        logger.info(f"Loaded {len(labeled_dataset)} total labeled samples for retraining")
        return labeled_dataset
