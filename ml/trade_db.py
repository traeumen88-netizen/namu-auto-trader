"""v7.0 Trade Database & Post-Trade Analytics (ml/trade_db.py)
Stores complete trade history, model predictions, excursion metrics (MAE/MFE),
and categorizes bad trades for automated reinforcement and retraining.
"""

import sqlite3
import json
import os
from datetime import datetime
from typing import Dict, Any, List, Optional
from dataclasses import dataclass, asdict


@dataclass
class TradeRecord:
    trade_id: str
    symbol: str
    symbol_name: str
    setup_name: str
    time_horizon: str
    side: str
    entry_time: str
    exit_time: str
    entry_price: float
    exit_price: float
    shares: int
    stop_price: float
    target_price: float
    pnl: float
    return_pct: float
    r_multiple: float
    mae_pct: float
    mfe_pct: float
    mae_r: float
    mfe_r: float
    holding_seconds: float
    model_version: str
    p_target_pred: float
    p_stop_pred: float
    expected_net_r_pred: float
    bad_trade_category: str
    raw_features: Optional[str] = "{}"


class TradeDatabase:
    """
    Persistent SQLite trade database with excursion tracking & bad trade categorization.
    """

    BAD_TRADE_CATEGORIES = [
        "PROFIT_TARGET",
        "NORMAL_STOP",
        "FAKE_BREAKOUT",
        "LATE_ENTRY",
        "LOW_LIQUIDITY",
        "MARKET_SHOCK"
    ]

    def __init__(self, db_path: str = "data/trade_history_v7.db"):
        self.db_path = db_path
        os.makedirs(os.path.dirname(self.db_path), exist_ok=True)
        self._init_db()

    def _get_connection(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        return conn

    def _init_db(self):
        with self._get_connection() as conn:
            conn.execute("""
            CREATE TABLE IF NOT EXISTS trades (
                trade_id TEXT PRIMARY KEY,
                symbol TEXT NOT NULL,
                symbol_name TEXT,
                setup_name TEXT NOT NULL,
                time_horizon TEXT NOT NULL,
                side TEXT NOT NULL,
                entry_time TEXT NOT NULL,
                exit_time TEXT NOT NULL,
                entry_price REAL NOT NULL,
                exit_price REAL NOT NULL,
                shares INTEGER NOT NULL,
                stop_price REAL NOT NULL,
                target_price REAL NOT NULL,
                pnl REAL NOT NULL,
                return_pct REAL NOT NULL,
                r_multiple REAL NOT NULL,
                mae_pct REAL NOT NULL,
                mfe_pct REAL NOT NULL,
                mae_r REAL NOT NULL,
                mfe_r REAL NOT NULL,
                holding_seconds REAL NOT NULL,
                model_version TEXT NOT NULL,
                p_target_pred REAL NOT NULL,
                p_stop_pred REAL NOT NULL,
                expected_net_r_pred REAL NOT NULL,
                bad_trade_category TEXT NOT NULL,
                raw_features TEXT,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
            """)
            conn.execute("CREATE INDEX IF NOT EXISTS idx_trades_symbol ON trades (symbol)")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_trades_setup ON trades (setup_name)")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_trades_category ON trades (bad_trade_category)")
            conn.commit()

    @staticmethod
    def categorize_trade(
        r_multiple: float,
        mfe_r: float,
        chase_ratio: float = 0.0,
        slippage_pct: float = 0.0,
        market_shock: bool = False
    ) -> str:
        """
        Categorizes completed trade based on execution & excursion metrics.
        """
        if market_shock:
            return "MARKET_SHOCK"
        if r_multiple > 0:
            return "PROFIT_TARGET"
        # Loss cases
        if chase_ratio > 0.015:
            return "LATE_ENTRY"
        if slippage_pct > 0.003:
            return "LOW_LIQUIDITY"
        if mfe_r < 0.3:
            # Reversal right after entry without gaining momentum
            return "FAKE_BREAKOUT"
        return "NORMAL_STOP"

    def record_trade(self, record: TradeRecord):
        """
        Inserts or replaces a trade record into the database.
        """
        with self._get_connection() as conn:
            conn.execute("""
            INSERT OR REPLACE INTO trades (
                trade_id, symbol, symbol_name, setup_name, time_horizon, side,
                entry_time, exit_time, entry_price, exit_price, shares,
                stop_price, target_price, pnl, return_pct, r_multiple,
                mae_pct, mfe_pct, mae_r, mfe_r, holding_seconds,
                model_version, p_target_pred, p_stop_pred, expected_net_r_pred,
                bad_trade_category, raw_features
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (
                record.trade_id, record.symbol, record.symbol_name, record.setup_name,
                record.time_horizon, record.side, record.entry_time, record.exit_time,
                record.entry_price, record.exit_price, record.shares, record.stop_price,
                record.target_price, record.pnl, record.return_pct, record.r_multiple,
                record.mae_pct, record.mfe_pct, record.mae_r, record.mfe_r,
                record.holding_seconds, record.model_version, record.p_target_pred,
                record.p_stop_pred, record.expected_net_r_pred, record.bad_trade_category,
                record.raw_features
            ))
            conn.commit()

    def get_recent_trades(self, limit: int = 100) -> List[Dict[str, Any]]:
        with self._get_connection() as conn:
            cursor = conn.execute("SELECT * FROM trades ORDER BY exit_time DESC LIMIT ?", (limit,))
            return [dict(row) for row in cursor.fetchall()]

    def get_bad_trades(self, category: Optional[str] = None) -> List[Dict[str, Any]]:
        with self._get_connection() as conn:
            if category:
                cursor = conn.execute("SELECT * FROM trades WHERE bad_trade_category = ? ORDER BY exit_time DESC", (category,))
            else:
                cursor = conn.execute("SELECT * FROM trades WHERE bad_trade_category NOT IN ('PROFIT_TARGET') ORDER BY exit_time DESC")
            return [dict(row) for row in cursor.fetchall()]

    def get_performance_summary(self, model_version: Optional[str] = None) -> Dict[str, Any]:
        """
        Calculates performance summary, win rate, average R, profit factor, and Brier calibration score.
        """
        with self._get_connection() as conn:
            query = "SELECT * FROM trades"
            params = []
            if model_version:
                query += " WHERE model_version = ?"
                params.append(model_version)
            cursor = conn.execute(query, tuple(params))
            rows = [dict(r) for r in cursor.fetchall()]

        if not rows:
            return {
                "total_trades": 0,
                "win_rate": 0.0,
                "profit_factor": 0.0,
                "avg_r": 0.0,
                "total_pnl": 0.0,
                "brier_score": 0.0,
                "bad_trade_breakdown": {}
            }

        total_trades = len(rows)
        wins = [r for r in rows if r["pnl"] > 0]
        losses = [r for r in rows if r["pnl"] <= 0]
        win_rate = len(wins) / total_trades if total_trades > 0 else 0.0

        gross_profit = sum(r["pnl"] for r in wins)
        gross_loss = abs(sum(r["pnl"] for r in losses))
        profit_factor = (gross_profit / gross_loss) if gross_loss > 0 else 999.0

        avg_r = sum(r["r_multiple"] for r in rows) / total_trades
        total_pnl = sum(r["pnl"] for r in rows)

        # Brier score calculation: mean((p_target_pred - outcome)^2) where outcome = 1 if win else 0
        brier_sum = sum((r["p_target_pred"] - (1.0 if r["pnl"] > 0 else 0.0)) ** 2 for r in rows)
        brier_score = brier_sum / total_trades

        # Bad trade distribution
        breakdown = {}
        for r in rows:
            cat = r["bad_trade_category"]
            breakdown[cat] = breakdown.get(cat, 0) + 1

        return {
            "total_trades": total_trades,
            "win_rate": round(win_rate, 4),
            "profit_factor": round(profit_factor, 2),
            "avg_r": round(avg_r, 4),
            "total_pnl": round(total_pnl, 2),
            "brier_score": round(brier_score, 4),
            "bad_trade_breakdown": breakdown
        }
