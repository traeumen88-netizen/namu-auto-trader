"""
Live Telemetry Exporter (execution/live_telemetry_exporter.py)
Automated real-time transaction telemetry exporter for public GitHub sync.
- Non-blocking Queue architecture: Trading engine is NEVER blocked
- Strict test/live isolation: pytest / test events are NEVER written to live YYYYMMDD/ directory
- Strict deduplication via event_id
- Accurate real-time pending_orders, open_positions, and order status state machine tracking
- Sanitizes sensitive keys, tokens, and account numbers via TelemetrySanitizer
- Partitioned storage: data/live_telemetry/YYYYMMDD/{live_decision,live_orders,live_fills,live_positions,live_rejections,live_errors}.jsonl
- Atomic latest_live_status.json updater
- Daily live_summary.json generator
"""

import os
import sys
import json
import time
import queue
import uuid
import tempfile
import logging
import threading
from datetime import datetime, timezone, timedelta
from typing import Dict, Any, Optional, List, Tuple
from collections import OrderedDict

from core.telemetry_sanitizer import TelemetrySanitizer
from core.models import OrderStatus

logger = logging.getLogger("LiveTelemetryExporter")

KST = timezone(timedelta(hours=9))

CRITICAL_EVENTS = {
    "BUY_FILLED",
    "SELL_FILLED",
    "BROKER_REJECT",
    "CIRCUIT_BREAKER",
    "ERROR",
    "RECONCILIATION_MISMATCH",
    "POSITION_MISMATCH",
    "ORDER_STATE_MISMATCH",
}


class LiveTelemetryExporter:
    """Thread-safe, non-blocking telemetry exporter singleton."""

    _instance: Optional["LiveTelemetryExporter"] = None
    _lock = threading.Lock()

    def __init__(
        self,
        base_dir: Optional[str] = None,
        environment: Optional[str] = None,
        source: Optional[str] = None,
        max_queue_size: int = 10000,
    ):
        # Auto-detect test environment (pytest, unit tests)
        is_test_env = (
            "pytest" in sys.modules
            or "PYTEST_CURRENT_TEST" in os.environ
            or os.environ.get("NAMU_TEST_ENV") == "1"
        )
        self.environment = environment or ("TEST" if is_test_env else "LIVE")
        self.default_source = source or ("UNIT_TEST" if self.environment == "TEST" else "LIVE_TRADER")

        if base_dir:
            self.base_dir = os.path.abspath(base_dir)
        elif is_test_env:
            self.base_dir = os.path.abspath(os.path.join(tempfile.gettempdir(), "namu_test_telemetry"))
        else:
            self.base_dir = os.path.abspath("data/live_telemetry")

        self.max_queue_size = max_queue_size
        self.queue: queue.Queue = queue.Queue(maxsize=max_queue_size)
        self._seen_event_ids: OrderedDict[str, float] = OrderedDict()
        self._seen_lock = threading.Lock()
        self._process_lock = threading.Lock()
        self.max_seen_history = 50000

        self.trading_mode: str = "LIVE"
        self.market_status: str = "OPEN"
        self.process_status: str = "RUNNING"

        # Active state registries for accurate status reporting
        self._active_pending_orders: Dict[str, Dict[str, Any]] = {}
        self._active_open_positions: Dict[str, Dict[str, Any]] = {}

        # Telemetry metrics counters
        self._metrics_lock = threading.Lock()
        self.metrics = {
            "buy_signal_count": 0,
            "buy_approved": 0,
            "buy_orders_sent": 0,
            "buy_fills": 0,
            "sell_signals": 0,
            "sell_orders_sent": 0,
            "sell_fills": 0,
            "open_positions": 0,
            "pending_orders": 0,
            "pending_order_count": 0,
            "pending_buy_orders": 0,
            "pending_sell_orders": 0,
            "entry_quality_reject": 0,
            "profit_opportunity_reject": 0,
            "reentry_reject": 0,
            "cash_reject": 0,
            "micro_trade_count": 0,
            "last_event": None,
            "git_sync_status": "OK",
            "last_push_at": None,
            "last_push_result": "NONE",
            "pending_telemetry_events": 0,
        }

        # Daily summary aggregators
        self._daily_stats = {
            "entry_quality": {"pass": 0, "reject": 0, "reasons": {}},
            "profit_opportunity": {"pass": 0, "reject": 0, "reasons": {}},
            "reentry": {
                "same_symbol_reentry": 0,
                "stop_to_reentry_lt_15m": 0,
                "stop_to_reentry_15_30m": 0,
                "stop_to_reentry_gt_30m": 0,
            },
            "micro_trade": {
                "count": 0,
                "total_trades": 0,
                "pnl_list": [],
                "holding_times": [],
            },
            "counterfactual": {
                "blocked_trade_count": 0,
                "mfe_5m_list": [],
                "mfe_15m_list": [],
                "mfe_30m_list": [],
            },
            "completed_trades": {
                "count": 0,
                "mfe_list": [],
                "mae_list": [],
                "holding_times": [],
                "gross_moves": [],
                "net_moves": [],
                "costs": [],
            },
        }

        # Git syncer reference
        self.git_syncer = None

        self._running = True
        self._worker_thread = threading.Thread(target=self._writer_loop, name="TelemetryExporterWorker", daemon=True)
        self._worker_thread.start()

    @classmethod
    def get_instance(
        cls,
        base_dir: Optional[str] = None,
        environment: Optional[str] = None,
        source: Optional[str] = None,
    ) -> "LiveTelemetryExporter":
        with cls._lock:
            if cls._instance is None:
                cls._instance = cls(base_dir=base_dir, environment=environment, source=source)
            else:
                if base_dir and cls._instance.base_dir != os.path.abspath(base_dir):
                    cls._instance.base_dir = os.path.abspath(base_dir)
                if environment:
                    cls._instance.environment = environment
                if source:
                    cls._instance.default_source = source
            return cls._instance

    @classmethod
    def reset_instance(cls):
        with cls._lock:
            if cls._instance:
                cls._instance.stop()
                cls._instance = None

    def stop(self):
        """Stops the worker thread and flushes remaining queue items."""
        self._running = False
        self.flush()

    def set_git_syncer(self, syncer):
        """Attaches a TelemetryGitSyncer instance for fast notifications."""
        self.git_syncer = syncer

    def update_git_status(self, status: str, last_push_at: Optional[str] = None, result: Optional[str] = None):
        """Updates git sync fields in latest_live_status."""
        with self._metrics_lock:
            self.metrics["git_sync_status"] = status
            if last_push_at is not None:
                self.metrics["last_push_at"] = last_push_at
            if result is not None:
                self.metrics["last_push_result"] = result
        self._write_latest_status()

    def sync_order_router_state(self, order_router: Any):
        """Directly synchronizes active pending orders from OrderRouter."""
        if not order_router or not hasattr(order_router, "pending_orders"):
            return
        active_statuses = (
            OrderStatus.ORDER_CREATED,
            OrderStatus.ORDER_SENT,
            OrderStatus.ORDER_ACK,
            OrderStatus.PENDING,
            OrderStatus.PARTIAL_FILL,
        )
        with self._metrics_lock:
            self._active_pending_orders.clear()
            for cid, o in list(order_router.pending_orders.items()):
                st = getattr(o, "status", None)
                rem_qty = getattr(o, "remaining_qty", getattr(o, "qty", 0))
                st_val = getattr(st, "value", str(st))
                is_active = (st in active_statuses) or any(
                    s in st_val.upper() for s in ("CREATED", "SENT", "ACK", "PENDING", "PARTIAL")
                )
                if is_active and rem_qty > 0:
                    side_val = getattr(getattr(o, "side", None), "value", str(getattr(o, "side", ""))).upper()
                    self._active_pending_orders[cid] = {
                        "client_order_id": cid,
                        "broker_order_no": getattr(o, "broker_order_no", ""),
                        "symbol": getattr(o, "iem_cd", ""),
                        "side": side_val,
                        "status": st_val,
                        "remaining_qty": rem_qty,
                    }
            self.metrics["pending_orders"] = len(self._active_pending_orders)
            self.metrics["pending_order_count"] = len(self._active_pending_orders)
            self.metrics["pending_buy_orders"] = sum(
                1 for o in self._active_pending_orders.values() if "BUY" in str(o.get("side", "")).upper()
            )
            self.metrics["pending_sell_orders"] = sum(
                1 for o in self._active_pending_orders.values() if "SELL" in str(o.get("side", "")).upper()
            )
        self._write_latest_status()

    def sync_position_manager_state(self, position_manager: Any):
        """Directly synchronizes active open positions from PositionManager."""
        if not position_manager or not hasattr(position_manager, "positions"):
            return
        with self._metrics_lock:
            self._active_open_positions.clear()
            for pid, pos in list(position_manager.positions.items()):
                if not getattr(pos, "is_closed", False) and getattr(pos, "qty", 0) > 0:
                    self._active_open_positions[pid] = {
                        "symbol": getattr(pos, "iem_cd", ""),
                        "qty": getattr(pos, "qty", 0),
                        "entry_price": getattr(pos, "entry_price", 0.0),
                    }
            self.metrics["open_positions"] = len(self._active_open_positions)
        self._write_latest_status()

    def generate_event_id(self, event_type: str, symbol: str = "") -> str:
        """Generates a globally unique, sortable event ID."""
        now_str = datetime.now(KST).strftime("%Y%m%d_%H%M%S_%f")[:21]
        sym = symbol or "GEN"
        short_id = uuid.uuid4().hex[:6]
        return f"evt_{now_str}_{sym}_{event_type.lower()}_{short_id}"

    def is_duplicate(self, event_id: str) -> bool:
        """Checks and registers event_id to prevent duplicates."""
        if not event_id:
            return False
        with self._seen_lock:
            if event_id in self._seen_event_ids:
                return True
            self._seen_event_ids[event_id] = time.time()
            if len(self._seen_event_ids) > self.max_seen_history:
                self._seen_event_ids.popitem(last=False)
            return False

    def emit_event(
        self,
        event_type: str,
        payload: Dict[str, Any],
        is_critical: Optional[bool] = None,
        date_str: Optional[str] = None,
        environment: Optional[str] = None,
        source: Optional[str] = None,
    ) -> Optional[str]:
        """
        Non-blocking enqueue of a telemetry event.
        Guaranteed NEVER to block trading execution.
        """
        try:
            event_id = payload.get("event_id") or self.generate_event_id(event_type, payload.get("symbol", ""))
            payload["event_id"] = event_id
            payload["event"] = event_type
            if "timestamp" not in payload:
                payload["timestamp"] = datetime.now(KST).isoformat()

            # Assign environment and source
            if "environment" not in payload:
                payload["environment"] = environment or self.environment
            if "source" not in payload:
                payload["source"] = source or self.default_source

            if is_critical is None:
                is_critical = event_type.upper() in CRITICAL_EVENTS

            item = {
                "event_type": event_type,
                "event_id": event_id,
                "payload": payload,
                "is_critical": is_critical,
                "date_str": date_str or datetime.now(KST).strftime("%Y%m%d"),
            }

            self.queue.put_nowait(item)
            return event_id
        except queue.Full:
            logger.warning(f"Telemetry queue is full (max={self.max_queue_size}). Dropping event: {event_type}")
            return None
        except Exception as e:
            logger.error(f"Error emitting telemetry event {event_type}: {e}")
            return None

    def _writer_loop(self):
        """Background thread consuming queue items, sanitizing, and persisting."""
        while self._running or not self.queue.empty():
            try:
                try:
                    item = self.queue.get(timeout=0.5)
                except queue.Empty:
                    continue

                with self._process_lock:
                    self._process_item(item)
                self.queue.task_done()
            except Exception as loop_err:
                logger.error(f"Unexpected error in Telemetry worker loop: {loop_err}", exc_info=True)

    def _process_item(self, item: Dict[str, Any]):
        event_id = item["event_id"]
        event_type = item["event_type"].upper()
        raw_payload = item["payload"]
        date_str = item["date_str"]
        is_critical = item.get("is_critical", False)

        # 1. Deduplication check
        if self.is_duplicate(event_id):
            logger.debug(f"Skipping duplicate event_id: {event_id}")
            return

        # 2. Strict Sanitization
        sanitized = TelemetrySanitizer.sanitize_data(raw_payload)

        # 3. Environment routing:
        # TEST events must NEVER enter live YYYYMMDD date directories!
        event_env = str(sanitized.get("environment", self.environment)).upper()
        is_live_telemetry_dir = (
            self.base_dir.endswith("data/live_telemetry")
            or self.base_dir.endswith("data\\live_telemetry")
            or os.path.basename(self.base_dir) == "live_telemetry"
        )
        if event_env == "TEST" and is_live_telemetry_dir:
            date_dir = os.path.join(self.base_dir, "fixtures", date_str)
        else:
            date_dir = os.path.join(self.base_dir, date_str)

        os.makedirs(date_dir, exist_ok=True)
        target_filename = self._determine_target_file(event_type)
        file_path = os.path.join(date_dir, target_filename)

        # 4. Append to JSONL
        try:
            line = json.dumps(sanitized, ensure_ascii=False)
            with open(file_path, "a", encoding="utf-8") as f:
                f.write(line + "\n")
        except Exception as write_err:
            logger.error(f"Failed to write telemetry to {file_path}: {write_err}")

        # 5. Update in-memory metrics, active orders, and daily stats
        self._update_metrics_and_stats(event_type, sanitized)

        # 6. Write latest_live_status.json (Atomic)
        self._write_latest_status()

        # 7. Fast git sync trigger for critical events
        if is_critical and self.git_syncer is not None:
            try:
                self.git_syncer.trigger_critical_sync(event_type)
            except Exception as sync_err:
                logger.warning(f"Failed to trigger critical git sync: {sync_err}")

    def _determine_target_file(self, event_type: str) -> str:
        """Maps event_type to standard JSONL file."""
        if "DECISION" in event_type or event_type in ("BUY_APPROVED", "BUY_SIGNAL"):
            return "live_decision.jsonl"
        elif any(k in event_type for k in ("ORDER_CREATED", "ORDER_SENT", "ORDER_ACK", "PENDING", "CANCELLED", "CANCELED")):
            return "live_orders.jsonl"
        elif "FILL" in event_type:
            return "live_fills.jsonl"
        elif "POSITION" in event_type:
            return "live_positions.jsonl"
        elif any(k in event_type for k in ("REJECT", "NO_TRADE", "EXPIRED")):
            return "live_rejections.jsonl"
        elif any(k in event_type for k in ("ERROR", "RECONCILIATION", "CIRCUIT_BREAKER", "MISMATCH", "RESTART", "RECOVERY")):
            return "live_errors.jsonl"
        return "live_orders.jsonl"

    def _update_metrics_and_stats(self, event_type: str, data: Dict[str, Any]):
        with self._metrics_lock:
            self.metrics["pending_telemetry_events"] = self.queue.qsize()

            # Active pending orders tracking
            cid = data.get("client_order_id")
            if cid:
                order_state = str(data.get("broker_order_state", "")).upper()
                side = str(data.get("side", "")).upper()
                rem_qty = data.get("remaining_qty", data.get("requested_qty", 0))

                is_terminal = (
                    order_state in ("FILLED", "CANCELLED", "CANCELED", "REJECTED", "EXPIRED")
                    or ("FILL" in event_type and data.get("is_full_fill", False))
                )

                if not is_terminal and rem_qty > 0:
                    self._active_pending_orders[cid] = {
                        "client_order_id": cid,
                        "broker_order_no": data.get("broker_order_no", ""),
                        "symbol": data.get("symbol", ""),
                        "side": side,
                        "status": order_state or event_type,
                        "remaining_qty": rem_qty,
                    }
                elif is_terminal:
                    self._active_pending_orders.pop(cid, None)

            # Active positions tracking
            pid = data.get("position_id") or data.get("symbol")
            if event_type == "POSITION_OPEN" and pid:
                self._active_open_positions[pid] = data
            elif event_type == "POSITION_CLOSED" and pid:
                self._active_open_positions.pop(pid, None)

            # Cumulative event counters
            if event_type == "BUY_SIGNAL":
                self.metrics["buy_signal_count"] += 1
            elif event_type == "BUY_APPROVED":
                self.metrics["buy_approved"] += 1
                self._daily_stats["entry_quality"]["pass"] += 1
                self._daily_stats["profit_opportunity"]["pass"] += 1
            elif event_type in ("BUY_ORDER_SENT", "ORDER_SENT") and data.get("side", "").upper() == "BUY":
                self.metrics["buy_orders_sent"] += 1
            elif event_type in ("BUY_FILLED", "BUY_PARTIAL_FILL") or (event_type == "ORDER_FILLED" and data.get("side", "").upper() == "BUY"):
                if event_type == "BUY_FILLED":
                    self.metrics["buy_fills"] += 1

            elif event_type == "SELL_SIGNAL":
                self.metrics["sell_signals"] += 1
            elif event_type in ("SELL_ORDER_SENT", "ORDER_SENT") and data.get("side", "").upper() == "SELL":
                self.metrics["sell_orders_sent"] += 1
            elif event_type in ("SELL_FILLED", "SELL_PARTIAL_FILL") or (event_type == "ORDER_FILLED" and data.get("side", "").upper() == "SELL"):
                if event_type == "SELL_FILLED":
                    self.metrics["sell_fills"] += 1

            elif event_type == "POSITION_CLOSED":
                ct = self._daily_stats["completed_trades"]
                ct["count"] += 1
                if "mfe" in data:
                    ct["mfe_list"].append(float(data["mfe"]))
                if "mae" in data:
                    ct["mae_list"].append(float(data["mae"]))
                if "holding_time_sec" in data:
                    ht = float(data["holding_time_sec"]) / 60.0
                    ct["holding_times"].append(ht)
                    if ht < 3.0:
                        self.metrics["micro_trade_count"] += 1
                        self._daily_stats["micro_trade"]["count"] += 1
                        self._daily_stats["micro_trade"]["holding_times"].append(ht)
                        if "pnl" in data:
                            self._daily_stats["micro_trade"]["pnl_list"].append(float(data["pnl"]))
                if "gross_move" in data:
                    ct["gross_moves"].append(float(data["gross_move"]))
                if "net_move" in data:
                    ct["net_moves"].append(float(data["net_move"]))
                if "cost" in data:
                    ct["costs"].append(float(data["cost"]))

            elif "REJECT" in event_type or event_type == "NO_TRADE":
                reason = data.get("reason") or data.get("reject_reason") or "UNKNOWN"
                gate = data.get("gate") or data.get("rejected_gate") or event_type

                if "ENTRY_QUALITY" in event_type or "BREAKOUT" in str(gate).upper():
                    self.metrics["entry_quality_reject"] += 1
                    self._daily_stats["entry_quality"]["reject"] += 1
                    r_dict = self._daily_stats["entry_quality"]["reasons"]
                    r_dict[reason] = r_dict.get(reason, 0) + 1
                elif "PROFIT_OPPORTUNITY" in event_type or "PROFIT" in str(gate).upper():
                    self.metrics["profit_opportunity_reject"] += 1
                    self._daily_stats["profit_opportunity"]["reject"] += 1
                    r_dict = self._daily_stats["profit_opportunity"]["reasons"]
                    r_dict[reason] = r_dict.get(reason, 0) + 1
                elif "REENTRY" in event_type:
                    self.metrics["reentry_reject"] += 1
                elif "CASH" in event_type:
                    self.metrics["cash_reject"] += 1

                self._daily_stats["counterfactual"]["blocked_trade_count"] += 1

            # Update pending & position counts accurately
            self.metrics["pending_orders"] = len(self._active_pending_orders)
            self.metrics["pending_order_count"] = len(self._active_pending_orders)
            self.metrics["pending_buy_orders"] = sum(
                1 for o in self._active_pending_orders.values() if "BUY" in str(o.get("side", "")).upper()
            )
            self.metrics["pending_sell_orders"] = sum(
                1 for o in self._active_pending_orders.values() if "SELL" in str(o.get("side", "")).upper()
            )
            self.metrics["open_positions"] = len(self._active_open_positions)

            # Set last event
            self.metrics["last_event"] = {
                "event": event_type,
                "symbol": data.get("symbol", ""),
                "environment": data.get("environment", self.environment),
                "timestamp": data.get("timestamp", datetime.now(KST).isoformat()),
            }

    def _write_latest_status(self):
        """Atomically writes latest_live_status.json."""
        os.makedirs(self.base_dir, exist_ok=True)
        status_path = os.path.join(self.base_dir, "latest_live_status.json")
        unique_id = uuid.uuid4().hex[:6]
        tmp_path = os.path.join(self.base_dir, f"latest_live_status.json.tmp.{unique_id}")

        with self._metrics_lock:
            payload = {
                "updated_at": datetime.now(KST).isoformat(),
                "trading_mode": self.trading_mode,
                "environment": self.environment,
                "market_status": self.market_status,
                "process_status": self.process_status,
                "buy_signal_count": self.metrics["buy_signal_count"],
                "buy_approved": self.metrics["buy_approved"],
                "buy_orders_sent": self.metrics["buy_orders_sent"],
                "buy_fills": self.metrics["buy_fills"],
                "sell_signals": self.metrics["sell_signals"],
                "sell_orders_sent": self.metrics["sell_orders_sent"],
                "sell_fills": self.metrics["sell_fills"],
                "open_positions": self.metrics["open_positions"],
                "pending_orders": self.metrics["pending_orders"],
                "pending_order_count": self.metrics["pending_order_count"],
                "pending_buy_orders": self.metrics["pending_buy_orders"],
                "pending_sell_orders": self.metrics["pending_sell_orders"],
                "entry_quality_reject": self.metrics["entry_quality_reject"],
                "profit_opportunity_reject": self.metrics["profit_opportunity_reject"],
                "reentry_reject": self.metrics["reentry_reject"],
                "cash_reject": self.metrics["cash_reject"],
                "micro_trade_count": self.metrics["micro_trade_count"],
                "last_event": self.metrics["last_event"],
                "git_sync_status": self.metrics["git_sync_status"],
                "last_push_at": self.metrics["last_push_at"],
                "last_push_result": self.metrics["last_push_result"],
                "pending_telemetry_events": self.queue.qsize(),
            }

        clean_payload = TelemetrySanitizer.sanitize_data(payload)
        try:
            with open(tmp_path, "w", encoding="utf-8") as f:
                json.dump(clean_payload, f, indent=2, ensure_ascii=False)
            for attempt in range(5):
                try:
                    os.replace(tmp_path, status_path)
                    break
                except (PermissionError, OSError):
                    if attempt < 4:
                        time.sleep(0.02)
                    else:
                        raise
        except Exception as err:
            logger.error(f"Failed to write latest_live_status.json: {err}")
        finally:
            if os.path.exists(tmp_path):
                try:
                    os.remove(tmp_path)
                except Exception:
                    pass

    def generate_daily_summary(self, date_str: Optional[str] = None) -> Dict[str, Any]:
        """Generates and writes live_summary.json for the specified date."""
        d_str = date_str or datetime.now(KST).strftime("%Y%m%d")
        # Route TEST environment summaries to fixtures directory if in live_telemetry dir
        is_live_telemetry_dir = (
            self.base_dir.endswith("data/live_telemetry")
            or self.base_dir.endswith("data\\live_telemetry")
            or os.path.basename(self.base_dir) == "live_telemetry"
        )
        if self.environment == "TEST" and is_live_telemetry_dir:
            date_dir = os.path.join(self.base_dir, "fixtures", d_str)
        else:
            date_dir = os.path.join(self.base_dir, d_str)

        os.makedirs(date_dir, exist_ok=True)
        summary_path = os.path.join(date_dir, "live_summary.json")
        tmp_path = os.path.join(date_dir, "live_summary.json.tmp")

        def _avg(lst: List[float]) -> float:
            return sum(lst) / len(lst) if lst else 0.0

        with self._metrics_lock:
            ds = self._daily_stats
            ct = ds["completed_trades"]
            mt = ds["micro_trade"]
            cf = ds["counterfactual"]

            total_trades = ct["count"]
            micro_count = mt["count"]
            micro_ratio = (micro_count / total_trades) if total_trades > 0 else 0.0

            summary_data = {
                "date": d_str,
                "generated_at": datetime.now(KST).isoformat(),
                "trading_mode": self.trading_mode,
                "environment": self.environment,
                "entry_quality": {
                    "buy_approved": self.metrics["buy_approved"],
                    "orders_sent": self.metrics["buy_orders_sent"],
                    "filled": self.metrics["buy_fills"],
                    "reject_count": self.metrics["entry_quality_reject"],
                    "reject_reasons": ds["entry_quality"]["reasons"],
                },
                "profit_opportunity": {
                    "pass": ds["profit_opportunity"]["pass"],
                    "reject": ds["profit_opportunity"]["reject"],
                    "reject_reasons": ds["profit_opportunity"]["reasons"],
                },
                "reentry": {
                    "same_symbol_reentry": ds["reentry"]["same_symbol_reentry"],
                    "stop_to_reentry_lt_15m": ds["reentry"]["stop_to_reentry_lt_15m"],
                    "stop_to_reentry_15_30m": ds["reentry"]["stop_to_reentry_15_30m"],
                    "stop_to_reentry_gt_30m": ds["reentry"]["stop_to_reentry_gt_30m"],
                },
                "micro_trade": {
                    "count": micro_count,
                    "ratio": round(micro_ratio, 4),
                    "average_pnl": round(_avg(mt["pnl_list"]), 2),
                    "average_holding_time_minutes": round(_avg(mt["holding_times"]), 2),
                },
                "counterfactual": {
                    "blocked_trade_count": cf["blocked_trade_count"],
                    "mfe_5m_avg": round(_avg(cf["mfe_5m_list"]), 4),
                    "mfe_15m_avg": round(_avg(cf["mfe_15m_list"]), 4),
                    "mfe_30m_avg": round(_avg(cf["mfe_30m_list"]), 4),
                },
                "completed_trades": {
                    "count": total_trades,
                    "average_mfe": round(_avg(ct["mfe_list"]), 4),
                    "average_mae": round(_avg(ct["mae_list"]), 4),
                    "average_holding_time_minutes": round(_avg(ct["holding_times"]), 2),
                    "average_gross_move": round(_avg(ct["gross_moves"]), 4),
                    "average_net_move": round(_avg(ct["net_moves"]), 4),
                    "average_cost": round(_avg(ct["costs"]), 4),
                },
            }

        sanitized_summary = TelemetrySanitizer.sanitize_data(summary_data)
        try:
            with open(tmp_path, "w", encoding="utf-8") as f:
                json.dump(sanitized_summary, f, indent=2, ensure_ascii=False)
            os.replace(tmp_path, summary_path)
        except Exception as err:
            logger.error(f"Failed to write live_summary.json: {err}")

        return sanitized_summary

    def flush(self):
        """Processes all pending events synchronously."""
        try:
            self.queue.join()
        except Exception:
            pass
        while not self.queue.empty():
            try:
                item = self.queue.get_nowait()
                with self._process_lock:
                    self._process_item(item)
                self.queue.task_done()
            except queue.Empty:
                break
            except Exception as e:
                logger.error(f"Error during flush: {e}")
        with self._process_lock:
            self._write_latest_status()

    # -------------------------------------------------------------------------
    # Convenience Emitter Methods
    # -------------------------------------------------------------------------

    def record_decision(self, record_or_dict: Any, decision_type: str = "BUY_APPROVED", environment: Optional[str] = None) -> Optional[str]:
        """Records a DecisionTrace record."""
        if hasattr(record_or_dict, "to_dict"):
            d = record_or_dict.to_dict()
        elif isinstance(record_or_dict, dict):
            d = dict(record_or_dict)
        else:
            d = {"data": str(record_or_dict)}

        event_type = decision_type
        if d.get("decision", "").upper() in ("REJECTED", "NO_TRADE") or "REJECT" in decision_type:
            event_type = "NO_TRADE"

        env = environment or ("LIVE" if str(d.get("trading_mode", "")).lower() == "live" else self.environment)
        d["environment"] = env
        d["source"] = "UNIT_TEST" if env == "TEST" else ("LIVE_TRADER" if env == "LIVE" else "MOCK_TRADER")

        return self.emit_event(event_type, d, is_critical=False, environment=env)

    def record_order_event(self, stage: str, order: Any, extra_info: Optional[Dict[str, Any]] = None, environment: Optional[str] = None) -> Optional[str]:
        """
        Records order state machine transitions:
        ORDER_CREATED, ORDER_SENT, ORDER_ACK, PENDING, CANCELLED, REJECTED
        """
        side_val = getattr(getattr(order, "side", None), "value", str(getattr(order, "side", ""))).upper()
        prefix = "BUY_" if "BUY" in side_val else "SELL_"
        event_type = f"{prefix}{stage.upper()}" if not stage.upper().startswith(("BUY_", "SELL_")) else stage.upper()

        env = environment or self.environment
        if env == "TEST":
            src = "UNIT_TEST"
        elif "ACK" in event_type or "REJECT" in event_type:
            src = "BROKER"
        else:
            src = self.default_source

        payload = {
            "environment": env,
            "source": src,
            "symbol": getattr(order, "iem_cd", ""),
            "name": getattr(order, "name", ""),
            "client_order_id": getattr(order, "client_order_id", ""),
            "broker_order_no": getattr(order, "broker_order_no", ""),
            "side": side_val,
            "order_type": getattr(getattr(order, "order_type", None), "value", str(getattr(order, "order_type", ""))),
            "broker_order_state": getattr(getattr(order, "status", None), "value", str(getattr(order, "status", ""))),
            "requested_qty": getattr(order, "qty", 0),
            "order_price": getattr(order, "price", 0),
            "filled_qty": getattr(order, "filled_qty", 0),
            "remaining_qty": getattr(order, "remaining_qty", getattr(order, "qty", 0)),
            "filled_avg_price": getattr(order, "filled_avg_price", 0.0),
        }
        if extra_info:
            payload.update(extra_info)

        is_critical = "REJECT" in event_type
        return self.emit_event(event_type, payload, is_critical=is_critical, environment=env)

    def record_fill(self, order: Any, fill_qty: int, fill_price: float, is_full_fill: bool, environment: Optional[str] = None) -> Optional[str]:
        """
        Records actual broker execution:
        BUY_PARTIAL_FILL / BUY_FILLED / SELL_PARTIAL_FILL / SELL_FILLED
        """
        side_val = getattr(getattr(order, "side", None), "value", str(getattr(order, "side", ""))).upper()
        prefix = "BUY_" if "BUY" in side_val else "SELL_"
        fill_type = "FILLED" if is_full_fill else "PARTIAL_FILL"
        event_type = f"{prefix}{fill_type}"

        env = environment or self.environment
        src = "UNIT_TEST" if env == "TEST" else "BROKER"
        payload = {
            "environment": env,
            "source": src,
            "symbol": getattr(order, "iem_cd", ""),
            "name": getattr(order, "name", ""),
            "client_order_id": getattr(order, "client_order_id", ""),
            "broker_order_no": getattr(order, "broker_order_no", ""),
            "side": side_val,
            "filled_qty": getattr(order, "filled_qty", fill_qty),
            "newly_filled_qty": fill_qty,
            "fill_price": fill_price,
            "remaining_qty": getattr(order, "remaining_qty", 0),
            "is_full_fill": is_full_fill,
        }
        return self.emit_event(event_type, payload, is_critical=True, environment=env)

    def record_position_event(self, event_name: str, pos: Any, extra_info: Optional[Dict[str, Any]] = None, environment: Optional[str] = None) -> Optional[str]:
        """Records POSITION_OPEN, PARTIAL_EXITED, POSITION_CLOSED."""
        env = environment or ("LIVE" if str(getattr(pos, "trading_mode", "")).upper() == "LIVE" else self.environment)
        src = "UNIT_TEST" if env == "TEST" else ("LIVE_TRADER" if env == "LIVE" else "MOCK_TRADER")
        payload = {
            "environment": env,
            "source": src,
            "position_id": getattr(pos, "position_id", ""),
            "symbol": getattr(pos, "iem_cd", ""),
            "name": getattr(pos, "name", ""),
            "strategy": getattr(pos, "strategy_id", ""),
            "qty": getattr(pos, "qty", 0),
            "entry_price": getattr(pos, "entry_price", 0.0),
            "current_price": getattr(pos, "current_price", 0.0),
            "realized_pnl": getattr(pos, "realized_pnl", 0.0),
            "status": getattr(pos, "status", ""),
        }
        if extra_info:
            payload.update(extra_info)
        return self.emit_event(event_name, payload, is_critical=False, environment=env)

    def record_rejection(self, gate_name: str, symbol: str, reason: str, details: Optional[Dict[str, Any]] = None, environment: Optional[str] = None) -> Optional[str]:
        """Records gate and pre-order rejection events."""
        event_type = f"{gate_name.upper()}_REJECT" if not gate_name.upper().endswith("_REJECT") else gate_name.upper()
        env = environment or self.environment
        src = "UNIT_TEST" if env == "TEST" else self.default_source
        payload = {
            "environment": env,
            "source": src,
            "symbol": symbol,
            "gate": gate_name,
            "reason": reason,
        }
        if details:
            payload.update(details)
        return self.emit_event(event_type, payload, is_critical=False, environment=env)

    def record_error(self, error_type: str, message: str, details: Optional[Dict[str, Any]] = None, environment: Optional[str] = None) -> Optional[str]:
        """Records error and reconciliation mismatch events."""
        event_type = error_type.upper()
        env = environment or self.environment
        src = "UNIT_TEST" if env == "TEST" else "SYSTEM"
        payload = {
            "environment": env,
            "source": src,
            "error_type": error_type,
            "message": message,
        }
        if details:
            payload.update(details)
        is_critical = event_type in CRITICAL_EVENTS or "ERROR" in event_type or "MISMATCH" in event_type
        return self.emit_event(event_type, payload, is_critical=is_critical, environment=env)
