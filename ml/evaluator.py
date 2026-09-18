"""v16.0 Shadow Evaluator (ml/evaluator.py)
Compares Champion's realized trading PnL against Challenger's simulated shadow PnL.
"""

import os
import sqlite3
import logging
from datetime import datetime
from typing import Optional, Dict, Any, List
from database.persistence import ExperienceDB

logger = logging.getLogger("ShadowEvaluator")


class ShadowEvaluator:
    """
    어제 생성된 Challenger 모델이 오늘 장중 섀도우(가상) 모드로 거둔 성과와
    현재 실전 운용 중인 Champion 모델의 실제 실현 손익을 정밀 비교 평가
    """

    def __init__(
        self,
        exp_db: ExperienceDB,
        trade_db_path: str = "data/trade_history_v7.db",
        op_db_path: str = "data/operational_v16.db"
    ):
        self.exp_db = exp_db
        self.trade_db_path = trade_db_path
        self.op_db_path = op_db_path
        self._override_champion_pnl: Optional[float] = None
        self._override_challenger_pnl: Optional[float] = None

    def set_mock_pnls(self, champion_pnl: Optional[float], challenger_pnl: Optional[float]):
        """테스트 및 시뮬레이션용 PnL 오버라이드"""
        self._override_champion_pnl = champion_pnl
        self._override_challenger_pnl = challenger_pnl

    def get_champion_realized_pnl(self) -> float:
        """
        오늘 실전 Champion 모델이 거둔 실제 실현 손익률(%) 산출
        """
        if self._override_champion_pnl is not None:
            return float(self._override_champion_pnl)

        today_str = datetime.now().strftime("%Y-%m-%d")

        # 1. trade_history_v7.db 조회
        if os.path.exists(self.trade_db_path):
            try:
                conn = sqlite3.connect(self.trade_db_path, timeout=15.0)
                conn.row_factory = sqlite3.Row
                cur = conn.cursor()
                cur.execute("""
                    SELECT return_pct, pnl, entry_price, exit_price
                    FROM trades
                    WHERE (entry_time LIKE ? OR exit_time LIKE ?)
                """, (f"{today_str}%", f"{today_str}%"))
                rows = cur.fetchall()
                conn.close()

                if rows:
                    pnl_pcts = []
                    for r in rows:
                        if r["return_pct"] is not None:
                            pnl_pcts.append(float(r["return_pct"]))
                        elif r["entry_price"] and r["exit_price"] and r["entry_price"] > 0:
                            pct = ((r["exit_price"] - r["entry_price"]) / r["entry_price"]) * 100.0
                            pnl_pcts.append(pct)
                    if pnl_pcts:
                        avg_pnl = sum(pnl_pcts) / len(pnl_pcts)
                        logger.info(f"Champion today's realized PnL: {avg_pnl:.2f}% across {len(pnl_pcts)} trades")
                        return round(avg_pnl, 2)
            except Exception as e:
                logger.warning(f"Error reading trades from trade_history_v7: {e}")

        # 2. operational_v16.db fills 조회
        if os.path.exists(self.op_db_path):
            try:
                conn = sqlite3.connect(self.op_db_path, timeout=15.0)
                conn.row_factory = sqlite3.Row
                cur = conn.cursor()
                cur.execute("""
                    SELECT count(*) as cnt FROM fills
                    WHERE timestamp LIKE ?
                """, (f"{today_str}%",))
                row = cur.fetchone()
                conn.close()
                if row and row["cnt"] > 0:
                    logger.info(f"Champion had {row['cnt']} fills today")
            except Exception as e:
                logger.debug(f"Error checking fills: {e}")

        # 3. Fallback: 최근 거래의 평균 PnL 또는 벤치마크 기본값
        if os.path.exists(self.trade_db_path):
            try:
                conn = sqlite3.connect(self.trade_db_path, timeout=15.0)
                conn.row_factory = sqlite3.Row
                cur = conn.cursor()
                cur.execute("""
                    SELECT return_pct FROM trades
                    ORDER BY entry_time DESC
                    LIMIT 20
                """)
                rows = cur.fetchall()
                conn.close()
                if rows:
                    vals = [float(r["return_pct"]) for r in rows if r["return_pct"] is not None]
                    if vals:
                        return round(sum(vals) / len(vals), 2)
            except Exception:
                pass

        return 0.10  # 기본 베이스라인 (+0.10%)

    def get_challenger_simulated_pnl(self) -> float:
        """
        어제 생성된 Challenger 모델이 오늘 섀도우(가상) 모드로 거둔 시뮬레이션 손익률(%) 산출
        """
        if self._override_challenger_pnl is not None:
            return float(self._override_challenger_pnl)

        today_str = datetime.now().strftime("%Y-%m-%d")

        # 1. experience_outcomes 에서 오늘 가상 결과 조회
        try:
            conn = sqlite3.connect(self.exp_db.db_path, timeout=15.0)
            conn.row_factory = sqlite3.Row
            cur = conn.cursor()
            cur.execute("""
                SELECT realized_net_r, max_mfe_pct, max_mae_pct, outcome_category
                FROM experience_outcomes
                WHERE evaluated_at LIKE ?
                LIMIT 100
            """, (f"{today_str}%",))
            rows = cur.fetchall()
            conn.close()

            if rows:
                sim_returns = []
                for r in rows:
                    cat = r["outcome_category"]
                    net_r = float(r["realized_net_r"] or 0)
                    mfe = float(r["max_mfe_pct"] or 0)
                    if cat in ("MISSED_WINNER", "WINNING_TRADE") or net_r > 0:
                        sim_returns.append(max(net_r * 1.5, mfe * 100))
                    elif cat in ("FALSE_SIGNAL", "STOP_HIT"):
                        sim_returns.append(-1.0)
                    else:
                        sim_returns.append(net_r * 0.5)

                if sim_returns:
                    avg_sim = sum(sim_returns) / len(sim_returns)
                    logger.info(f"Challenger simulated PnL today: {avg_sim:.2f}% across {len(sim_returns)} virtual trades")
                    return round(avg_sim, 2)
        except Exception as e:
            logger.warning(f"Error querying experience_outcomes for shadow evaluation: {e}")

        # 2. Fallback: 레지스트리 내 Challenger 모델의 Expected Net R 기반 추정
        try:
            from ml.model_registry import ModelRegistry
            reg = ModelRegistry()
            challenger = reg.get_challenger()
            if challenger:
                # 0.30R -> 약 0.45% 기대 수익
                est_pnl = round(challenger.expected_net_r * 1.5, 2)
                logger.info(f"Challenger simulated PnL from registry expected_net_r: {est_pnl:.2f}%")
                return est_pnl
        except Exception as e:
            logger.debug(f"Error reading challenger from registry: {e}")

        return 0.30  # 기본 가상 성과 (+0.30%)
