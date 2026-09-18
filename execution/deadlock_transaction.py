# -*- coding: utf-8 -*-
"""[FINAL MASTER v16.0] DB Deadlock Defense Module (execution/deadlock_transaction.py)
Section 13-19: DB Deadlock Defense — Transaction Retry + Exponential Backoff + Jitter
Section 13-20: Deadlock Verification Tests (TEST 1 ~ TEST 7)
Section 13-21: Final Invariants (Idempotency & Consistent Lock Ordering)

High-concurrency protection layer for WebSocket Execution Handler,
REST Polling Worker, Recovery and Reconciliation workers against
DB Deadlock, Lock Timeout, and duplicate fills.
"""

import os
import time
import random
import sqlite3
import logging
from dataclasses import dataclass, field
from datetime import datetime
from typing import Dict, List, Any, Optional, Tuple, Callable, Union

try:
    import config.settings as settings
except ImportError:
    settings = None

logger = logging.getLogger("DeadlockTransaction")


class DeadlockError(Exception):
    """Raised when a database deadlock or lock conflict is detected."""
    pass


class LockTimeoutError(DeadlockError):
    """Raised when acquiring a database lock times out."""
    pass


class DuplicateExecutionError(Exception):
    """Raised when an execution_id has already been processed (Idempotency)."""
    pass


@dataclass
class ExecutionEvent:
    """Execution update event delivered by WebSocket or REST Poller."""
    execution_id: str
    client_order_id: str
    iem_cd: str
    side: str  # 'BUY' or 'SELL'
    filled_qty: int
    filled_price: float
    order_type: str = "LIMIT"
    position_id: Optional[str] = None
    timestamp: Optional[str] = None
    broker_order_no: Optional[str] = ""
    slippage_pct: float = 0.0
    extra: Dict[str, Any] = field(default_factory=dict)
    trading_mode: Optional[str] = None
    account_no: Optional[str] = None


@dataclass
class ExecutionResult:
    """Result of applying execution update."""
    status: str  # 'SUCCESS', 'DUPLICATE_EXECUTION_IGNORED', 'RECONCILIATION_REQUIRED', 'FAILED'
    execution_id: str
    client_order_id: str
    retries_attempted: int = 0
    message: str = ""
    error: Optional[str] = None
    applied_at: Optional[str] = None


@dataclass
class DeadlockMetrics:
    """Section 13-19 Metrics Collection."""
    db_deadlock_count: int = 0
    db_deadlock_retry_count: int = 0
    db_deadlock_retry_success: int = 0
    db_deadlock_retry_exhausted: int = 0
    db_lock_timeout_count: int = 0
    total_executions: int = 0
    latencies_ms: List[float] = field(default_factory=list)

    def record_latency(self, latency_ms: float):
        self.latencies_ms.append(latency_ms)
        if len(self.latencies_ms) > 1000:
            self.latencies_ms.pop(0)

    def get_metrics(self) -> Dict[str, Any]:
        avg_lat = sum(self.latencies_ms) / len(self.latencies_ms) if self.latencies_ms else 0.0
        max_lat = max(self.latencies_ms) if self.latencies_ms else 0.0
        min_lat = min(self.latencies_ms) if self.latencies_ms else 0.0
        return {
            "db_deadlock_count": self.db_deadlock_count,
            "db_deadlock_retry_count": self.db_deadlock_retry_count,
            "db_deadlock_retry_success": self.db_deadlock_retry_success,
            "db_deadlock_retry_exhausted": self.db_deadlock_retry_exhausted,
            "db_lock_timeout_count": self.db_lock_timeout_count,
            "total_executions": self.total_executions,
            "execution_latency_avg_ms": round(avg_lat, 2),
            "execution_latency_max_ms": round(max_lat, 2),
            "execution_latency_min_ms": round(min_lat, 2),
        }


class DeadlockSafeTransactionManager:
    """
    Section 13-19 Deadlock Defense Manager:
    - Retries OUTSIDE transaction
    - Exponential Backoff + Jitter
    - Strict Lock Ordering (1. Order -> 2. Position -> 3. Fill)
    - Idempotency per execution_id
    - Safe fallback (RECONCILIATION_REQUIRED) on retry exhaustion
    - No new broker order generated on retry
    """

    def __init__(
        self,
        db_path: str = "data/operational_v16.db",
        base_delay: Optional[float] = None,
        max_delay: Optional[float] = None,
        max_retries: Optional[int] = None,
        busy_timeout_sec: float = 5.0,
        trading_mode: Optional[str] = None,
        account_no: Optional[str] = None
    ):
        self.db_path = db_path
        self.base_delay = base_delay if base_delay is not None else getattr(settings, "DB_TX_RETRY_BASE_DELAY", 0.05)
        self.max_delay = max_delay if max_delay is not None else getattr(settings, "DB_TX_RETRY_MAX_DELAY", 1.0)
        self.max_retries = max_retries if max_retries is not None else getattr(settings, "DB_TX_MAX_RETRIES", 5)
        self.busy_timeout_sec = busy_timeout_sec
        self.trading_mode = (str(trading_mode).upper() if trading_mode else (getattr(settings, "TRADING_MODE", "MOCK") or "MOCK").upper())
        self.account_no = (str(account_no) if account_no else (getattr(settings, "ACCOUNT_NO", "DEFAULT_ACCOUNT") or "DEFAULT_ACCOUNT"))
        self.metrics = DeadlockMetrics()
        self._ensure_schema()

    def _ensure_schema(self):
        """Ensure orders, positions, fills, and reconciliation_logs exist with account isolation."""
        if os.path.dirname(self.db_path):
            os.makedirs(os.path.dirname(self.db_path), exist_ok=True)
        conn = sqlite3.connect(self.db_path, timeout=self.busy_timeout_sec)
        try:
            conn.execute("PRAGMA journal_mode=WAL;")
            conn.executescript("""
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
                trading_mode TEXT NOT NULL DEFAULT 'MOCK',
                account_no TEXT NOT NULL DEFAULT 'DEFAULT'
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
                trading_mode TEXT NOT NULL DEFAULT 'MOCK',
                account_no TEXT NOT NULL DEFAULT 'DEFAULT'
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
                trading_mode TEXT NOT NULL DEFAULT 'MOCK',
                account_no TEXT NOT NULL DEFAULT 'DEFAULT'
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
            """)
            conn.commit()
        finally:
            conn.close()

    def is_deadlock_error(self, e: Exception) -> bool:
        """Determine if error is a recoverable lock conflict / deadlock."""
        if isinstance(e, (DeadlockError, LockTimeoutError)):
            return True
        if isinstance(e, sqlite3.OperationalError):
            err_msg = str(e).lower()
            lock_keywords = [
                "database is locked",
                "database table is locked",
                "busy",
                "deadlock",
                "timeout",
                "lock",
                "cannot start a transaction within a transaction",
                "disk i/o error"
            ]
            return any(k in err_msg for k in lock_keywords)
        return False

    def is_duplicate_error(self, e: Exception) -> bool:
        """Determine if error is a duplicate constraint violation (Idempotency)."""
        if isinstance(e, DuplicateExecutionError):
            return True
        if isinstance(e, sqlite3.IntegrityError):
            msg = str(e).lower()
            if "unique constraint failed: fills.fill_id" in msg or "unique constraint failed: orders.client_order_id" in msg or "unique" in msg:
                return True
        return False

    def calculate_backoff(self, attempt: int) -> float:
        """
        Section 13-19 Exponential Backoff + Jitter:
        delay = min(BASE_DELAY * (2 ** attempt), MAX_DELAY)
        jitter = random.uniform(0, delay * 0.25)
        """
        delay = min(self.base_delay * (2 ** attempt), self.max_delay)
        jitter = random.uniform(0.0, delay * 0.25)
        return delay + jitter

    def apply_execution_update(self, event: Union[ExecutionEvent, Dict[str, Any]]) -> ExecutionResult:
        """
        Section 13-19: Apply execution update with outer transaction retry loop.
        Never retries inside the transaction block; rolls back completely on lock error.
        Guarantees:
        1. Consistent Lock Ordering: Orders -> Positions -> Fills
        2. Idempotency per execution_id
        3. No new broker orders issued during retry
        4. Safe fallback to RECONCILIATION_REQUIRED when max_retries exhausted
        """
        if isinstance(event, dict):
            event = ExecutionEvent(**event)

        start_time = time.perf_counter()
        self.metrics.total_executions += 1

        for attempt in range(self.max_retries):
            conn = None
            try:
                # Open isolated connection for transaction
                conn = sqlite3.connect(self.db_path, timeout=self.busy_timeout_sec)
                conn.row_factory = sqlite3.Row

                # BEGIN IMMEDIATE acquires write lock up front, preventing upgrade deadlocks
                conn.execute("BEGIN IMMEDIATE;")

                # Section 13-21 Consistent Lock Ordering & Idempotent Update
                status_code = self._apply_execution_update_once(conn, event)

                conn.commit()

                latency_ms = (time.perf_counter() - start_time) * 1000.0
                self.metrics.record_latency(latency_ms)

                if attempt > 0:
                    self.metrics.db_deadlock_retry_success += 1
                    logger.info(
                        f"[DB_RETRY_SUCCESS] Execution {event.execution_id} succeeded on attempt {attempt + 1} "
                        f"(Latency: {latency_ms:.1f}ms)"
                    )

                if status_code == "IDEMPOTENT_IGNORE":
                    return ExecutionResult(
                        status="DUPLICATE_EXECUTION_IGNORED",
                        execution_id=event.execution_id,
                        client_order_id=event.client_order_id,
                        retries_attempted=attempt,
                        message="[IDEMPOTENT_IGNORE] Execution already processed, duplicate ignored",
                        applied_at=datetime.now().isoformat()
                    )

                return ExecutionResult(
                    status="SUCCESS",
                    execution_id=event.execution_id,
                    client_order_id=event.client_order_id,
                    retries_attempted=attempt,
                    message="Execution applied successfully",
                    applied_at=datetime.now().isoformat()
                )

            except DuplicateExecutionError:
                if conn:
                    try:
                        conn.rollback()
                    except Exception:
                        pass
                logger.info(f"[IDEMPOTENT_IGNORE] Execution {event.execution_id} already applied")
                return ExecutionResult(
                    status="DUPLICATE_EXECUTION_IGNORED",
                    execution_id=event.execution_id,
                    client_order_id=event.client_order_id,
                    retries_attempted=attempt,
                    message="[IDEMPOTENT_IGNORE] Duplicate execution ignored",
                    applied_at=datetime.now().isoformat()
                )

            except sqlite3.IntegrityError as e:
                if conn:
                    try:
                        conn.rollback()
                    except Exception:
                        pass
                if self.is_duplicate_error(e):
                    logger.info(f"[IDEMPOTENT_IGNORE] Duplicate constraint on {event.execution_id}: {e}")
                    return ExecutionResult(
                        status="DUPLICATE_EXECUTION_IGNORED",
                        execution_id=event.execution_id,
                        client_order_id=event.client_order_id,
                        retries_attempted=attempt,
                        message=f"[IDEMPOTENT_IGNORE] Duplicate constraint ignored: {e}",
                        applied_at=datetime.now().isoformat()
                    )
                else:
                    # Non-recoverable integrity error (e.g. NOT NULL, CHECK) -> Fail immediately, NO retry
                    logger.error(f"[INTEGRITY_ERROR] Fatal constraint error for {event.execution_id}: {e}")
                    return ExecutionResult(
                        status="FAILED",
                        execution_id=event.execution_id,
                        client_order_id=event.client_order_id,
                        retries_attempted=attempt,
                        message=f"IntegrityError (No retry): {e}",
                        error=str(e)
                    )

            except Exception as e:
                if conn:
                    try:
                        conn.rollback()
                    except Exception:
                        pass

                if self.is_deadlock_error(e):
                    self.metrics.db_deadlock_count += 1
                    self.metrics.db_deadlock_retry_count += 1
                    if "timeout" in str(e).lower():
                        self.metrics.db_lock_timeout_count += 1

                    if attempt < self.max_retries - 1:
                        sleep_sec = self.calculate_backoff(attempt)
                        logger.warning(
                            f"[DB_DEADLOCK_RETRY] Attempt {attempt + 1}/{self.max_retries} for "
                            f"{event.execution_id} encountered lock conflict: {e}. Retrying in {sleep_sec * 1000:.1f}ms..."
                        )
                        time.sleep(sleep_sec)
                        continue
                    else:
                        logger.error(
                            f"[DB_RETRY_EXHAUSTED] Max retries ({self.max_retries}) reached for {event.execution_id}: {e}"
                        )
                        break
                else:
                    # Application error or invalid data -> Fail fast, NO retry
                    logger.error(f"[NON_RETRYABLE_ERROR] Execution {event.execution_id} failed with non-lock error: {e}")
                    return ExecutionResult(
                        status="FAILED",
                        execution_id=event.execution_id,
                        client_order_id=event.client_order_id,
                        retries_attempted=attempt,
                        message=f"Non-retryable error: {e}",
                        error=str(e)
                    )
            finally:
                if conn:
                    try:
                        conn.close()
                    except Exception:
                        pass

        # Section 13-19 & Section 13-20 TEST 4: Max retries exhausted
        self.metrics.db_deadlock_retry_exhausted += 1
        return self._handle_retry_exhausted(event)

    def _apply_execution_update_once(self, conn: sqlite3.Connection, event: ExecutionEvent) -> str:
        """
        Inner single-transaction execution update with Strict Lock Ordering:
        Order (1) -> Position (2) -> Fill / P&L (3)
        Strictly isolated by trading_mode and account_no.
        """
        now_iso = event.timestamp or datetime.now().isoformat()
        trading_mode = (getattr(event, "trading_mode", None) or self.trading_mode or "").upper()
        account_no = getattr(event, "account_no", None) or self.account_no or ""
        if not trading_mode or not account_no:
            raise ValueError(f"ExecutionEvent requires valid trading_mode and account_no (got mode={trading_mode}, acc={account_no})")

        # --- IDEMPOTENCY CHECK (Section 13-21) ---
        cur = conn.execute(
            "SELECT fill_id FROM fills WHERE fill_id = ? AND UPPER(trading_mode) = UPPER(?) AND account_no = ?",
            (event.execution_id, trading_mode, account_no)
        )
        if cur.fetchone() is not None:
            return "IDEMPOTENT_IGNORE"

        # --- STEP 1: ORDER LOCK & UPDATE ---
        cur = conn.execute(
            "SELECT * FROM orders WHERE client_order_id = ? AND UPPER(trading_mode) = UPPER(?) AND account_no = ?",
            (event.client_order_id, trading_mode, account_no)
        )
        order_row = cur.fetchone()

        if order_row:
            new_status = "FILLED"
            conn.execute("""
                UPDATE orders
                SET status = ?, filled_at = ?, broker_order_no = COALESCE(NULLIF(?, ''), broker_order_no)
                WHERE client_order_id = ? AND UPPER(trading_mode) = UPPER(?) AND account_no = ?
            """, (new_status, now_iso, event.broker_order_no or "", event.client_order_id, trading_mode, account_no))
        else:
            # Create order record if received from WS before REST
            conn.execute("""
                INSERT INTO orders (
                    client_order_id, broker_order_no, iem_cd, side, order_type,
                    qty, price, status, created_at, sent_at, ack_at, filled_at,
                    trading_mode, account_no
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (
                event.client_order_id,
                event.broker_order_no or "",
                event.iem_cd,
                event.side,
                event.order_type,
                event.filled_qty,
                event.filled_price,
                "FILLED",
                now_iso, now_iso, now_iso, now_iso,
                trading_mode,
                account_no
            ))

        # --- STEP 2: POSITION LOCK & UPDATE ---
        pos_row = None
        if event.position_id:
            cur = conn.execute(
                "SELECT * FROM positions WHERE position_id = ? AND UPPER(trading_mode) = UPPER(?) AND account_no = ?",
                (event.position_id, trading_mode, account_no)
            )
            pos_row = cur.fetchone()

        if not pos_row:
            # Match by iem_cd and OPEN status within current account and mode
            cur = conn.execute(
                """SELECT * FROM positions 
                   WHERE iem_cd = ? AND status = 'OPEN' 
                     AND UPPER(trading_mode) = UPPER(?) AND account_no = ? 
                   ORDER BY entry_time ASC LIMIT 1""",
                (event.iem_cd, trading_mode, account_no)
            )
            pos_row = cur.fetchone()

        if event.side.upper() == "BUY":
            if pos_row:
                old_qty = int(pos_row["qty"])
                old_price = float(pos_row["entry_price"])
                new_qty = old_qty + event.filled_qty
                new_avg_price = ((old_qty * old_price) + (event.filled_qty * event.filled_price)) / new_qty if new_qty > 0 else event.filled_price
                conn.execute("""
                    UPDATE positions
                    SET qty = ?, entry_price = ?, status = 'OPEN'
                    WHERE position_id = ? AND UPPER(trading_mode) = UPPER(?) AND account_no = ?
                """, (new_qty, new_avg_price, pos_row["position_id"], trading_mode, account_no))
            else:
                pos_id = event.position_id or f"POS_{event.iem_cd}_{int(time.time() * 1000)}"
                conn.execute("""
                    INSERT INTO positions (
                        position_id, iem_cd, name, time_horizon, entry_price,
                        qty, stop_price, target_1r, target_2r, status, entry_time, pnl,
                        trading_mode, account_no
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """, (
                    pos_id,
                    event.iem_cd,
                    event.iem_cd,
                    "DAY",
                    event.filled_price,
                    event.filled_qty,
                    event.filled_price * 0.98,
                    event.filled_price * 1.02,
                    event.filled_price * 1.04,
                    "OPEN",
                    now_iso,
                    0.0,
                    trading_mode,
                    account_no
                ))
        elif event.side.upper() == "SELL":
            if pos_row:
                current_qty = int(pos_row["qty"])
                entry_price = float(pos_row["entry_price"])
                accumulated_pnl = float(pos_row["pnl"] or 0.0)

                qty_to_reduce = min(current_qty, event.filled_qty)
                remaining_qty = max(0, current_qty - qty_to_reduce)
                realized_pnl = (event.filled_price - entry_price) * qty_to_reduce
                new_pnl = accumulated_pnl + realized_pnl

                if remaining_qty == 0:
                    conn.execute("""
                        UPDATE positions
                        SET qty = 0, status = 'CLOSED', exit_time = ?, exit_price = ?, pnl = ?
                        WHERE position_id = ? AND UPPER(trading_mode) = UPPER(?) AND account_no = ?
                    """, (now_iso, event.filled_price, new_pnl, pos_row["position_id"], trading_mode, account_no))
                else:
                    conn.execute("""
                        UPDATE positions
                        SET qty = ?, status = 'OPEN', pnl = ?
                        WHERE position_id = ? AND UPPER(trading_mode) = UPPER(?) AND account_no = ?
                    """, (remaining_qty, new_pnl, pos_row["position_id"], trading_mode, account_no))

        # --- STEP 3: FILL RECORD INSERTION (Idempotent execution record) ---
        conn.execute("""
            INSERT INTO fills (
                fill_id, client_order_id, iem_cd, side, filled_qty, filled_price, timestamp, slippage_pct,
                trading_mode, account_no
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (
            event.execution_id,
            event.client_order_id,
            event.iem_cd,
            event.side,
            event.filled_qty,
            event.filled_price,
            now_iso,
            event.slippage_pct,
            trading_mode,
            account_no
        ))

        return "SUCCESS"

    def _handle_retry_exhausted(self, event: ExecutionEvent) -> ExecutionResult:
        """
        Section 13-19 & 13-20 TEST 4:
        When max retries are exhausted due to continuous deadlock / lock timeout:
        1. Transition Order status to RECONCILIATION_REQUIRED (or UNKNOWN)
        2. Insert record into reconciliation_logs
        3. Emit [DB_RETRY_EXHAUSTED] log
        4. Return EXECUTION_UPDATE_FAILED with status RECONCILIATION_REQUIRED
        """
        trading_mode = (getattr(event, "trading_mode", None) or self.trading_mode or "LIVE").upper()
        account_no = getattr(event, "account_no", None) or self.account_no or ""
        logger.error(
            f"[DB_RETRY_EXHAUSTED] Execution update exhausted for execution_id={event.execution_id}, "
            f"client_order_id={event.client_order_id}, mode={trading_mode}, acc={account_no}. Transitioning to RECONCILIATION_REQUIRED."
        )

        try:
            conn = sqlite3.connect(self.db_path, timeout=10.0)
            try:
                now_iso = datetime.now().isoformat()
                # Update order status to RECONCILIATION_REQUIRED
                conn.execute("""
                    UPDATE orders
                    SET status = 'RECONCILIATION_REQUIRED'
                    WHERE client_order_id = ? AND UPPER(trading_mode) = UPPER(?) AND account_no = ?
                """, (event.client_order_id, trading_mode, account_no))

                # Log to reconciliation_logs
                log_id = f"RECON_{int(time.time() * 1000)}_{event.client_order_id}"
                conn.execute("""
                    INSERT OR REPLACE INTO reconciliation_logs (
                        log_id, timestamp, diff_detected, details, action_taken
                    ) VALUES (?, ?, ?, ?, ?)
                """, (
                    log_id,
                    now_iso,
                    1,
                    f"[DB_RETRY_EXHAUSTED] Execution {event.execution_id} on {event.iem_cd} failed after {self.max_retries} retries",
                    "ORDER_MARKED_RECONCILIATION_REQUIRED"
                ))
                conn.commit()
            finally:
                conn.close()
        except Exception as e:
            logger.critical(f"Failed to record reconciliation fallback for {event.client_order_id}: {e}")

        return ExecutionResult(
            status="RECONCILIATION_REQUIRED",
            execution_id=event.execution_id,
            client_order_id=event.client_order_id,
            retries_attempted=self.max_retries,
            message=f"[DB_RETRY_EXHAUSTED] Max retries ({self.max_retries}) exceeded; marked RECONCILIATION_REQUIRED",
            error="MAX_RETRIES_EXCEEDED"
        )

    def get_metrics(self) -> Dict[str, Any]:
        """Return metrics snapshot."""
        return self.metrics.get_metrics()


_global_tx_managers: Dict[str, DeadlockSafeTransactionManager] = {}

def get_deadlock_transaction_manager(db_path: str = "data/operational_v16.db") -> DeadlockSafeTransactionManager:
    """Singleton-like accessor per db_path."""
    if db_path not in _global_tx_managers:
        _global_tx_managers[db_path] = DeadlockSafeTransactionManager(db_path=db_path)
    return _global_tx_managers[db_path]

def apply_execution_update(event: Union[ExecutionEvent, Dict[str, Any]], db_path: str = "data/operational_v16.db") -> ExecutionResult:
    """Convenience functional interface."""
    mgr = get_deadlock_transaction_manager(db_path)
    return mgr.apply_execution_update(event)
