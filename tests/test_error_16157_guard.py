import pytest
from datetime import datetime
from unittest.mock import MagicMock
from core.models import Position, TradeSignal, OrderSide, OrderType, OrderStatus, TimeHorizon
from execution.position_manager import PositionManager
from execution.order_router import OrderRouter

REGULAR_TIME = datetime(2026, 9, 17, 10, 30, 0)

def test_sync_from_broker_reconciles_existing_quantity():
    mock_router = MagicMock()
    pm = PositionManager(order_router=mock_router)
    # Add initial position with 7 shares
    pos = Position(
        position_id="RECOVERED_INT_027360",
        time_horizon=TimeHorizon.INTRADAY,
        strategy_id="RESTART_RECOVERY",
        iem_cd="027360",
        name="아주IB투자",
        qty=7,
        entry_price=3770.0,
        current_price=3845.0,
        stop_price=3675,
        target_1r=3865,
        target_2r=3960,
        target_3r=4055,
        r_unit=95.0,
        initial_risk_amount=665.0,
        entry_time=REGULAR_TIME,
        trailing_stop_price=3675,
        highest_price=3845
    )
    pm.positions[pos.position_id] = pos

    # Broker says actually only 5 shares exist
    broker_holdings = [
        {"iem_cd": "027360", "qty": 5, "buy_price": 3770.0, "now_price": 3845}
    ]
    pm.sync_from_broker(broker_holdings)

    # Position quantity should be automatically reconciled to 5 shares
    assert pos.qty == 5
    assert not pos.is_closed

def test_close_position_auto_heals_when_broker_has_zero_qty():
    mock_router = MagicMock()
    mock_router.submit_order.return_value = None
    mock_router.pending_orders = {}

    mock_client = MagicMock()
    mock_client.dry_run = False
    mock_client.act_no = "20201549311"
    mock_client.trade_base_url = "https://api.test"
    mock_client.balance_service.get_balance.return_value = {"holdings": []}
    mock_router.client = mock_client

    pm = PositionManager(order_router=mock_router)
    pos = Position(
        position_id="RECOVERED_INT_027360",
        time_horizon=TimeHorizon.INTRADAY,
        strategy_id="RESTART_RECOVERY",
        iem_cd="027360",
        name="아주IB투자",
        qty=7,
        entry_price=3770.0,
        current_price=3845.0,
        stop_price=3675,
        target_1r=3865,
        target_2r=3960,
        target_3r=4055,
        r_unit=95.0,
        initial_risk_amount=665.0,
        entry_time=REGULAR_TIME,
        trailing_stop_price=3675,
        highest_price=3845
    )
    pm.positions[pos.position_id] = pos

    # Attempt close
    pm._close_position(pos, 3845, REGULAR_TIME, "Trailing Stop")

    # Should detect broker has 0 shares, immediately close position, and remove from active positions
    assert pos.is_closed is True
    assert pos.status == "POSITION_CLOSED"
    assert pos.qty == 0
    assert pos.position_id not in pm.positions
    assert pos in pm.closed_positions

def test_close_position_quarantine_after_three_failures():
    mock_router = MagicMock()
    mock_router.submit_order.return_value = None
    mock_router.pending_orders = {}
    mock_client = MagicMock()
    mock_client.dry_run = False
    mock_client.balance_service.get_balance.side_effect = Exception("API Timeout")
    mock_router.client = mock_client

    pm = PositionManager(order_router=mock_router)
    pos = Position(
        position_id="RECOVERED_INT_027360",
        time_horizon=TimeHorizon.INTRADAY,
        strategy_id="RESTART_RECOVERY",
        iem_cd="027360",
        name="아주IB투자",
        qty=7,
        entry_price=3770.0,
        current_price=3845.0,
        stop_price=3675,
        target_1r=3865,
        target_2r=3960,
        target_3r=4055,
        r_unit=95.0,
        initial_risk_amount=665.0,
        entry_time=REGULAR_TIME,
        trailing_stop_price=3675,
        highest_price=3845
    )
    pm.positions[pos.position_id] = pos

    # Trigger failure 1
    pm._close_position(pos, 3845, REGULAR_TIME, "Trailing Stop")
    assert pos.exit_failure_count == 1
    assert pos.status == "EXIT_FAILED"

    # Trigger failure 2
    pm._close_position(pos, 3845, REGULAR_TIME, "Trailing Stop")
    assert pos.exit_failure_count == 2

    # Trigger failure 3 -> Must quarantine and cease looping!
    pm._close_position(pos, 3845, REGULAR_TIME, "Trailing Stop")
    assert pos.exit_failure_count == 3
    assert pos.is_closed is True
    assert pos.status == "EXIT_QUARANTINED"
    assert pos.position_id not in pm.positions

def test_pre_order_check_blocks_overselling():
    router = OrderRouter(namu_client=MagicMock(), circuit_breaker=None)
    
    signal = TradeSignal(
        strategy_id="INT_MOMENTUM_EXIT",
        time_horizon=TimeHorizon.INTRADAY,
        iem_cd="027360",
        name="아주IB투자",
        side=OrderSide.SELL,
        strategy_price=3845,
        stop_price=0,
        score=100.0,
        reason="TEST",
        timestamp=REGULAR_TIME
    )

    # Balance has only 5 shares
    balance = {
        "holdings": [{"iem_cd": "027360", "qty": 5}]
    }

    # Attempt to sell 7 shares -> Must be rejected!
    passed, msg = router.run_pre_order_checks(
        signal=signal,
        shares=7,
        order_price=3845,
        balance=balance,
        portfolio_risk_status="NORMAL",
        now=REGULAR_TIME
    )
    assert not passed
    assert "INSUFFICIENT_PSBL_QTY" in msg

    # Attempt to sell 5 shares -> Should pass!
    passed2, msg2 = router.run_pre_order_checks(
        signal=signal,
        shares=5,
        order_price=3845,
        balance=balance,
        portfolio_risk_status="NORMAL",
        now=REGULAR_TIME
    )
    assert passed2
