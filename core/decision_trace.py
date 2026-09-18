"""[FINAL MASTER v16.4] Unified Decision Trace Engine (core/decision_trace.py)
Captures and unifies BUY_DECISION_TRACE and NO_TRADE_TRACE across the trading pipeline.

Key Capabilities:
1. Unified DecisionTraceRecord:
   - Header: timestamp, trading_mode, account_no, symbol, strategy, entry_price, current_price, bid, ask, spread_pct
   - Entry Quality: RVOL, 1m candle direction, 3m momentum, VWAP gap, distance from high, rebound_strength, gap_pct, turnover, signal_age_ms
   - Profit Opportunity: expected_move_pct, atr_normalized_move, expected_gross_profit_pct, expected_cost_pct, expected_net_move_pct, expected_net_r, cost_to_opportunity_ratio, predicted_mfe, predicted_mae
   - Risk / Execution: stop_distance_pct, position_size, usable_cash, reserved_cash, broker_psbl_qty, signal_ttl_result, price_drift_pct, execution_result
   - Final Decision: decision (BUY / NO_TRADE), final_reason, rejected_gate, rejected_reason, initial_rejected_gate, initial_rejected_reason
2. Formatted Loggers:
   - format_buy_trace(): Formatted text matching [BUY_DECISION_TRACE] specification
   - format_no_trade_trace(): Formatted text matching [NO_TRADE_TRACE] specification
3. DecisionTraceRegistry (Singleton):
   - In-memory registry with query filters and JSONL file persistence
   - Direct integration with ProfitOpportunityTelemetry and DiagnosticEngine
"""

import json
import os
import logging
from dataclasses import dataclass, field, asdict
from datetime import datetime
from typing import Optional, Dict, Any, List

logger = logging.getLogger("DecisionTrace")


@dataclass
class DecisionTraceRecord:
    # --- Identifiers & Pricing ---
    timestamp: str  # ISO 8601 string
    trading_mode: str  # "live" | "mock"
    account_no: str
    symbol: str
    strategy: str
    entry_price: float
    current_price: float
    bid: float = 0.0
    ask: float = 0.0
    spread_pct: float = 0.001
    spread_bps: float = 0.0
    target_1r: float = 0.0
    stop_price: float = 0.0

    # --- Entry Quality ---
    rvol: float = 1.0
    candle_1m_direction: str = "FLAT"  # "UP" | "DOWN" | "FLAT"
    momentum_3m: float = 0.0
    vwap_gap: float = 0.0
    dist_high: float = 0.0
    rebound_strength: float = 0.50
    gap_pct: float = 0.0
    turnover: float = 0.0
    signal_age_ms: float = 0.0

    # --- Profit Opportunity ---
    expected_move_pct: float = 0.02
    gross_move_pct: float = 0.02
    atr_normalized_move: float = 1.0
    expected_gross_profit_pct: float = 0.02
    expected_cost_pct: float = 0.0023
    expected_net_move_pct: float = 0.0177
    net_move_pct: float = 0.0177
    expected_net_r: float = 1.0
    cost_to_opportunity_ratio: float = 0.15
    predicted_mfe: float = 0.02
    predicted_mae: float = 0.015

    # --- Risk / Execution ---
    stop_distance_pct: float = 0.015
    position_size: int = 0
    shares: int = 0
    order_type: str = "LIMIT"
    usable_cash: float = 0.0
    reserved_cash: float = 0.0
    broker_psbl_qty: int = 0
    signal_ttl_result: str = "PASS"  # "PASS" | "EXPIRED"
    price_drift_pct: float = 0.0
    execution_result: str = "PENDING"  # "SUCCESS" | "FAILED" | "NOT_SENT" | "REJECTED"

    # --- Final Decision ---
    decision: str = "BUY"  # "BUY" | "BUY_APPROVED" | "NO_TRADE" | "REJECTED"
    final_reason: str = "ALL_GATES_PASSED"
    rejected_gate: Optional[str] = None
    reject_gate: Optional[str] = None
    rejected_reason: Optional[str] = None
    reject_reason: Optional[str] = None
    initial_rejected_gate: Optional[str] = None
    initial_rejected_reason: Optional[str] = None

    def __post_init__(self):
        if self.spread_bps == 0.0 and self.spread_pct > 0:
            self.spread_bps = self.spread_pct * 10000.0
        elif self.spread_pct == 0.001 and self.spread_bps > 0:
            self.spread_pct = self.spread_bps / 10000.0
        if self.net_move_pct != 0.0177 and self.expected_net_move_pct == 0.0177:
            self.expected_net_move_pct = self.net_move_pct
        elif self.net_move_pct == 0.0177 and self.expected_net_move_pct != 0.0177:
            self.net_move_pct = self.expected_net_move_pct
        if self.gross_move_pct != 0.02 and self.expected_gross_profit_pct == 0.02:
            self.expected_gross_profit_pct = self.gross_move_pct
        elif self.gross_move_pct == 0.02 and self.expected_gross_profit_pct != 0.02:
            self.gross_move_pct = self.expected_gross_profit_pct
        if self.reject_gate and not self.rejected_gate:
            self.rejected_gate = self.reject_gate
        elif self.rejected_gate and not self.reject_gate:
            self.reject_gate = self.rejected_gate
        if self.reject_reason and not self.rejected_reason:
            self.rejected_reason = self.reject_reason
        elif self.rejected_reason and not self.reject_reason:
            self.reject_reason = self.rejected_reason

    def format_buy_trace(self) -> str:
        """Section 1 Specification format for BUY_DECISION_TRACE"""
        return (
            "[BUY_DECISION_TRACE]\n\n"
            f"symbol={self.symbol}\n"
            f"strategy={self.strategy}\n"
            f"entry_price={int(self.entry_price):,}\n"
            f"current_price={int(self.current_price):,}\n"
            f"bid={int(self.bid):,}\n"
            f"ask={int(self.ask):,}\n"
            f"spread={self.spread_bps:.1f}bps\n"
            f"target_1r={int(self.target_1r):,}\n"
            f"stop_price={int(self.stop_price):,}\n"
            f"gross_move={self.gross_move_pct*100:+.2f}%\n"
            f"cost={self.expected_cost_pct*100:+.2f}%\n"
            f"net_move={self.net_move_pct*100:+.2f}%\n"
            f"cost/opp={self.cost_to_opportunity_ratio:.2f}\n"
            f"rebound_strength={self.rebound_strength:.2f}\n"
            f"RVOL={self.rvol:.2f}\n"
            f"3m_mom={self.momentum_3m*100:+.2f}%\n"
            f"VWAP_gap={self.vwap_gap*100:+.2f}%\n"
            f"dist_high={self.dist_high*100:+.2f}%\n"
            f"expected_net_r={self.expected_net_r:.2f}\n"
            f"signal_age={int(self.signal_age_ms)}ms\n"
            "DECISION=BUY_APPROVED\n"
            f"reason={self.final_reason}"
        )

    def format_no_trade_trace(self) -> str:
        """Section 2 Specification format for NO_TRADE_TRACE"""
        rej_gate = self.reject_gate or self.rejected_gate or "GATE"
        rej_reason = self.reject_reason or self.rejected_reason or self.final_reason
        return (
            "[NO_TRADE_TRACE]\n\n"
            f"symbol={self.symbol}\n"
            f"strategy={self.strategy}\n"
            f"current_price={int(self.current_price):,}\n"
            f"bid={int(self.bid):,}\n"
            f"ask={int(self.ask):,}\n"
            f"spread={self.spread_bps:.1f}bps\n"
            f"RVOL={self.rvol:.2f}\n"
            f"3m_mom={self.momentum_3m*100:+.2f}%\n"
            f"VWAP_gap={self.vwap_gap*100:+.2f}%\n"
            f"expected_move={self.expected_move_pct*100:+.2f}%\n"
            f"expected_net_r={self.expected_net_r:.2f}\n"
            f"rebound_strength={self.rebound_strength:.2f}\n"
            "DECISION=REJECTED\n"
            f"GATE={rej_gate}\n"
            f"reason={rej_reason}"
        )

    def format_trace(self) -> str:
        if self.decision.upper() in ("BUY", "BUY_APPROVED"):
            return self.format_buy_trace()
        return self.format_no_trade_trace()

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)

    def to_json(self) -> str:
        return json.dumps(self.to_dict(), ensure_ascii=False)


def format_buy_trace(record: DecisionTraceRecord) -> str:
    """Format a BUY_DECISION_TRACE record."""
    return record.format_buy_trace()


def format_no_trade_trace(record: DecisionTraceRecord) -> str:
    """Format a NO_TRADE_TRACE record."""
    return record.format_no_trade_trace()


def format_trace(record: DecisionTraceRecord) -> str:
    """Format either a BUY or NO_TRADE trace record based on decision."""
    return record.format_trace()


class DecisionTraceRegistry:
    """Central singleton registry storing and publishing decision traces"""
    _instance: Optional["DecisionTraceRegistry"] = None

    def __init__(self, jsonl_path: str = "data/decision_traces.jsonl"):
        self.jsonl_path = jsonl_path
        self.traces: List[DecisionTraceRecord] = []
        os.makedirs(os.path.dirname(self.jsonl_path), exist_ok=True)

    @classmethod
    def get_instance(cls, jsonl_path: str = "data/decision_traces.jsonl") -> "DecisionTraceRegistry":
        if cls._instance is None:
            cls._instance = cls(jsonl_path=jsonl_path)
        return cls._instance

    def reset(self):
        """Clears in-memory records (useful for test isolation)"""
        self.traces.clear()

    def record_trace(
        self,
        record: DecisionTraceRecord,
        log_message: bool = True,
        append_file: bool = True
    ) -> DecisionTraceRecord:
        """Register decision trace, emit formatted log, and append to JSONL"""
        self.traces.append(record)

        if log_message:
            formatted = record.format_trace()
            if record.decision == "BUY":
                logger.info(f"\n{formatted}")
            else:
                logger.info(f"\n{formatted}")

        if append_file:
            try:
                with open(self.jsonl_path, "a", encoding="utf-8") as f:
                    f.write(record.to_json() + "\n")
            except Exception as err:
                logger.warning(f"Failed to append decision trace to file: {err}")

        return record

    @property
    def buy_traces(self) -> List[DecisionTraceRecord]:
        return [t for t in self.traces if t.decision.upper() in ("BUY", "BUY_APPROVED")]

    @property
    def no_trade_traces(self) -> List[DecisionTraceRecord]:
        return [t for t in self.traces if t.decision.upper() not in ("BUY", "BUY_APPROVED")]

    def record_buy(
        self,
        symbol_or_record: Any = None,
        strategy: str = "",
        entry_price: float = 0.0,
        current_price: float = 0.0,
        trading_mode: str = "live",
        account_no: str = "",
        bid: float = 0.0,
        ask: float = 0.0,
        spread_pct: float = 0.001,
        rvol: float = 1.0,
        candle_1m_direction: str = "UP",
        momentum_3m: float = 0.0,
        vwap_gap: float = 0.0,
        dist_high: float = 0.0,
        rebound_strength: float = 0.50,
        gap_pct: float = 0.0,
        turnover: float = 0.0,
        signal_age_ms: float = 0.0,
        expected_move_pct: float = 0.02,
        atr_normalized_move: float = 1.0,
        expected_gross_profit_pct: float = 0.02,
        expected_cost_pct: float = 0.0023,
        expected_net_move_pct: float = 0.0177,
        expected_net_r: float = 1.0,
        cost_to_opportunity_ratio: float = 0.15,
        predicted_mfe: float = 0.02,
        predicted_mae: float = 0.015,
        stop_distance_pct: float = 0.015,
        position_size: int = 0,
        usable_cash: float = 0.0,
        reserved_cash: float = 0.0,
        broker_psbl_qty: int = 0,
        signal_ttl_result: str = "PASS",
        price_drift_pct: float = 0.0,
        execution_result: str = "SUCCESS",
        final_reason: str = "ALL_GATES_PASSED",
        now: Optional[datetime] = None,
        log_message: bool = True
    ) -> DecisionTraceRecord:
        if isinstance(symbol_or_record, DecisionTraceRecord):
            return self.record_trace(symbol_or_record, log_message=log_message)
        now_str = (now or datetime.now()).isoformat()
        rec = DecisionTraceRecord(
            timestamp=now_str,
            trading_mode=str(trading_mode).lower(),
            account_no=str(account_no),
            symbol=str(symbol_or_record),
            strategy=strategy,
            entry_price=entry_price,
            current_price=current_price,
            bid=bid,
            ask=ask,
            spread_pct=spread_pct,
            rvol=rvol,
            candle_1m_direction=candle_1m_direction,
            momentum_3m=momentum_3m,
            vwap_gap=vwap_gap,
            dist_high=dist_high,
            rebound_strength=rebound_strength,
            gap_pct=gap_pct,
            turnover=turnover,
            signal_age_ms=signal_age_ms,
            expected_move_pct=expected_move_pct,
            atr_normalized_move=atr_normalized_move,
            expected_gross_profit_pct=expected_gross_profit_pct,
            expected_cost_pct=expected_cost_pct,
            expected_net_move_pct=expected_net_move_pct,
            expected_net_r=expected_net_r,
            cost_to_opportunity_ratio=cost_to_opportunity_ratio,
            predicted_mfe=predicted_mfe,
            predicted_mae=predicted_mae,
            stop_distance_pct=stop_distance_pct,
            position_size=position_size,
            usable_cash=usable_cash,
            reserved_cash=reserved_cash,
            broker_psbl_qty=broker_psbl_qty,
            signal_ttl_result=signal_ttl_result,
            price_drift_pct=price_drift_pct,
            execution_result=execution_result,
            decision="BUY",
            final_reason=final_reason
        )
        return self.record_trace(rec, log_message=log_message)

    def record_no_trade(
        self,
        symbol_or_record: Any = None,
        strategy: str = "",
        entry_price: float = 0.0,
        current_price: float = 0.0,
        rejected_gate: str = "",
        rejected_reason: str = "",
        initial_rejected_gate: Optional[str] = None,
        initial_rejected_reason: Optional[str] = None,
        trading_mode: str = "live",
        account_no: str = "",
        bid: float = 0.0,
        ask: float = 0.0,
        spread_pct: float = 0.001,
        rvol: float = 1.0,
        candle_1m_direction: str = "FLAT",
        momentum_3m: float = 0.0,
        vwap_gap: float = 0.0,
        dist_high: float = 0.0,
        rebound_strength: float = 0.50,
        gap_pct: float = 0.0,
        turnover: float = 0.0,
        signal_age_ms: float = 0.0,
        expected_move_pct: float = 0.02,
        atr_normalized_move: float = 1.0,
        expected_gross_profit_pct: float = 0.02,
        expected_cost_pct: float = 0.0023,
        expected_net_move_pct: float = 0.0177,
        expected_net_r: float = 1.0,
        cost_to_opportunity_ratio: float = 0.15,
        predicted_mfe: float = 0.02,
        predicted_mae: float = 0.015,
        stop_distance_pct: float = 0.015,
        position_size: int = 0,
        usable_cash: float = 0.0,
        reserved_cash: float = 0.0,
        broker_psbl_qty: int = 0,
        signal_ttl_result: str = "PASS",
        price_drift_pct: float = 0.0,
        execution_result: str = "REJECTED",
        now: Optional[datetime] = None,
        log_message: bool = True
    ) -> DecisionTraceRecord:
        if isinstance(symbol_or_record, DecisionTraceRecord):
            return self.record_trace(symbol_or_record, log_message=log_message)
        now_str = (now or datetime.now()).isoformat()
        rec = DecisionTraceRecord(
            timestamp=now_str,
            trading_mode=str(trading_mode).lower(),
            account_no=str(account_no),
            symbol=str(symbol_or_record),
            strategy=strategy,
            entry_price=entry_price,
            current_price=current_price,
            bid=bid,
            ask=ask,
            spread_pct=spread_pct,
            rvol=rvol,
            candle_1m_direction=candle_1m_direction,
            momentum_3m=momentum_3m,
            vwap_gap=vwap_gap,
            dist_high=dist_high,
            rebound_strength=rebound_strength,
            gap_pct=gap_pct,
            turnover=turnover,
            signal_age_ms=signal_age_ms,
            expected_move_pct=expected_move_pct,
            atr_normalized_move=atr_normalized_move,
            expected_gross_profit_pct=expected_gross_profit_pct,
            expected_cost_pct=expected_cost_pct,
            expected_net_move_pct=expected_net_move_pct,
            expected_net_r=expected_net_r,
            cost_to_opportunity_ratio=cost_to_opportunity_ratio,
            predicted_mfe=predicted_mfe,
            predicted_mae=predicted_mae,
            stop_distance_pct=stop_distance_pct,
            position_size=position_size,
            usable_cash=usable_cash,
            reserved_cash=reserved_cash,
            broker_psbl_qty=broker_psbl_qty,
            signal_ttl_result=signal_ttl_result,
            price_drift_pct=price_drift_pct,
            execution_result=execution_result,
            decision="NO_TRADE",
            final_reason=rejected_reason,
            rejected_gate=rejected_gate,
            rejected_reason=rejected_reason,
            initial_rejected_gate=initial_rejected_gate or rejected_gate,
            initial_rejected_reason=initial_rejected_reason or rejected_reason
        )
        return self.record_trace(rec, log_message=log_message)

    def get_summary(self, trading_mode: Optional[str] = None) -> Dict[str, Any]:
        """Provides summary metrics for daily report and diagnostics"""
        filtered = self.traces
        if trading_mode:
            filtered = [t for t in filtered if t.trading_mode.lower() == trading_mode.lower()]

        buys = [t for t in filtered if t.decision.upper() in ("BUY", "BUY_APPROVED")]
        no_trades = [t for t in filtered if t.decision.upper() not in ("BUY", "BUY_APPROVED")]

        gate_rejects: Dict[str, int] = {}
        for nt in no_trades:
            gate = nt.reject_gate or nt.rejected_gate or "OTHER"
            gate_rejects[gate] = gate_rejects.get(gate, 0) + 1

        reasons = [nt.reject_reason or nt.rejected_reason or nt.final_reason for nt in no_trades if (nt.reject_reason or nt.rejected_reason or nt.final_reason)]

        def _avg(vals):
            return sum(vals) / len(vals) if vals else 0.0

        return {
            "total_decisions": len(filtered),
            "buy_approved_count": len(buys),
            "no_trade_count": len(no_trades),
            "gate_rejects": gate_rejects,
            "reasons": reasons,
            "avg_buy_rvol": _avg([b.rvol for b in buys]),
            "avg_buy_3m_mom": _avg([b.momentum_3m for b in buys]),
            "avg_buy_vwap_gap": _avg([b.vwap_gap for b in buys]),
            "avg_buy_rebound_strength": _avg([b.rebound_strength for b in buys]),
            "avg_buy_expected_move": _avg([b.expected_move_pct for b in buys]),
            "avg_buy_expected_net_r": _avg([b.expected_net_r for b in buys]),
            "avg_buy_cost_ratio": _avg([b.cost_to_opportunity_ratio for b in buys]),
        }
