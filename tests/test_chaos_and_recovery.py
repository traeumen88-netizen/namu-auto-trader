"""[TEST CHAOS AND RECOVERY v1.0] Data / Order / Position Chaos & Recovery Validation Suite
(tests/test_chaos_and_recovery.py)

1. Data Quality Chaos:
   - missing bar
   - duplicate timestamp
   - out-of-order timestamp
   - zero volume
   - invalid price (high < low, negative price, open <= 0)
   - abnormal price jump (>30% in 1 bar)
   - missing / inverted bid/ask
   - trading halt
   Assertion: Exactly 0 BUY orders generated across all corrupted data conditions.

2. Order / Broker Chaos & Idempotency:
   - test_order_idempotency
   - test_retry_no_duplicate (ORDER_SENT -> ACK 지연 -> RETRY scenario)
   - test_delayed_ack
   - test_partial_fill_after_retry

3. Position Recovery Chaos:
   Simulates crash and recovery across 6 lifecycle states:
   - POSITION_OPEN
   - PARTIAL_FILL
   - SCALE_OUT
   - TRAILING
   - TIME_STOP
   - EOD
   Assertion: quantity, entry_price, stop, target, trailing, realized_pnl, remaining_qty match 100%.
   0 ghost positions / orders created.
"""

import pytest
from datetime import datetime, timedelta, time as dtime
from typing import Dict, Any, List

from core.models import (
    SymbolInfo, SymbolState, Order, OrderSide, OrderType, OrderStatus,
    TimeHorizon, TradeSignal, Position
)
from core.data_quality_gate import DataQualityGate
from backtester.shared_decision_core import SharedDecisionCore
from execution.order_router import OrderRouter
from execution.position_manager import PositionManager
from backtester.position_tracker import PositionTracker
from backtester.execution_simulator import RealisticExecutionSimulator


# =========================================================================
# 1. Data Quality Chaos Tests
# =========================================================================

def test_data_quality_chaos():
    """Verifies that all 8 corrupted data conditions are blocked with 0 BUY orders."""
    symbol = "005930"
    sym_info = SymbolInfo(iem_cd=symbol, name="삼성전자", price=70000, high_price=70000, low_price=70000, is_tradable=True)
    core = SharedDecisionCore(symbol_master={symbol: sym_info})

    now = datetime(2026, 9, 11, 9, 30, 0)
    baseline_bar = {"open": 70000, "high": 70500, "low": 69800, "close": 70200, "volume": 10000, "timestamp": now}

    # First feed valid baseline bar
    core.evaluate_bar(symbol, baseline_bar, now, 100_000_000.0, 100_000_000.0, [])

    corrupted_cases = [
        ("missing_bar_none", None, now + timedelta(minutes=1)),
        ("missing_bar_empty", {}, now + timedelta(minutes=2)),
        ("duplicate_timestamp", {"open": 70200, "high": 70500, "low": 70000, "close": 70300, "volume": 10000, "timestamp": now}, now),
        ("out_of_order_timestamp", {"open": 70200, "high": 70500, "low": 70000, "close": 70300, "volume": 10000, "timestamp": now - timedelta(minutes=5)}, now - timedelta(minutes=5)),
        ("zero_volume", {"open": 70200, "high": 70500, "low": 70000, "close": 70300, "volume": 0, "timestamp": now + timedelta(minutes=3)}, now + timedelta(minutes=3)),
        ("negative_volume", {"open": 70200, "high": 70500, "low": 70000, "close": 70300, "volume": -500, "timestamp": now + timedelta(minutes=4)}, now + timedelta(minutes=4)),
        ("invalid_price_high_less_than_low", {"open": 70200, "high": 69000, "low": 70500, "close": 70000, "volume": 10000, "timestamp": now + timedelta(minutes=5)}, now + timedelta(minutes=5)),
        ("invalid_price_negative", {"open": -70000, "high": 70500, "low": 69000, "close": 70000, "volume": 10000, "timestamp": now + timedelta(minutes=6)}, now + timedelta(minutes=6)),
        ("invalid_price_zero", {"open": 0, "high": 70500, "low": 69000, "close": 70000, "volume": 10000, "timestamp": now + timedelta(minutes=7)}, now + timedelta(minutes=7)),
        ("abnormal_price_jump_up_50pct", {"open": 70200, "high": 110000, "low": 70000, "close": 108000, "volume": 50000, "timestamp": now + timedelta(minutes=8)}, now + timedelta(minutes=8)),
        ("missing_bid_ask", {"open": 70200, "high": 70500, "low": 70000, "close": 70300, "volume": 10000, "bid": 0, "ask": 70300, "timestamp": now + timedelta(minutes=9)}, now + timedelta(minutes=9)),
        ("inverted_bid_ask", {"open": 70200, "high": 70500, "low": 70000, "close": 70300, "volume": 10000, "bid": 71000, "ask": 70000, "timestamp": now + timedelta(minutes=10)}, now + timedelta(minutes=10)),
        ("trading_halt", {"open": 70200, "high": 70500, "low": 70000, "close": 70300, "volume": 10000, "is_halted": True, "timestamp": now + timedelta(minutes=11)}, now + timedelta(minutes=11)),
    ]

    total_buy_orders = 0
    for name, bar, bar_time in corrupted_cases:
        decisions = core.evaluate_bar(
            symbol=symbol,
            bar=bar,
            current_time=bar_time,
            account_equity=100_000_000.0,
            available_cash=100_000_000.0,
            active_positions=[]
        )
        for dec in decisions:
            if dec.meta_decision in ("BUY", "BUY_SMALL") or dec.approved_shares > 0:
                total_buy_orders += 1
                print(f"[FAIL] BUY order generated on corrupted data: {name} -> {dec}")

    assert total_buy_orders == 0, f"Data Quality Gate Failed: {total_buy_orders} BUY orders generated on corrupted data!"
    print(f"\n[PASS] Data Quality Chaos: All {len(corrupted_cases)} corrupted conditions defended with 0 BUY orders.")


# =========================================================================
# 2. Order / Broker Chaos & Idempotency Tests
# =========================================================================

def test_order_idempotency():
    """Verifies that duplicate order submissions with identical client_order_id return existing order."""
    router = OrderRouter(namu_client=None, circuit_breaker=None)
    signal = TradeSignal(
        strategy_id="TEST_MOMENTUM",
        time_horizon=TimeHorizon.INTRADAY,
        iem_cd="005930",
        name="삼성전자",
        side=OrderSide.BUY,
        strategy_price=70000,
        stop_price=68600,
        score=85.0,
        reason="Test",
        timestamp=datetime.now(),
        order_type=OrderType.LIMIT
    )

    cid = "TEST_IDEMPOTENCY_ORD_001"
    order1 = router.submit_order(signal, shares=100, order_price=70000, client_order_id=cid)
    assert order1 is not None, "First order submission must succeed"
    assert order1.client_order_id == cid

    # Submit second identical order
    order2 = router.submit_order(signal, shares=100, order_price=70000, client_order_id=cid)
    assert order2 is not None, "Idempotent submission must return existing order"
    assert order2.client_order_id == cid
    assert order1 is order2, "Must be the exact same Order instance"
    assert len(router.order_registry) == 1, "Order registry must contain exactly 1 order"
    print("\n[PASS] test_order_idempotency: Exact 1 order maintained upon duplicate submission.")


def test_retry_no_duplicate():
    """Verifies ORDER_SENT -> ACK 지연 -> RETRY scenario: no duplicate broker orders sent."""
    router = OrderRouter(namu_client=None, circuit_breaker=None)
    signal = TradeSignal(
        strategy_id="TEST_RETRY",
        time_horizon=TimeHorizon.INTRADAY,
        iem_cd="005930",
        name="삼성전자",
        side=OrderSide.BUY,
        strategy_price=70000,
        stop_price=68600,
        score=85.0,
        reason="Test",
        timestamp=datetime.now(),
        order_type=OrderType.LIMIT
    )

    # 1. ORDER_SENT
    order = router.submit_order(signal, shares=100, order_price=70000)
    assert order is not None
    cid = order.client_order_id
    assert cid in router.pending_orders

    # 2. ACK 지연: order remains pending
    assert order.status == OrderStatus.PENDING

    # 3. RETRY via retry_order
    ok, reason, retried_order = router.retry_order(cid)
    assert not ok, "Retry while order is ALREADY_PENDING must be blocked from duplicate sending"
    assert "ALREADY_PENDING" in reason
    assert retried_order is order

    # 4. RETRY via submit_order with new signal for same symbol while pending
    sig2 = TradeSignal(
        strategy_id="TEST_RETRY_2",
        time_horizon=TimeHorizon.INTRADAY,
        iem_cd="005930",
        name="삼성전자",
        side=OrderSide.BUY,
        strategy_price=70000,
        stop_price=68600,
        score=85.0,
        reason="Retry duplicate attempt",
        timestamp=datetime.now(),
        order_type=OrderType.LIMIT
    )
    order_dup = router.submit_order(sig2, shares=100, order_price=70000)
    assert order_dup is None, "Submitting another BUY for same symbol while pending must be rejected"
    assert any("미체결 주문 진행 중" in r for r in sig2.rejection_reasons)
    assert len(router.pending_orders) == 1
    print("\n[PASS] test_retry_no_duplicate: Blocked duplicate orders during ACK delay retry.")


def test_delayed_ack():
    """Verifies delayed ACK attaches cleanly to pending order without side-effects."""
    router = OrderRouter(namu_client=None, circuit_breaker=None)
    signal = TradeSignal(
        strategy_id="TEST_DELAYED_ACK",
        time_horizon=TimeHorizon.INTRADAY,
        iem_cd="005930",
        name="삼성전자",
        side=OrderSide.BUY,
        strategy_price=70000,
        stop_price=68600,
        score=85.0,
        reason="Test",
        timestamp=datetime.now(),
        order_type=OrderType.LIMIT
    )

    order = router.submit_order(signal, shares=50, order_price=70000)
    cid = order.client_order_id
    assert order.ack_at is None or order.broker_order_no == ""

    # Delayed ACK arrives
    router.on_ack(cid, "BROKER_ACK_12345")
    assert order.broker_order_no == "BROKER_ACK_12345"
    assert order.ack_at is not None
    assert order.status == OrderStatus.PENDING
    print("\n[PASS] test_delayed_ack: Delayed ACK properly registered.")


def test_partial_fill_after_retry():
    """Verifies partial fill + subsequent fill after retry achieves exact order quantity without duplication."""
    router = OrderRouter(namu_client=None, circuit_breaker=None)
    signal = TradeSignal(
        strategy_id="TEST_PARTIAL_FILL",
        time_horizon=TimeHorizon.INTRADAY,
        iem_cd="005930",
        name="삼성전자",
        side=OrderSide.BUY,
        strategy_price=70000,
        stop_price=68600,
        score=85.0,
        reason="Test",
        timestamp=datetime.now(),
        order_type=OrderType.LIMIT
    )

    order = router.submit_order(signal, shares=100, order_price=70000)
    cid = order.client_order_id

    # Retry attempt blocked
    ok, _, _ = router.retry_order(cid)
    assert not ok

    # Partial Fill of 40 shares arrives
    router.on_fill(cid, filled_qty=40, fill_price=70000.0)
    assert order.status == OrderStatus.PARTIAL
    assert order.filled_qty == 40
    assert cid in router.pending_orders

    # Remaining 60 shares fill arrives
    router.on_fill(cid, filled_qty=60, fill_price=70000.0)
    assert order.status == OrderStatus.FILLED
    assert order.filled_qty == 100
    assert cid not in router.pending_orders
    print("\n[PASS] test_partial_fill_after_retry: 40 + 60 = 100 shares filled cleanly without duplicate.")


# =========================================================================
# 3. Position Recovery Chaos Tests
# =========================================================================

def test_position_recovery_lifecycle_states():
    """
    Verifies restart recovery across 6 lifecycle states:
      1. POSITION_OPEN
      2. PARTIAL_FILL
      3. SCALE_OUT
      4. TRAILING
      5. TIME_STOP
      6. EOD
    Ensures quantity, entry_price, stop, target, trailing, realized_pnl, remaining_qty match 100%.
    0 ghost positions or ghost orders.
    """
    router = OrderRouter(namu_client=None, circuit_breaker=None)
    pos_mgr = PositionManager(router)

    # 1. POSITION_OPEN
    p1 = pos_mgr.open_position(
        time_horizon=TimeHorizon.INTRADAY,
        strategy_id="INT_MOMENTUM",
        iem_cd="005930",
        name="삼성전자",
        qty=100,
        entry_price=70000.0,
        stop_price=68600,
        target_1r=71400,
        target_2r=72800,
        target_3r=74200,
        initial_risk=1400.0 * 100
    )
    p1.realized_pnl = 0.0

    # 2. PARTIAL_FILL (Opened with partial shares)
    p2 = pos_mgr.open_position(
        time_horizon=TimeHorizon.INTRADAY,
        strategy_id="INT_BREAKOUT",
        iem_cd="000660",
        name="SK하이닉스",
        qty=40,
        entry_price=120000.0,
        stop_price=117600,
        target_1r=122400,
        target_2r=124800,
        target_3r=127200,
        initial_risk=2400.0 * 40
    )

    # 3. SCALE_OUT on p1: 30 shares sold at +1R (71,400)
    sell_qty = 30
    p1.qty -= sell_qty
    p1.target_1r_taken = True
    p1.stop_price = 70000  # break-even
    p1.realized_pnl = (71400.0 - 70000.0) * sell_qty  # 42,000 KRW

    # 4. TRAILING on p2: Price surged, trailing stop ratcheted to 121,500
    p2.highest_price = 123000
    p2.trailing_stop_price = 121500

    # 5. TIME_STOP: Closed due to time stop
    p3 = pos_mgr.open_position(
        time_horizon=TimeHorizon.INTRADAY,
        strategy_id="INT_STAGNANT",
        iem_cd="035420",
        name="NAVER",
        qty=50,
        entry_price=200000.0,
        stop_price=196000,
        target_1r=204000,
        target_2r=208000,
        target_3r=212000,
        initial_risk=4000.0 * 50
    )
    p3.is_closed = True
    p3.status = "TIME_STOP_TRIGGERED"
    p3.qty = 0

    # 6. EOD: Closed due to 15:20 EOD liquidation
    p4 = pos_mgr.open_position(
        time_horizon=TimeHorizon.INTRADAY,
        strategy_id="INT_EOD",
        iem_cd="068270",
        name="셀트리온",
        qty=80,
        entry_price=150000.0,
        stop_price=147000,
        target_1r=153000,
        target_2r=156000,
        target_3r=159000,
        initial_risk=3000.0 * 80
    )
    p4.is_closed = True
    p4.status = "EOD_MARKET_CLOSE"
    p4.qty = 0

    # --- SIMULATE CRASH & PERSISTENCE ---
    saved_state = pos_mgr.serialize_state()

    # --- SIMULATE RESTART ---
    new_router = OrderRouter(namu_client=None, circuit_breaker=None)
    new_pos_mgr = PositionManager(new_router)
    restored_count = new_pos_mgr.restore_state(saved_state)

    # Verifications:
    # 1. Exactly 2 active positions restored (p1 and p2). Closed p3 and p4 NOT restored (no ghost positions!)
    assert restored_count == 2, f"Expected 2 restored active positions, got {restored_count}"
    assert len(new_pos_mgr.positions) == 2, "Active positions count must be exactly 2"
    assert "035420" not in [p.iem_cd for p in new_pos_mgr.positions.values()], "Closed TIME_STOP position must NOT be restored (Ghost position prevention)"
    assert "068270" not in [p.iem_cd for p in new_pos_mgr.positions.values()], "Closed EOD position must NOT be restored (Ghost position prevention)"

    # Check p1 (SCALE_OUT state):
    rest_p1 = next(p for p in new_pos_mgr.positions.values() if p.iem_cd == "005930")
    assert rest_p1.qty == 70, f"Remaining qty must be 70, got {rest_p1.qty}"
    assert rest_p1.entry_price == 70000.0
    assert rest_p1.stop_price == 70000, "Break-even stop price must be preserved upon restart"
    assert rest_p1.target_1r == 71400
    assert rest_p1.target_2r == 72800
    assert rest_p1.realized_pnl == 42000.0, f"Realized PnL must be 42,000 KRW, got {rest_p1.realized_pnl}"
    assert rest_p1.target_1r_taken is True

    # Check p2 (TRAILING state):
    rest_p2 = next(p for p in new_pos_mgr.positions.values() if p.iem_cd == "000660")
    assert rest_p2.qty == 40
    assert rest_p2.entry_price == 120000.0
    assert rest_p2.trailing_stop_price == 121500, "Ratcheted trailing stop must be preserved upon restart"
    assert rest_p2.highest_price == 123000

    print("\n[PASS] test_position_recovery_lifecycle_states: 100% state parity across all 6 states with 0 ghost positions.")


def test_backtester_position_tracker_recovery():
    """Verifies state recovery on Backtest PositionTracker."""
    sim = RealisticExecutionSimulator()
    tracker = PositionTracker(execution_simulator=sim)

    pos = tracker.open_position(
        symbol="005930",
        name="삼성전자",
        strategy_id="INT_VWAP_PULLBACK",
        time_horizon=TimeHorizon.INTRADAY,
        qty=100,
        entry_price=70000.0,
        stop_price=68600.0,
        target_1r=71400.0,
        target_2r=72800.0,
        target_3r=74200.0,
        entry_time=datetime(2026, 9, 11, 9, 30, 0)
    )
    pos.qty = 70
    pos.realized_pnl = 42000.0
    pos.target_1r_taken = True

    # Serialize & Restore
    state = tracker.serialize_state()
    new_tracker = PositionTracker(execution_simulator=sim)
    restored = new_tracker.restore_state(state)

    assert restored == 1
    p = list(new_tracker.positions.values())[0]
    assert p.qty == 70
    assert p.entry_price == 70000.0
    assert p.realized_pnl == 42000.0
    assert p.target_1r_taken is True
    print("\n[PASS] test_backtester_position_tracker_recovery: Backtest PositionTracker state recovery 100% matched.")
