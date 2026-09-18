"""[EXECUTION SIMULATOR v1.0] Realistic Backtest Execution Simulator
(backtester/execution_simulator.py)

Simulates realistic order execution dynamics for Korean stock markets:
- Market Order vs Limit Order distinction
- Bid/Ask Spread, Market Impact, and Depth Participation
- Order latency pipeline: Signal Time -> Order Created -> Order Sent -> Fill
- Partial Fill & Unfilled handling based on bar volume depth
- Gap-down Stop Loss Execution (Open < Stop -> fills at Open with slippage, not Stop)
- Exact transaction costs (broker commissions 0.015% each side, securities transaction tax 0.18% on sell)
"""

import math
from datetime import datetime, timedelta
from enum import Enum
from typing import Dict, Any, Optional, Tuple, List
from dataclasses import dataclass, field

from core.models import OrderSide, OrderType
from core.tick_normalizer import get_tick_size, normalize_price


class OrderFillStatus(str, Enum):
    FILLED = "FILLED"
    PARTIAL_FILL = "PARTIAL_FILL"
    UNFILLED = "UNFILLED"
    REJECTED = "REJECTED"


@dataclass
class SimulatedOrder:
    order_id: str
    symbol: str
    name: str
    side: OrderSide
    order_type: OrderType
    requested_qty: int
    requested_price: float
    signal_time: datetime
    created_at: Optional[datetime] = None
    sent_at: Optional[datetime] = None
    filled_at: Optional[datetime] = None
    status: OrderFillStatus = OrderFillStatus.UNFILLED
    filled_qty: int = 0
    filled_avg_price: float = 0.0
    slippage: float = 0.0
    fee: float = 0.0
    tax: float = 0.0
    reason: str = ""
    rejection_reason: Optional[str] = None


@dataclass
class ExecutionResult:
    status: OrderFillStatus
    filled_qty: int
    filled_avg_price: float
    slippage_amount: float
    fee_amount: float
    tax_amount: float
    total_cost: float
    signal_time: datetime
    fill_time: datetime
    execution_notes: str
    reference_price: float = 0.0
    price_impact: float = 0.0
    tranches: List[Dict[str, Any]] = field(default_factory=list)


class RealisticExecutionSimulator:
    """
    Realistic execution engine reflecting true market dynamics:
    - Participation rate: max 10% of 1-minute volume without partial fill delay
    - Latency: 100ms signal->order, 150ms order->fill
    - Fees: 0.015% buy/sell, Tax: 0.18% sell
    """

    def __init__(
        self,
        signal_to_order_latency_ms: int = 100,
        order_to_fill_latency_ms: int = 150,
        default_spread_ratio: float = 0.0015,  # 0.15% average spread
        max_bar_volume_participation: float = 0.10,  # Max 10% of 1m bar volume
        commission_rate: float = 0.00015,  # 0.015%
        securities_tax_rate: float = 0.0018,  # 0.18% KOSPI/KOSDAQ
    ):
        self.signal_to_order_latency_ms = signal_to_order_latency_ms
        self.order_to_fill_latency_ms = order_to_fill_latency_ms
        self.default_spread_ratio = default_spread_ratio
        self.max_bar_volume_participation = max_bar_volume_participation
        self.commission_rate = commission_rate
        self.securities_tax_rate = securities_tax_rate

    def simulate_entry_execution(
        self,
        order: SimulatedOrder,
        bar: Dict[str, Any],
        quote_book: Optional[Dict[str, Any]] = None
    ) -> ExecutionResult:
        """
        Simulates entry execution (BUY) on the current bar.
        Handles MARKET vs LIMIT, Spread, Depth Participation, and Partial Fills.
        """
        bar_open = float(bar.get("open", bar.get("close", 0)))
        bar_high = float(bar.get("high", bar_open))
        bar_low = float(bar.get("low", bar_open))
        bar_close = float(bar.get("close", bar_open))
        bar_volume = int(bar.get("volume", 10000))

        signal_time = order.signal_time
        order.created_at = signal_time + timedelta(milliseconds=self.signal_to_order_latency_ms)
        order.sent_at = order.created_at + timedelta(milliseconds=50)
        fill_time = order.sent_at + timedelta(milliseconds=self.order_to_fill_latency_ms)
        order.filled_at = fill_time

        # 1. Depth & Maximum fillable quantity check
        max_executable_qty = max(1, int(bar_volume * self.max_bar_volume_participation))
        if quote_book and "ask1_qty" in quote_book:
            max_executable_qty = max(1, int(quote_book["ask1_qty"]))

        if order.requested_qty <= max_executable_qty:
            fill_qty = order.requested_qty
            status = OrderFillStatus.FILLED
        else:
            fill_qty = max_executable_qty
            status = OrderFillStatus.PARTIAL_FILL

        if fill_qty <= 0:
            return ExecutionResult(
                status=OrderFillStatus.UNFILLED,
                filled_qty=0,
                filled_avg_price=0.0,
                slippage_amount=0.0,
                fee_amount=0.0,
                tax_amount=0.0,
                total_cost=0.0,
                signal_time=signal_time,
                fill_time=fill_time,
                execution_notes="Insufficient volume/depth: 0 shares filled"
            )

        # 2. Price Determination based on Order Type
        is_market = (order.order_type in (OrderType.MARKET, "05", "MARKET"))

        if is_market:
            # Market order executes against Ask
            half_spread = (bar_open * self.default_spread_ratio) / 2.0
            # Market impact scales with order size / bar volume
            volume_ratio = min(1.0, order.requested_qty / max(1.0, float(bar_volume)))
            market_impact = bar_open * (0.0005 + 0.05 * volume_ratio)
            base_price = bar_open + half_spread + market_impact
            # Ensure price does not exceed bar high
            execution_price = min(bar_high, max(bar_low, base_price))
            execution_price = float(normalize_price(int(execution_price), OrderSide.BUY))
            slippage = execution_price - bar_open

        else:
            # Limit order: Buyer sets requested_price
            limit_price = float(order.requested_price)
            # Limit order only fills if market touched or traded through limit price
            if bar_low > limit_price:
                # Never touched -> UNFILLED
                return ExecutionResult(
                    status=OrderFillStatus.UNFILLED,
                    filled_qty=0,
                    filled_avg_price=0.0,
                    slippage_amount=0.0,
                    fee_amount=0.0,
                    tax_amount=0.0,
                    total_cost=0.0,
                    signal_time=signal_time,
                    fill_time=fill_time,
                    execution_notes=f"Limit price {limit_price:,.0f} not reached (Bar Low: {bar_low:,.0f})"
                )
            # If bar opened below limit price, buyer gets price improvement (bar_open)
            execution_price = min(limit_price, bar_open)
            execution_price = float(normalize_price(int(execution_price), OrderSide.BUY))
            slippage = execution_price - limit_price

        # 3. Cost calculation
        order_value = execution_price * fill_qty
        fee = order_value * self.commission_rate
        tax = 0.0  # No transaction tax on Buy

        order.status = status
        order.filled_qty = fill_qty
        order.filled_avg_price = execution_price
        order.slippage = slippage
        order.fee = fee
        order.tax = tax

        ref_price = float(order.requested_price) if not is_market else bar_open
        impact = market_impact if is_market else 0.0

        return ExecutionResult(
            status=status,
            filled_qty=fill_qty,
            filled_avg_price=execution_price,
            slippage_amount=slippage * fill_qty,
            fee_amount=fee,
            tax_amount=tax,
            total_cost=fee + tax,
            signal_time=signal_time,
            fill_time=fill_time,
            execution_notes=f"Filled {fill_qty}/{order.requested_qty} shares @ {execution_price:,.0f} ({status.value})",
            reference_price=ref_price,
            price_impact=impact,
            tranches=[{"price": execution_price, "qty": fill_qty}]
        )

    def simulate_exit_execution(
        self,
        symbol: str,
        qty: int,
        exit_reason: str,
        bar: Dict[str, Any],
        order_type: OrderType = OrderType.MARKET,
        target_price: Optional[float] = None,
        stop_price: Optional[float] = None,
        timestamp: Optional[datetime] = None
    ) -> ExecutionResult:
        """
        Simulates exit execution (SELL) on the current bar.
        Handles STOP_LOSS (with gap-down penalty), TARGET / SCALE_OUT (Limit),
        TRAILING_STOP, TIME_STOP, and EOD (Market).
        """
        bar_open = float(bar.get("open", bar.get("close", 0)))
        bar_high = float(bar.get("high", bar_open))
        bar_low = float(bar.get("low", bar_open))
        bar_volume = int(bar.get("volume", 10000))
        fill_time = timestamp or datetime.now()

        # Depth check
        # Market orders (Stop Loss, Trailing Stop, Time Stop, EOD) sweep available depth and fill fully with market impact
        is_market_order = (
            order_type in (OrderType.MARKET, "05", "MARKET") or
            "스톱" in exit_reason or "STOP" in exit_reason.upper() or "손절" in exit_reason or
            "장마감" in exit_reason or "EOD" in exit_reason.upper() or
            "TIME_STOP" in exit_reason or "Trailing" in exit_reason
        )
        if is_market_order:
            fill_qty = qty
            status = OrderFillStatus.FILLED
        else:
            max_executable_qty = max(1, int(bar_volume * self.max_bar_volume_participation * 1.5))
            fill_qty = min(qty, max_executable_qty)
            status = OrderFillStatus.FILLED if fill_qty == qty else OrderFillStatus.PARTIAL_FILL

        ref_stop = float(stop_price) if stop_price else bar_low
        ref_target = float(target_price) if target_price else bar_high
        market_impact = 0.0

        # 1. STOP LOSS Exit (with gap down handling)
        if "스톱" in exit_reason or "STOP" in exit_reason.upper() or "손절" in exit_reason:
            ref_stop = float(stop_price) if stop_price else bar_low
            # Gap Down: If market opens strictly below stop_price, execute at open or lower!
            if bar_open < ref_stop:
                # Critical Realism: Cannot fill at stop_price during overnight gap-down!
                execution_price = bar_open * (1.0 - 0.001)  # Open minus 0.1% slippage
            else:
                # Normal intraday stop touch: Low touched stop price
                execution_price = min(ref_stop, bar_open) * (1.0 - 0.0005)
            execution_price = min(bar_high, max(bar_low * 0.99, execution_price))
            execution_price = float(normalize_price(int(execution_price), OrderSide.SELL))
            slippage = ref_stop - execution_price  # Slippage is positive when selling lower
            market_impact = max(0.0, ref_stop - execution_price)

        # 2. TARGET / SCALE-OUT Exit (Limit Order)
        elif "익절" in exit_reason or "TARGET" in exit_reason.upper() or "SCALE_OUT" in exit_reason.upper():
            ref_target = float(target_price) if target_price else bar_high
            # Limit sell fills if High reached target
            if bar_high < ref_target:
                return ExecutionResult(
                    status=OrderFillStatus.UNFILLED,
                    filled_qty=0,
                    filled_avg_price=0.0,
                    slippage_amount=0.0,
                    fee_amount=0.0,
                    tax_amount=0.0,
                    total_cost=0.0,
                    signal_time=fill_time,
                    fill_time=fill_time,
                    execution_notes=f"Target {ref_target:,.0f} not reached (Bar High: {bar_high:,.0f})"
                )
            # If bar opened above target price, seller gets positive price improvement
            execution_price = max(ref_target, bar_open)
            execution_price = float(normalize_price(int(execution_price), OrderSide.SELL))
            slippage = ref_target - execution_price

        # 3. TRAILING_STOP / TIME_STOP / EOD (Market Orders)
        else:
            half_spread = (bar_open * self.default_spread_ratio) / 2.0
            volume_ratio = min(1.0, qty / max(1.0, float(bar_volume)))
            market_impact = bar_open * (0.0005 + 0.05 * volume_ratio)
            base_price = bar_open - half_spread - market_impact
            execution_price = max(bar_low, min(bar_high, base_price))
            execution_price = float(normalize_price(int(execution_price), OrderSide.SELL))
            slippage = bar_open - execution_price

        # Costs on Sell: Commission + Securities Transaction Tax (0.18%)
        order_value = execution_price * fill_qty
        fee = order_value * self.commission_rate
        tax = order_value * self.securities_tax_rate

        ref_p = ref_stop if ("스톱" in exit_reason or "STOP" in exit_reason.upper() or "손절" in exit_reason) else (ref_target if ("익절" in exit_reason or "TARGET" in exit_reason.upper() or "SCALE_OUT" in exit_reason.upper()) else bar_open)
        impact_val = market_impact if not ("익절" in exit_reason or "TARGET" in exit_reason.upper() or "SCALE_OUT" in exit_reason.upper()) else 0.0

        return ExecutionResult(
            status=status,
            filled_qty=fill_qty,
            filled_avg_price=execution_price,
            slippage_amount=slippage * fill_qty,
            fee_amount=fee,
            tax_amount=tax,
            total_cost=fee + tax,
            signal_time=fill_time,
            fill_time=fill_time,
            execution_notes=f"Exit: {exit_reason} -> {fill_qty} shares @ {execution_price:,.0f} ({status.value})",
            reference_price=ref_p,
            price_impact=impact_val
        )
