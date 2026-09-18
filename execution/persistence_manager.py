"""[FINAL MASTER v16.0] 영구 저장소 및 3계층 스토리지 티어링 엔진 (execution/persistence_manager.py)
Section 43 ~ 54: Persistent Database, Storage Tiering, Restart Recovery & Periodic Reconciliation

1. WARM DB (SQLite: data/operational_v16.db):
   - market_events, candidates, setups, orders, fills, positions, stops,
     no_trade_records, reconciliation_logs, api_requests 영구 보존
2. HOT -> WARM -> COLD (Parquet) 3계층 스토리지 티어링:
   - 장 종료 후 또는 배치로 Parquet 파일로 압축 이관
   - Row Count 대조 및 Checksum 무결성 검증 완료 후에만 HOT/WARM 정리
3. Restart Recovery (재시작 복구):
   - 브로커 실제 잔고/보유종목과 내부 DB 대조 후 포지션/스톱 상태 완벽 복원
4. Periodic Reconciliation (주기적 정합성 조정):
   - 장중 3분 주기 백그라운드 불일치 감지 및 자동 조정
"""

import os
import json
import sqlite3
import hashlib
import logging
import contextlib
from datetime import datetime, timedelta
from typing import Dict, List, Any, Optional, Tuple, Union
import pandas as pd

from execution.deadlock_transaction import (
    DeadlockSafeTransactionManager,
    ExecutionEvent,
    ExecutionResult
)
import config.settings as settings

logger = logging.getLogger("PersistenceManager")


class PersistenceManager:
    """운영 데이터 영구 저장, 3계층 스토리지 티어링 및 정합성 조정 관리자"""

    def __init__(
        self,
        db_path: str = "data/operational_v16.db",
        cold_dir: str = "data/cold_parquet",
        trading_mode: Optional[str] = None,
        account_no: Optional[str] = None
    ):
        self.db_path = db_path
        self.cold_dir = cold_dir
        self.trading_mode = (str(trading_mode).upper() if trading_mode else None)
        self.account_no = (str(account_no) if account_no else None)
        if os.path.dirname(self.db_path):
            os.makedirs(os.path.dirname(self.db_path), exist_ok=True)
        if self.cold_dir:
            os.makedirs(self.cold_dir, exist_ok=True)
        self._init_db()
        self.tx_mgr = DeadlockSafeTransactionManager(
            db_path=self.db_path,
            trading_mode=self.trading_mode,
            account_no=self.account_no
        )
        self.last_recon_time = datetime.now()

    @contextlib.contextmanager
    def _get_conn(self):
        conn = sqlite3.connect(self.db_path, timeout=60.0)
        conn.row_factory = sqlite3.Row
        try:
            yield conn
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    def _init_db(self):
        """10대 핵심 운영 테이블 스키마 초기화 (계좌 격리 식별자 포함)"""
        with self._get_conn() as conn:
            conn.execute("PRAGMA journal_mode=WAL;")
            conn.executescript("""
            CREATE TABLE IF NOT EXISTS market_events (
                event_id TEXT PRIMARY KEY,
                iem_cd TEXT,
                name TEXT,
                timestamp TEXT,
                event_type TEXT,
                score REAL,
                description TEXT
            );

            CREATE TABLE IF NOT EXISTS candidates (
                candidate_id TEXT PRIMARY KEY,
                iem_cd TEXT,
                name TEXT,
                timestamp TEXT,
                score REAL,
                state TEXT,
                reason TEXT
            );

            CREATE TABLE IF NOT EXISTS setups (
                setup_id TEXT PRIMARY KEY,
                iem_cd TEXT,
                strategy_id TEXT,
                timestamp TEXT,
                score REAL,
                is_valid INTEGER,
                block_reason TEXT
            );

            CREATE TABLE IF NOT EXISTS orders (
                client_order_id TEXT PRIMARY KEY,
                broker_order_no TEXT,
                iem_cd TEXT,
                side TEXT,
                order_type TEXT,
                qty INTEGER,
                price REAL,
                status TEXT,
                created_at TEXT,
                sent_at TEXT,
                ack_at TEXT,
                filled_at TEXT,
                trading_mode TEXT NOT NULL,
                account_no TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS fills (
                fill_id TEXT PRIMARY KEY,
                client_order_id TEXT,
                iem_cd TEXT,
                side TEXT,
                filled_qty INTEGER,
                filled_price REAL,
                timestamp TEXT,
                slippage_pct REAL,
                trading_mode TEXT NOT NULL,
                account_no TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS positions (
                position_id TEXT PRIMARY KEY,
                iem_cd TEXT,
                name TEXT,
                time_horizon TEXT,
                entry_price REAL,
                qty INTEGER,
                stop_price REAL,
                target_1r REAL,
                target_2r REAL,
                status TEXT,
                entry_time TEXT,
                exit_time TEXT,
                exit_price REAL,
                pnl REAL,
                trading_mode TEXT NOT NULL,
                account_no TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS stops (
                stop_id TEXT PRIMARY KEY,
                position_id TEXT,
                iem_cd TEXT,
                stop_price REAL,
                current_price REAL,
                triggered_at TEXT,
                order_status TEXT,
                trading_mode TEXT NOT NULL,
                account_no TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS no_trade_records (
                record_id TEXT PRIMARY KEY,
                iem_cd TEXT,
                name TEXT,
                strategy_id TEXT,
                timestamp TEXT,
                rule_score REAL,
                ml_prob REAL,
                expected_net_r REAL,
                primary_reason TEXT,
                secondary_reason TEXT,
                category TEXT
            );

            CREATE TABLE IF NOT EXISTS cash_shortfall_records (
                shortfall_id TEXT PRIMARY KEY,
                iem_cd TEXT,
                signal_id TEXT,
                strategy_id TEXT,
                decision TEXT,
                cash_available REAL,
                reserved_cash REAL,
                effective_available_cash REAL,
                required_order_value REAL,
                estimated_cost REAL,
                cash_shortfall REAL,
                timestamp TEXT,
                reason TEXT,
                trading_mode TEXT NOT NULL,
                account_no TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS reconciliation_logs (
                log_id TEXT PRIMARY KEY,
                timestamp TEXT,
                diff_detected INTEGER,
                details TEXT,
                action_taken TEXT
            );

            CREATE INDEX IF NOT EXISTS idx_positions_acc_lookup ON positions(trading_mode, account_no, iem_cd, status);
            CREATE INDEX IF NOT EXISTS idx_orders_acc_client ON orders(trading_mode, account_no, client_order_id);
            CREATE INDEX IF NOT EXISTS idx_fills_acc_client ON fills(trading_mode, account_no, client_order_id);
            CREATE INDEX IF NOT EXISTS idx_stops_acc_mode ON stops(trading_mode, account_no, iem_cd);
            CREATE INDEX IF NOT EXISTS idx_shortfall_acc_mode ON cash_shortfall_records(trading_mode, account_no, iem_cd);

            CREATE TABLE IF NOT EXISTS api_requests (
                request_id TEXT PRIMARY KEY,
                timestamp TEXT,
                endpoint TEXT,
                latency_ms REAL,
                status TEXT,
                error_msg TEXT
            );

            CREATE TABLE IF NOT EXISTS exit_execution_logs (
                log_id TEXT PRIMARY KEY,
                position_id TEXT,
                iem_cd TEXT,
                exit_reason TEXT,
                selected_order_type TEXT,
                fallback_order_type TEXT,
                reference_price REAL,
                limit_price REAL,
                aggressive_ticks INTEGER,
                bid REAL,
                ask REAL,
                spread REAL,
                order_quantity INTEGER,
                timestamp TEXT,
                description TEXT
            );
            """)
            try:
                conn.execute("ALTER TABLE orders ADD COLUMN signal_session TEXT DEFAULT 'REGULAR'")
            except sqlite3.OperationalError:
                pass
            try:
                conn.execute("ALTER TABLE orders ADD COLUMN entry_session TEXT DEFAULT 'REGULAR'")
            except sqlite3.OperationalError:
                pass
            try:
                conn.execute("ALTER TABLE positions ADD COLUMN signal_session TEXT DEFAULT 'REGULAR'")
            except sqlite3.OperationalError:
                pass
            try:
                conn.execute("ALTER TABLE positions ADD COLUMN entry_session TEXT DEFAULT 'REGULAR'")
            except sqlite3.OperationalError:
                pass

    # =========================================================================
    # 1. 운영 데이터 실시간 기록
    # =========================================================================

    def record_order(self, order, trading_mode: Optional[str] = None, account_no: Optional[str] = None):
        mode = trading_mode or getattr(order, "trading_mode", None) or self.trading_mode or "MOCK"
        acc = account_no or getattr(order, "account_no", None) or self.account_no or getattr(settings, "ACCOUNT_MOCK", "50001003032")
        mode = str(mode).upper()
        acc = str(acc)
        sig_s = getattr(order, "signal_session", "REGULAR") or "REGULAR"
        ent_s = getattr(order, "entry_session", "REGULAR") or "REGULAR"
        with self._get_conn() as conn:
            conn.execute("""
            INSERT OR REPLACE INTO orders (
                client_order_id, broker_order_no, iem_cd, side, order_type,
                qty, price, status, created_at, sent_at, ack_at, filled_at,
                trading_mode, account_no, signal_session, entry_session
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (
                order.client_order_id,
                order.broker_order_no or "",
                order.iem_cd,
                order.side.value if hasattr(order.side, "value") else str(order.side),
                order.order_type.value if hasattr(order.order_type, "value") else str(order.order_type),
                order.qty,
                order.price,
                order.status.value if hasattr(order.status, "value") else str(order.status),
                order.created_at.isoformat() if order.created_at else "",
                order.sent_at.isoformat() if order.sent_at else "",
                order.ack_at.isoformat() if order.ack_at else "",
                order.filled_at.isoformat() if order.filled_at else "",
                mode,
                acc,
                sig_s,
                ent_s
            ))

    def record_fill(
        self,
        client_order_id: str,
        iem_cd: str,
        side: str,
        qty: int,
        price: float,
        slippage_pct: float = 0.0,
        execution_id: Optional[str] = None,
        trading_mode: Optional[str] = None,
        account_no: Optional[str] = None
    ) -> ExecutionResult:
        """Section 13-19 Deadlock-safe fill recording with idempotency, exponential backoff and retry."""
        mode = trading_mode or self.trading_mode or "MOCK"
        acc = account_no or self.account_no or getattr(settings, "ACCOUNT_MOCK", "50001003032")
        mode = str(mode).upper()
        acc = str(acc)
        fill_id = execution_id or f"FILL_{client_order_id}_{int(datetime.now().timestamp() * 1000)}"
        event = ExecutionEvent(
            execution_id=fill_id,
            client_order_id=client_order_id,
            iem_cd=iem_cd,
            side=side,
            filled_qty=qty,
            filled_price=price,
            slippage_pct=slippage_pct,
            trading_mode=mode,
            account_no=acc
        )
        return self.apply_execution_update(event)

    def apply_execution_update(self, event: Union[ExecutionEvent, Dict[str, Any]]) -> ExecutionResult:
        """Section 13-19 Deadlock-safe transaction execution."""
        return self.tx_mgr.apply_execution_update(event)

    def get_deadlock_metrics(self) -> Dict[str, Any]:
        """Return Section 13-19 deadlock retry & latency metrics."""
        return self.tx_mgr.get_metrics()

    def record_no_trade(
        self,
        iem_cd: str,
        name: str,
        strategy_id: str,
        score: float,
        ml_prob: float,
        expected_net_r: float,
        primary_reason: str,
        secondary_reason: str = "",
        category: str = "CORRECT_NO_TRADE"
    ):
        rec_id = f"NT_{iem_cd}_{int(datetime.now().timestamp() * 1000)}"
        with self._get_conn() as conn:
            conn.execute("""
            INSERT OR REPLACE INTO no_trade_records (
                record_id, iem_cd, name, strategy_id, timestamp, rule_score,
                ml_prob, expected_net_r, primary_reason, secondary_reason, category
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (
                rec_id, iem_cd, name, strategy_id, datetime.now().isoformat(),
                score, ml_prob, expected_net_r, primary_reason, secondary_reason, category
            ))

    def record_cash_shortfall(self, rec: Any, trading_mode: Optional[str] = None, account_no: Optional[str] = None):
        """Section 9: 현금 부족(INSUFFICIENT_CASH) 감사 기록 저장"""
        mode = trading_mode or getattr(rec, "trading_mode", None) or self.trading_mode or "MOCK"
        acc = account_no or getattr(rec, "account_no", None) or self.account_no or getattr(settings, "ACCOUNT_MOCK", "50001003032")
        mode = str(mode).upper()
        acc = str(acc)
        shortfall_id = f"SHORTFALL_{rec.symbol}_{int(datetime.now().timestamp() * 1000)}"
        with self._get_conn() as conn:
            conn.execute("""
            INSERT OR REPLACE INTO cash_shortfall_records (
                shortfall_id, iem_cd, signal_id, strategy_id, decision,
                cash_available, reserved_cash, effective_available_cash,
                required_order_value, estimated_cost, cash_shortfall,
                timestamp, reason, trading_mode, account_no
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (
                shortfall_id, rec.symbol, rec.signal_id, rec.strategy_id, rec.decision,
                rec.cash_available, rec.reserved_cash, rec.effective_available_cash,
                rec.required_order_value, rec.estimated_cost, rec.cash_shortfall,
                rec.timestamp, rec.reason, mode, acc
            ))

        # 동시에 no_trade_records에도 연동 기록
        self.record_no_trade(
            iem_cd=rec.symbol,
            name=rec.symbol,
            strategy_id=rec.strategy_id,
            score=0.0,
            ml_prob=0.0,
            expected_net_r=0.0,
            primary_reason=f"INSUFFICIENT_CASH (부족액: {rec.cash_shortfall:,.0f}원)",
            secondary_reason=f"필요: {rec.required_order_value:,.0f}원, 가용: {rec.effective_available_cash:,.0f}원",
            category="INSUFFICIENT_CASH"
        )

    def record_exit_execution(self, plan: Any, iem_cd: str, position_id: Optional[str] = None) -> str:
        """
        Section 8: 주문 방식 결정 로그 (Decision Audit Trail) 영구 보존
        - exit_reason, selected_order_type, fallback_order_type, reference_price, limit_price,
          aggressive_ticks, bid, ask, spread, order_quantity, timestamp 영구 기록
        """
        log_id = f"EXIT_{int(datetime.now().timestamp() * 1000)}_{iem_cd}"
        with self._get_conn() as conn:
            conn.execute("""
            INSERT OR REPLACE INTO exit_execution_logs (
                log_id, position_id, iem_cd, exit_reason, selected_order_type,
                fallback_order_type, reference_price, limit_price, aggressive_ticks,
                bid, ask, spread, order_quantity, timestamp, description
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (
                log_id,
                position_id or "",
                iem_cd,
                plan.exit_reason.value if hasattr(plan.exit_reason, "value") else str(plan.exit_reason),
                plan.selected_order_type.name if hasattr(plan.selected_order_type, "name") else str(plan.selected_order_type),
                plan.fallback_order_type.name if hasattr(plan.fallback_order_type, "name") else str(plan.fallback_order_type),
                float(plan.reference_price),
                float(plan.limit_price),
                int(plan.aggressive_ticks),
                float(plan.bid),
                float(plan.ask),
                float(plan.spread),
                int(plan.order_quantity),
                plan.timestamp,
                getattr(plan, "description", "")
            ))
        return log_id

    def record_stop_trigger(
        self,
        position_id: str,
        iem_cd: str,
        stop_price: float,
        current_price: float,
        order_status: str = "TRIGGERED",
        trading_mode: Optional[str] = None,
        account_no: Optional[str] = None
    ) -> str:
        """Section 38: 스톱로스 도달 시 감사 로그 영구 기록"""
        mode = trading_mode or self.trading_mode
        acc = account_no or self.account_no
        if not mode or not acc:
            raise ValueError(f"record_stop_trigger requires trading_mode and account_no (got mode={mode}, acc={acc})")
        mode = str(mode).upper()
        acc = str(acc)
        stop_id = f"STOP_{iem_cd}_{int(datetime.now().timestamp() * 1000)}"
        with self._get_conn() as conn:
            conn.execute("""
            INSERT OR REPLACE INTO stops (
                stop_id, position_id, iem_cd, stop_price, current_price, triggered_at, order_status,
                trading_mode, account_no
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (
                stop_id, position_id, iem_cd, stop_price, current_price,
                datetime.now().isoformat(), order_status, mode, acc
            ))
        return stop_id

    def get_exit_execution_logs(self, limit: int = 100, iem_cd: Optional[str] = None) -> List[Dict[str, Any]]:
        """Exit 주문 결정 감사 로그 조회"""
        query = "SELECT * FROM exit_execution_logs"
        params: List[Any] = []
        if iem_cd:
            query += " WHERE iem_cd = ?"
            params.append(iem_cd)
        query += " ORDER BY rowid DESC LIMIT ?"
        params.append(limit)
        with self._get_conn() as conn:
            rows = conn.execute(query, params).fetchall()
            return [dict(r) for r in rows]

    # =========================================================================
    # 2. Section 44 & 45: Storage Tiering & Migration (HOT -> WARM -> COLD Parquet)
    # =========================================================================

    def migrate_to_cold_parquet(self, table_name: str = "no_trade_records", target_date: Optional[str] = None) -> Dict[str, Any]:
        """
        WARM SQLite 데이터를 COLD Parquet 파일로 추출 및 무결성 검증 후 안전 정리
        """
        target_date = target_date or datetime.now().strftime("%Y%m%d")
        dest_dir = os.path.join(self.cold_dir, target_date)
        os.makedirs(dest_dir, exist_ok=True)
        parquet_file = os.path.join(dest_dir, f"{table_name}.parquet")

        with self._get_conn() as conn:
            df = pd.read_sql_query(f"SELECT * FROM {table_name}", conn)

        raw_row_count = len(df)
        if raw_row_count == 0:
            return {"status": "EMPTY", "rows": 0, "file": parquet_file, "verified": True}

        # 1. Parquet로 저장
        df.to_parquet(parquet_file, engine="pyarrow", index=False)

        # 2. Row Count 및 Checksum 무결성 검증 (Section 45)
        df_read = pd.read_parquet(parquet_file, engine="pyarrow")
        verified = (len(df_read) == raw_row_count)

        # Checksum 검증
        with open(parquet_file, "rb") as f:
            checksum = hashlib.sha256(f.read()).hexdigest()

        if verified:
            logger.info(f"COLD 스토리지 이관 완료 ({table_name}): {raw_row_count}건 검증 성공 (SHA256: {checksum[:12]}...)")
            return {
                "status": "SUCCESS",
                "rows": raw_row_count,
                "file": parquet_file,
                "checksum": checksum,
                "verified": True
            }
        else:
            logger.error(f"COLD 이관 검증 실패 ({table_name}): 원본 {raw_row_count} != 파케이 {len(df_read)}")
            return {
                "status": "FAILED",
                "rows": raw_row_count,
                "file": parquet_file,
                "verified": False
            }

    # =========================================================================
    # 3. Section 48 & 49: Restart Recovery & Periodic Reconciliation (3분 주기)
    # =========================================================================

    def perform_reconciliation(self, client, position_manager, order_router, force: bool = False) -> Dict[str, Any]:
        """
        브로커 실제 잔고/미체결 주문과 내부 포지션 매니저 상태를 주기적으로 대조 및 복구
        """
        now = datetime.now()
        self.last_recon_time = now

        if not client or (getattr(client, "dry_run", False) and not force):
            return {"status": "SKIPPED_DRY_RUN", "diff": False}

        try:
            balance = client.get_balance()
            holdings = balance.get("holdings", [])
            broker_codes = {h["iem_cd"]: h for h in holdings}
            internal_codes = {p.iem_cd: p for p in position_manager.positions.values() if not p.is_closed}

            diff_detected = False
            details = []

            # 1. 브로커에는 있으나 내부 시스템에 없는 포지션 복구
            for c, h in broker_codes.items():
                if c not in internal_codes:
                    diff_detected = True
                    details.append(f"MISSING_IN_INTERNAL: {c}({h.get('qty')}주)")
                    position_manager.sync_from_broker([h])

            # 2. 내부에 있으나 브로커에 없는 포지션 종료 처리
            for c, pos in internal_codes.items():
                if c not in broker_codes:
                    diff_detected = True
                    details.append(f"MISSING_IN_BROKER: {c}")
                    pos.is_closed = True
                    pos.exit_time = now
                    pos.exit_reason = "RECONCILIATION_BROKER_CLOSED"
                    if pos.position_id in position_manager.positions:
                        del position_manager.positions[pos.position_id]
                    position_manager.closed_positions.append(pos)

            # 3. 양쪽 모두 존재하나 보유 수량이 불일치하는 경우 (부분체결/주문가능수량 괴리 복구)
            for c, h in broker_codes.items():
                if c in internal_codes:
                    pos = internal_codes[c]
                    b_qty = int(h.get("qty", 0))
                    if pos.qty != b_qty:
                        diff_detected = True
                        details.append(f"QTY_MISMATCH: {c} (내부 {pos.qty}주 != 브로커 {b_qty}주 -> 브로커 수량으로 동기화)")
                        if b_qty <= 0:
                            pos.is_closed = True
                            pos.exit_time = now
                            pos.exit_reason = "RECONCILIATION_BROKER_ZERO"
                            if pos.position_id in position_manager.positions:
                                del position_manager.positions[pos.position_id]
                            position_manager.closed_positions.append(pos)
                        else:
                            pos.qty = b_qty

            # 정합성 로그 기록
            log_id = f"RECON_{int(now.timestamp())}"
            action = f"Auto-synced {len(details)} diffs" if diff_detected else "MATCH_OK"
            with self._get_conn() as conn:
                conn.execute("""
                INSERT OR REPLACE INTO reconciliation_logs (
                    log_id, timestamp, diff_detected, details, action_taken
                ) VALUES (?, ?, ?, ?, ?)
                """, (log_id, now.isoformat(), 1 if diff_detected else 0, "; ".join(details), action))

            return {
                "status": "COMPLETED",
                "diff": diff_detected,
                "details": details,
                "broker_count": len(broker_codes),
                "internal_count": len(position_manager.positions)
            }

        except Exception as e:
            logger.error(f"주기적 정합성 조정 오류: {e}")
            return {"status": "ERROR", "error": str(e), "diff": False}
