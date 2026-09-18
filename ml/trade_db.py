"""v7.0 Trade Database & Post-Trade Analytics (ml/trade_db.py)
Stores complete trade history, model predictions, excursion metrics (MAE/MFE),
and categorizes bad trades for automated reinforcement and retraining.
"""

import sqlite3
import json
import os
from datetime import datetime, timedelta
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
    exit_reason: Optional[str] = ""
    trading_mode: Optional[str] = "live"
    account_no: Optional[str] = ""
    signal_session: Optional[str] = "REGULAR"
    entry_session: Optional[str] = "REGULAR"
    entry_rvol: Optional[float] = None
    entry_momentum_3m: Optional[float] = None
    entry_vwap_gap: Optional[float] = None
    entry_rebound_strength: Optional[float] = None
    expected_move_pct: Optional[float] = None
    expected_net_r: Optional[float] = None


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
        conn = sqlite3.connect(self.db_path, timeout=60.0)
        conn.row_factory = sqlite3.Row
        return conn

    def _init_db(self):
        with self._get_connection() as conn:
            conn.execute("PRAGMA journal_mode=WAL;")
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
                exit_reason TEXT DEFAULT '',
                trading_mode TEXT DEFAULT 'live',
                account_no TEXT DEFAULT '',
                signal_session TEXT DEFAULT 'REGULAR',
                entry_session TEXT DEFAULT 'REGULAR',
                entry_rvol REAL DEFAULT NULL,
                entry_momentum_3m REAL DEFAULT NULL,
                entry_vwap_gap REAL DEFAULT NULL,
                entry_rebound_strength REAL DEFAULT NULL,
                expected_move_pct REAL DEFAULT NULL,
                expected_net_r REAL DEFAULT NULL,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
            """)
            conn.execute("CREATE INDEX IF NOT EXISTS idx_trades_symbol ON trades (symbol)")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_trades_setup ON trades (setup_name)")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_trades_category ON trades (bad_trade_category)")
            try:
                conn.execute("ALTER TABLE trades ADD COLUMN exit_reason TEXT DEFAULT ''")
            except sqlite3.OperationalError:
                pass
            try:
                conn.execute("ALTER TABLE trades ADD COLUMN trading_mode TEXT DEFAULT 'live'")
            except sqlite3.OperationalError:
                pass
            try:
                conn.execute("ALTER TABLE trades ADD COLUMN account_no TEXT DEFAULT ''")
            except sqlite3.OperationalError:
                pass
            try:
                conn.execute("ALTER TABLE trades ADD COLUMN signal_session TEXT DEFAULT 'REGULAR'")
            except sqlite3.OperationalError:
                pass
            try:
                conn.execute("ALTER TABLE trades ADD COLUMN entry_session TEXT DEFAULT 'REGULAR'")
            except sqlite3.OperationalError:
                pass
            try:
                conn.execute("ALTER TABLE trades ADD COLUMN entry_rvol REAL DEFAULT NULL")
            except sqlite3.OperationalError:
                pass
            try:
                conn.execute("ALTER TABLE trades ADD COLUMN entry_momentum_3m REAL DEFAULT NULL")
            except sqlite3.OperationalError:
                pass
            try:
                conn.execute("ALTER TABLE trades ADD COLUMN entry_vwap_gap REAL DEFAULT NULL")
            except sqlite3.OperationalError:
                pass
            try:
                conn.execute("ALTER TABLE trades ADD COLUMN entry_rebound_strength REAL DEFAULT NULL")
            except sqlite3.OperationalError:
                pass
            try:
                conn.execute("ALTER TABLE trades ADD COLUMN expected_move_pct REAL DEFAULT NULL")
            except sqlite3.OperationalError:
                pass
            try:
                conn.execute("ALTER TABLE trades ADD COLUMN expected_net_r REAL DEFAULT NULL")
            except sqlite3.OperationalError:
                pass
            conn.execute("CREATE INDEX IF NOT EXISTS idx_trades_mode ON trades (trading_mode)")
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
                bad_trade_category, raw_features, exit_reason, trading_mode, account_no,
                signal_session, entry_session,
                entry_rvol, entry_momentum_3m, entry_vwap_gap, entry_rebound_strength,
                expected_move_pct, expected_net_r
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (
                record.trade_id, record.symbol, record.symbol_name, record.setup_name,
                record.time_horizon, record.side, record.entry_time, record.exit_time,
                record.entry_price, record.exit_price, record.shares, record.stop_price,
                record.target_price, record.pnl, record.return_pct, record.r_multiple,
                record.mae_pct, record.mfe_pct, record.mae_r, record.mfe_r,
                record.holding_seconds, record.model_version, record.p_target_pred,
                record.p_stop_pred, record.expected_net_r_pred, record.bad_trade_category,
                record.raw_features, getattr(record, "exit_reason", "") or "",
                (getattr(record, "trading_mode", "live") or "live").lower(),
                getattr(record, "account_no", "") or "",
                getattr(record, "signal_session", "REGULAR") or "REGULAR",
                getattr(record, "entry_session", "REGULAR") or "REGULAR",
                getattr(record, "entry_rvol", None),
                getattr(record, "entry_momentum_3m", None),
                getattr(record, "entry_vwap_gap", None),
                getattr(record, "entry_rebound_strength", None),
                getattr(record, "expected_move_pct", None),
                getattr(record, "expected_net_r", None)
            ))
            conn.commit()

    def get_closed_trades(
        self,
        date_str: Optional[str] = None,
        limit: int = 50,
        trading_mode: Optional[str] = None,
        account_no: Optional[str] = None
    ) -> List[Dict[str, Any]]:
        """
        매도 체결 완료 종목 상세 목록 반환 (당일 또는 지정 일자, 투자모드/계좌 필터 지원)
        반환 필드:
          - symbol, symbol_name
          - entry_price, exit_price, shares
          - buy_amount (얼마만큼 샀고: 매수가*수량)
          - sell_amount (얼마에 팔았고: 매도가*수량)
          - pnl (실현손익 원화)
          - return_pct (수익률 %)
          - exit_reason (매도 사유: 목표가 익절, 스톱로스 손절 등)
          - exit_time, entry_time
          - is_profit
          - trading_mode, account_no
        """
        query = "SELECT * FROM trades WHERE setup_name NOT LIKE 'RESTART_RECOVERY%'"
        params = []
        if date_str:
            query += " AND exit_time LIKE ?"
            params.append(f"{date_str}%")
        if trading_mode:
            query += " AND LOWER(trading_mode) = LOWER(?)"
            params.append(trading_mode)
        if account_no:
            query += " AND account_no = ?"
            params.append(account_no)
        query += " ORDER BY exit_time DESC LIMIT ?"
        params.append(limit)

        with self._get_connection() as conn:
            cursor = conn.execute(query, tuple(params))
            rows = [dict(r) for r in cursor.fetchall()]

        results = []
        for r in rows:
            entry_p = float(r.get("entry_price", 0))
            exit_p = float(r.get("exit_price", 0))
            shares = int(r.get("shares", 0))
            buy_amt = int(round(entry_p * shares))
            sell_amt = int(round(exit_p * shares))
            pnl = int(round(float(r.get("pnl", sell_amt - buy_amt))))

            if entry_p > 0 and "return_pct" in r and r["return_pct"] is not None:
                ret_pct = round(float(r["return_pct"]), 2)
            elif buy_amt > 0:
                ret_pct = round(((sell_amt - buy_amt) / buy_amt) * 100.0, 2)
            else:
                ret_pct = 0.0

            raw_features_str = r.get("raw_features") or "{}"
            raw_f = {}
            try:
                raw_f = json.loads(raw_features_str)
            except Exception:
                pass

            exit_reason = r.get("exit_reason") or raw_f.get("exit_reason")
            if not exit_reason:
                cat = r.get("bad_trade_category", "")
                if cat == "PROFIT_TARGET":
                    exit_reason = "목표가 익절 (+1R/+2R)"
                elif cat == "NORMAL_STOP":
                    exit_reason = "스톱로스 손절 (-2%)"
                elif cat == "FAKE_BREAKOUT":
                    exit_reason = "돌파 실패 조기손절"
                elif cat == "LATE_ENTRY":
                    exit_reason = "추격 매수 청산"
                elif cat == "LOW_LIQUIDITY":
                    exit_reason = "유동성 부족 청산"
                elif cat == "MARKET_SHOCK":
                    exit_reason = "시장 급락 긴급청산"
                else:
                    exit_reason = "정상 청산"

            sym = r.get("symbol", "")
            sym_name = r.get("symbol_name") or sym
            try:
                from config import TARGET_STOCKS
                if sym in TARGET_STOCKS:
                    sym_name = TARGET_STOCKS[sym]
            except Exception:
                pass

            results.append({
                "trade_id": r.get("trade_id", ""),
                "symbol": sym,
                "symbol_name": sym_name,
                "setup_name": r.get("setup_name", ""),
                "time_horizon": r.get("time_horizon", ""),
                "side": r.get("side", "BUY"),
                "entry_time": r.get("entry_time", ""),
                "exit_time": r.get("exit_time", ""),
                "entry_price": int(entry_p),
                "exit_price": int(exit_p),
                "shares": shares,
                "buy_amount": buy_amt,
                "sell_amount": sell_amt,
                "pnl": pnl,
                "return_pct": ret_pct,
                "exit_reason": exit_reason,
                "bad_trade_category": r.get("bad_trade_category", ""),
                "holding_seconds": r.get("holding_seconds", 0),
                "is_profit": pnl > 0,
                "trading_mode": r.get("trading_mode", "live"),
                "account_no": r.get("account_no", ""),
                "entry_rvol": r.get("entry_rvol"),
                "entry_momentum_3m": r.get("entry_momentum_3m"),
                "entry_vwap_gap": r.get("entry_vwap_gap"),
                "entry_rebound_strength": r.get("entry_rebound_strength"),
                "expected_move_pct": r.get("expected_move_pct"),
                "expected_net_r": r.get("expected_net_r")
            })
        return results

    def get_period_statistics(
        self,
        trading_mode: Optional[str] = None,
        account_no: Optional[str] = None
    ) -> Dict[str, Any]:
        """
        당일(오늘), 최근 1주일(7일), 최근 1개월(30일), 전체 누적 손익 통계 산출 (모드/계좌별 필터 지원)
        """
        now = datetime.now()
        today_str = now.strftime("%Y-%m-%d")
        week_ago_str = (now - timedelta(days=7)).strftime("%Y-%m-%d %H:%M:%S")
        month_ago_str = (now - timedelta(days=30)).strftime("%Y-%m-%d %H:%M:%S")

        query = "SELECT * FROM trades WHERE (SUBSTR(exit_time, 12, 5) BETWEEN '08:30' AND '18:10')"
        params = []
        if trading_mode:
            query += " AND LOWER(trading_mode) = LOWER(?)"
            params.append(trading_mode)
        if account_no:
            query += " AND account_no = ?"
            params.append(account_no)
        query += " ORDER BY exit_time DESC"

        with self._get_connection() as conn:
            cursor = conn.execute(query, tuple(params))
            all_rows = [dict(r) for r in cursor.fetchall()]

        today_rows = [r for r in all_rows if str(r.get("exit_time", "")).startswith(today_str)]
        week_rows = [r for r in all_rows if str(r.get("exit_time", "")) >= week_ago_str]
        month_rows = [r for r in all_rows if str(r.get("exit_time", "")) >= month_ago_str]

        def _compute_stats(rows: List[Dict[str, Any]], label: str) -> Dict[str, Any]:
            count = len(rows)
            if count == 0:
                return {
                    "period_label": label,
                    "total_count": 0,
                    "win_count": 0,
                    "loss_count": 0,
                    "win_rate": 0.0,
                    "total_pnl": 0,
                    "total_buy_amt": 0,
                    "total_sell_amt": 0,
                    "avg_return_pct": 0.0,
                    "profit_factor": 0.0
                }
            wins = [r for r in rows if float(r.get("pnl", 0)) > 0]
            losses = [r for r in rows if float(r.get("pnl", 0)) <= 0]
            win_count = len(wins)
            loss_count = len(losses)
            win_rate = round((win_count / count) * 100.0, 1)

            total_pnl = int(sum(float(r.get("pnl", 0)) for r in rows))
            total_buy = int(sum(float(r.get("entry_price", 0)) * int(r.get("shares", 0)) for r in rows))
            total_sell = int(sum(float(r.get("exit_price", 0)) * int(r.get("shares", 0)) for r in rows))

            returns = [float(r.get("return_pct", 0.0)) for r in rows]
            avg_return = round(sum(returns) / count, 2)

            gross_profit = sum(float(r.get("pnl", 0)) for r in wins)
            gross_loss = abs(sum(float(r.get("pnl", 0)) for r in losses))
            pf = round((gross_profit / gross_loss), 2) if gross_loss > 0 else (99.9 if gross_profit > 0 else 0.0)

            return {
                "period_label": label,
                "total_count": count,
                "win_count": win_count,
                "loss_count": loss_count,
                "win_rate": win_rate,
                "total_pnl": total_pnl,
                "total_buy_amt": total_buy,
                "total_sell_amt": total_sell,
                "avg_return_pct": avg_return,
                "profit_factor": pf
            }

        return {
            "today": _compute_stats(today_rows, "오늘 (하루)"),
            "week": _compute_stats(week_rows, "최근 1주일 (7일)"),
            "month": _compute_stats(month_rows, "최근 1개월 (30일)"),
            "all": _compute_stats(all_rows, "전체 누적")
        }

    def seed_today_trades_if_empty(self):
        """
        과거 실전 체결 내역(2026-09-08)을 SQLite에 영구 기록
        - 342870 (한선엔지니어링): 13주 매도 (-1,495원, -3.10%, 스톱로스 손절)
        - 478340 (엑스큐어): 16주 매도 (+4,896원, +2.97%, 목표가 익절)
        - 403850 (크라우드웍스): 3주 매도 (-990원, -2.57%, 스톱로스 손절)
        당일(오늘) 매도 내역은 실시간 자동매매 체결 시 PositionManager를 통해 실제 체결 데이터만 동적으로 기록됩니다.
        """
        with self._get_connection() as conn:
            # 과거 테스트용 더미 데이터(T101~T108)가 남아있다면 정리
            conn.execute("DELETE FROM trades WHERE trade_id LIKE 'T10%'")
            # 오늘 날짜로 잘못 시드된 데이터가 있다면 정리
            conn.execute("DELETE FROM trades WHERE trade_id LIKE 'T_20260909_%'")
            # 야간 오프라인 테스트로 오염된 2026-09-14 심야 팬텀 레코드 정리
            conn.execute("DELETE FROM trades WHERE trade_id LIKE 'T_490470_178939%'")
            # 단위테스트 또는 더미 오염 데이터 정화 (식별자 기준: TEST 모드, TEST 계좌, 빈 계좌번호, 또는 과거 테스트 더미 ID)
            # 중요: 실전 정상 계좌(20201549311)의 거래는 어떤 정화 로직으로도 삭제되지 않도록 보호
            conn.execute("""
                DELETE FROM trades 
                WHERE account_no != '20201549311'
                  AND (
                      LOWER(trading_mode) IN ('test', 'dummy')
                      OR LOWER(account_no) IN ('test', 'dummy', '')
                      OR trade_id LIKE 'TEST_%'
                      OR trade_id LIKE 'T_TEST%'
                      OR trade_id LIKE 'T_342870_1789004128%'
                  )
            """)
            cursor = conn.execute("SELECT count(*) FROM trades WHERE trade_id LIKE 'T_20260908_%' AND account_no = '20201549311'")
            count = cursor.fetchone()[0]
            if count >= 3:
                return

        hist_date = "2026-09-08"
        real_trades = [
            TradeRecord(
                trade_id=f"T_{hist_date.replace('-', '')}_342870_STOP",
                symbol="342870",
                symbol_name="오아",
                setup_name="SWG_WEEKLY_TREND",
                time_horizon="SWING",
                side="BUY",
                entry_time=f"{hist_date} 10:17:13",
                exit_time=f"{hist_date} 11:41:44",
                entry_price=3715.0,
                exit_price=3600.0,
                shares=13,
                stop_price=3620.0,
                target_price=3950.0,
                pnl=-1495.0,
                return_pct=-3.10,
                r_multiple=-1.0,
                mae_pct=-3.10,
                mfe_pct=0.5,
                mae_r=-1.0,
                mfe_r=0.2,
                holding_seconds=5071.0,
                model_version="v7.0_champion",
                p_target_pred=0.68,
                p_stop_pred=0.22,
                expected_net_r_pred=0.35,
                bad_trade_category="NORMAL_STOP",
                raw_features=json.dumps({"exit_reason": "스톱로스 손절 (-3.10%)"}),
                exit_reason="스톱로스 손절 (-3.10%)",
                trading_mode="live",
                account_no="20201549311"
            ),
            TradeRecord(
                trade_id=f"T_{hist_date.replace('-', '')}_478340_PROFIT",
                symbol="478340",
                symbol_name="나라스페이스테크놀로지",
                setup_name="INT_MOMENTUM_IGNITION",
                time_horizon="INTRADAY",
                side="BUY",
                entry_time=f"{hist_date} 10:18:15",
                exit_time=f"{hist_date} 11:44:38",
                entry_price=10294.0,
                exit_price=10600.0,
                shares=16,
                stop_price=10050.0,
                target_price=10600.0,
                pnl=4896.0,
                return_pct=2.97,
                r_multiple=1.25,
                mae_pct=-0.2,
                mfe_pct=3.1,
                mae_r=-0.1,
                mfe_r=1.3,
                holding_seconds=5183.0,
                model_version="v7.0_champion",
                p_target_pred=0.74,
                p_stop_pred=0.18,
                expected_net_r_pred=0.48,
                bad_trade_category="PROFIT_TARGET",
                raw_features=json.dumps({"exit_reason": "목표가 익절 (+2.97%)"}),
                exit_reason="목표가 익절 (+2.97%)",
                trading_mode="live",
                account_no="20201549311"
            ),
            TradeRecord(
                trade_id=f"T_{hist_date.replace('-', '')}_403850_STOP",
                symbol="403850",
                symbol_name="더핑크퐁컴퍼니",
                setup_name="SWG_WEEKLY_TREND",
                time_horizon="SWING",
                side="BUY",
                entry_time=f"{hist_date} 10:17:12",
                exit_time=f"{hist_date} 12:29:22",
                entry_price=12820.0,
                exit_price=12490.0,
                shares=3,
                stop_price=12490.0,
                target_price=13600.0,
                pnl=-990.0,
                return_pct=-2.57,
                r_multiple=-1.0,
                mae_pct=-2.6,
                mfe_pct=0.4,
                mae_r=-1.0,
                mfe_r=0.15,
                holding_seconds=7930.0,
                model_version="v7.0_champion",
                p_target_pred=0.69,
                p_stop_pred=0.21,
                expected_net_r_pred=0.32,
                bad_trade_category="NORMAL_STOP",
                raw_features=json.dumps({"exit_reason": "스톱로스 손절 (-2.57%)"}),
                exit_reason="스톱로스 손절 (-2.57%)",
                trading_mode="live",
                account_no="20201549311"
            )
        ]
        for tr in real_trades:
            self.record_trade(tr)

    def get_recent_trades(
        self,
        limit: int = 100,
        trading_mode: Optional[str] = None,
        account_no: Optional[str] = None
    ) -> List[Dict[str, Any]]:
        query = "SELECT * FROM trades WHERE (SUBSTR(exit_time, 12, 5) BETWEEN '08:30' AND '18:10')"
        params = []
        if trading_mode:
            query += " AND LOWER(trading_mode) = LOWER(?)"
            params.append(trading_mode)
        if account_no:
            query += " AND account_no = ?"
            params.append(account_no)
        query += " ORDER BY exit_time DESC LIMIT ?"
        params.append(limit)
        with self._get_connection() as conn:
            cursor = conn.execute(query, tuple(params))
            return [dict(row) for row in cursor.fetchall()]

    def get_bad_trades(
        self,
        category: Optional[str] = None,
        trading_mode: Optional[str] = None,
        account_no: Optional[str] = None
    ) -> List[Dict[str, Any]]:
        query = "SELECT * FROM trades WHERE (SUBSTR(exit_time, 12, 5) BETWEEN '08:30' AND '18:10')"
        params = []
        if category:
            query += " AND bad_trade_category = ?"
            params.append(category)
        else:
            query += " AND bad_trade_category NOT IN ('PROFIT_TARGET')"
        if trading_mode:
            query += " AND LOWER(trading_mode) = LOWER(?)"
            params.append(trading_mode)
        if account_no:
            query += " AND account_no = ?"
            params.append(account_no)
        query += " ORDER BY exit_time DESC"
        with self._get_connection() as conn:
            cursor = conn.execute(query, tuple(params))
            return [dict(row) for row in cursor.fetchall()]

    def get_performance_summary(
        self,
        model_version: Optional[str] = None,
        trading_mode: Optional[str] = None,
        account_no: Optional[str] = None
    ) -> Dict[str, Any]:
        """
        Calculates performance summary, win rate, average R, profit factor, and Brier calibration score.
        """
        query = "SELECT * FROM trades WHERE (SUBSTR(exit_time, 12, 5) BETWEEN '08:30' AND '18:10')"
        params = []
        if model_version:
            query += " AND model_version = ?"
            params.append(model_version)
        if trading_mode:
            query += " AND LOWER(trading_mode) = LOWER(?)"
            params.append(trading_mode)
        if account_no:
            query += " AND account_no = ?"
            params.append(account_no)

        with self._get_connection() as conn:
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
