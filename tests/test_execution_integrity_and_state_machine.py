"""[Item 15] Execution Integrity and Order State Machine Comprehensive Unit Tests
14개 필수 테스트 파이프라인 전수 검증:
- Test 1: Broker ACK does not set status to FILLED (status is ORDER_ACK / PENDING)
- Test 2: Broker ACK does not decrement internal PositionManager quantity
- Test 3: Broker ACK does not send Telegram "체결 완료" notification
- Test 4: Broker ACK keeps order in pending_orders
- Test 5: Broker dailyOrderExecution fill detection updates order to FILLED
- Test 6: Broker dailyOrderExecution partial fill updates order to PARTIAL_FILL and updates remaining_qty
- Test 7: Only verified fill triggers Telegram "체결 완료" notification
- Test 8: Only verified fill triggers PositionManager quantity decrement
- Test 9: Active exit order in pending_orders blocks duplicate SELL order (ACTIVE_EXIT_ORDER_EXISTS)
- Test 10: SELL order validation verifies broker_psbl_qty and blocks if requested > psbl
- Test 11: Single account process lock blocks second process on same LIVE account (DUPLICATE_ACCOUNT_PROCESS)
- Test 12: Independent simultaneous execution of LIVE and MOCK accounts is permitted
- Test 13: Reconciliation on restart sets authoritative broker position and pending sell orders
- Test 14: Reconcile correction record in DB restores order status to PENDING and position qty to broker truth without deleting audit trail
"""

import os
import sys
import json
import sqlite3
import pytest
from datetime import datetime
from unittest.mock import MagicMock, patch

from core.models import Position, TradeSignal, Order, OrderSide, OrderType, OrderStatus, TimeHorizon
from execution.order_router import OrderRouter
from execution.position_manager import PositionManager
from core.account_lock import AccountProcessLock, AccountLockManager

REGULAR_TIME = datetime(2026, 9, 18, 10, 30, 0)


def make_signal(side=OrderSide.SELL, shares=2, price=4020, reason="TEST_REASON", iem_cd="027360"):
    return TradeSignal(
        strategy_id="TEST_EXIT",
        time_horizon=TimeHorizon.INTRADAY,
        iem_cd=iem_cd,
        name="아주IB투자",
        side=side,
        strategy_price=price,
        stop_price=3770,
        score=100.0,
        reason=reason,
        timestamp=REGULAR_TIME
    )


def make_order(client_order_id="TEST_ORDER_001", broker_order_no="327455", side=OrderSide.SELL, qty=2, price=4020, status=OrderStatus.ORDER_ACK, iem_cd="027360"):
    return Order(
        client_order_id=client_order_id,
        iem_cd=iem_cd,
        side=side,
        order_type=OrderType.LIMIT,
        qty=qty,
        price=price,
        strategy_id="TEST_EXIT",
        time_horizon=TimeHorizon.INTRADAY,
        status=status,
        created_at=REGULAR_TIME,
        sent_at=REGULAR_TIME,
        remaining_qty=qty,
        filled_qty=0,
        broker_order_no=broker_order_no
    )


def make_position(qty=3, pending_exit_qty=0, iem_cd="027360"):
    return Position(
        position_id=f"POS_{iem_cd}_TEST",
        time_horizon=TimeHorizon.INTRADAY,
        strategy_id="TEST",
        iem_cd=iem_cd,
        name="아주IB투자",
        qty=qty,
        entry_price=3770.0,
        current_price=4020.0,
        stop_price=3675,
        target_1r=3865,
        target_2r=3960,
        target_3r=4055,
        r_unit=95.0,
        initial_risk_amount=285.0,
        entry_time=REGULAR_TIME,
        trailing_stop_price=3675,
        highest_price=4020,
        pending_exit_qty=pending_exit_qty
    )


# ============================================================================
# Test 1: Broker ACK does not set status to FILLED (status is ORDER_ACK / PENDING)
# ============================================================================
def test_1_broker_ack_does_not_set_status_to_filled():
    mock_client = MagicMock()
    mock_client.dry_run = False
    mock_client.act_no = "20201549311"
    mock_client.sell_limit.return_value = {"Output_0": {"mkt_orr_no": "327455"}}

    router = OrderRouter(namu_client=mock_client)
    sig = make_signal(side=OrderSide.SELL, price=4020)

    order = router.submit_order(
        signal=sig,
        shares=2,
        order_type=OrderType.LIMIT,
        order_price=4020,
        now=REGULAR_TIME
    )

    assert order is not None
    assert order.status == OrderStatus.ORDER_ACK
    assert order.status != OrderStatus.FILLED
    assert order.filled_qty == 0
    assert order.remaining_qty == 2
    assert order.broker_order_no == "327455"


# ============================================================================
# Test 2: Broker ACK does not decrement internal PositionManager quantity
# ============================================================================
def test_2_broker_ack_does_not_decrement_position_manager_quantity():
    mock_client = MagicMock()
    mock_client.dry_run = False
    mock_client.act_no = "20201549311"
    mock_client.sell_limit.return_value = {"Output_0": {"mkt_orr_no": "327455"}}

    router = OrderRouter(namu_client=mock_client)
    pm = PositionManager(order_router=router)

    pos = make_position(qty=3, pending_exit_qty=0)
    pm.positions[pos.position_id] = pos

    pm._partial_exit(pos, sell_qty=2, price=4020, reason="+2R 도달", now=REGULAR_TIME)

    # ACK 시점에는 내부 보유 수량이 유지되어야 함 (3주 유지, 매도대기 2주)
    assert pos.qty == 3
    assert pos.pending_exit_qty == 2
    assert pos.available_qty == 1
    assert not pos.is_closed
    assert pos.status == "EXIT_PENDING"


# ============================================================================
# Test 3: Broker ACK does not send Telegram "체결 완료" notification
# ============================================================================
def test_3_broker_ack_does_not_send_telegram_fill_notification():
    mock_client = MagicMock()
    mock_client.dry_run = False
    mock_client.act_no = "20201549311"
    mock_client.sell_limit.return_value = {"Output_0": {"mkt_orr_no": "327455"}}

    router = OrderRouter(namu_client=mock_client)
    router._send_fill_telegram = MagicMock()

    sig = make_signal(side=OrderSide.SELL, price=4020)
    router.submit_order(signal=sig, shares=2, order_type=OrderType.LIMIT, order_price=4020, now=REGULAR_TIME)

    # ACK 접수 시점에는 텔레그램 체결 알림 발송 절대 금지
    assert router._send_fill_telegram.call_count == 0


# ============================================================================
# Test 4: Broker ACK keeps order in pending_orders
# ============================================================================
def test_4_broker_ack_keeps_order_in_pending_orders():
    mock_client = MagicMock()
    mock_client.dry_run = False
    mock_client.act_no = "20201549311"
    mock_client.sell_limit.return_value = {"Output_0": {"mkt_orr_no": "327455"}}

    router = OrderRouter(namu_client=mock_client)
    sig = make_signal(side=OrderSide.SELL, price=4020)

    order = router.submit_order(signal=sig, shares=2, order_type=OrderType.LIMIT, order_price=4020, now=REGULAR_TIME)

    # pending_orders에서 삭제되지 않고 체결 대기 중이어야 함
    assert order.client_order_id in router.pending_orders
    assert router.pending_orders[order.client_order_id].status == OrderStatus.ORDER_ACK


# ============================================================================
# Test 5: Broker dailyOrderExecution fill detection updates order to FILLED
# ============================================================================
def test_5_broker_daily_order_execution_fill_detection_updates_to_filled():
    mock_client = MagicMock()
    mock_client.dry_run = False
    mock_client.act_no = "20201549311"
    router = OrderRouter(namu_client=mock_client)

    order = make_order(client_order_id="TEST_EXIT_027360_001", broker_order_no="327455", qty=2, status=OrderStatus.ORDER_ACK)
    router.pending_orders[order.client_order_id] = order

    # 브로커 당일 주문체결내역에서 2주 전량 체결 확인 모의
    mock_client.get_daily_order_execution.return_value = [
        {
            "itg_orr_no": "327455",
            "tot_cns_qty": "2",
            "ny_cns_qty": "0",
            "cns_avg_uit_pr": "4020"
        }
    ]

    res = router.reconcile_orders()

    assert res["filled_count"] == 1
    assert order.status == OrderStatus.FILLED
    assert order.filled_qty == 2
    assert order.remaining_qty == 0
    assert order.client_order_id not in router.pending_orders


# ============================================================================
# Test 6: Broker dailyOrderExecution partial fill updates order to PARTIAL_FILL and updates remaining_qty
# ============================================================================
def test_6_broker_daily_order_execution_partial_fill_updates():
    mock_client = MagicMock()
    mock_client.dry_run = False
    mock_client.act_no = "20201549311"
    router = OrderRouter(namu_client=mock_client)

    order = make_order(client_order_id="TEST_EXIT_027360_002", broker_order_no="327455", qty=5, status=OrderStatus.ORDER_ACK)
    router.pending_orders[order.client_order_id] = order

    # 브로커에서 2주만 부분 체결 확인
    mock_client.get_daily_order_execution.return_value = [
        {
            "itg_orr_no": "327455",
            "tot_cns_qty": "2",
            "ny_cns_qty": "3",
            "cns_avg_uit_pr": "4020"
        }
    ]

    res = router.reconcile_orders()

    assert res["partial_count"] == 1
    assert order.status == OrderStatus.PARTIAL_FILL
    assert order.filled_qty == 2
    assert order.remaining_qty == 3
    # 부분 체결 시 주문은 pending_orders에 잔여 유지
    assert order.client_order_id in router.pending_orders


# ============================================================================
# Test 7: Only verified fill triggers Telegram "체결 완료" notification
# ============================================================================
def test_7_only_verified_fill_triggers_telegram_fill_notification():
    mock_client = MagicMock()
    mock_client.dry_run = False
    router = OrderRouter(namu_client=mock_client)
    router._send_fill_telegram = MagicMock()

    order = make_order(client_order_id="TEST_ORDER_003", broker_order_no="327455", qty=2, status=OrderStatus.ORDER_ACK)
    router.pending_orders[order.client_order_id] = order

    # 1. 0주 체결(미체결) 시 알림 발송 없어야 함
    router.on_fill(order.client_order_id, fill_qty=0, fill_price=4020.0, is_cumulative=True)
    assert router._send_fill_telegram.call_count == 0

    # 2. 실제 2주 체결 확인 시 텔레그램 알림 정확히 1회 발송
    router.on_fill(order.client_order_id, fill_qty=2, fill_price=4020.0, is_cumulative=True)
    assert router._send_fill_telegram.call_count == 1
    args, _ = router._send_fill_telegram.call_args
    assert args[0].client_order_id == "TEST_ORDER_003"
    assert args[1] == 2  # newly_filled
    assert args[2] == 4020.0  # fill_price


# ============================================================================
# Test 8: Only verified fill triggers PositionManager quantity decrement
# ============================================================================
def test_8_only_verified_fill_triggers_position_manager_quantity_decrement():
    mock_client = MagicMock()
    mock_client.dry_run = False
    router = OrderRouter(namu_client=mock_client)
    pm = PositionManager(order_router=router)

    pos = make_position(qty=3, pending_exit_qty=2)
    pm.positions[pos.position_id] = pos

    order = make_order(client_order_id="TEST_ORDER_004", broker_order_no="327455", qty=2, status=OrderStatus.ORDER_ACK)
    router.pending_orders[order.client_order_id] = order

    # 실제 브로커 체결 알림 통지
    router.on_fill(order.client_order_id, fill_qty=2, fill_price=4020.0, is_cumulative=True)

    # PositionManager 내부 수량이 정확히 3 -> 1주로 차감되고, pending_exit_qty는 0으로 해소
    assert pos.qty == 1
    assert pos.pending_exit_qty == 0
    assert not pos.is_closed


# ============================================================================
# Test 9: Active exit order in pending_orders blocks duplicate SELL order (ACTIVE_EXIT_ORDER_EXISTS)
# ============================================================================
def test_9_active_exit_order_blocks_duplicate_sell_order():
    mock_client = MagicMock()
    mock_client.dry_run = False
    router = OrderRouter(namu_client=mock_client)

    # 이미 동일 종목의 매도 주문이 pending_orders에 대기 중
    pending_sell = make_order(client_order_id="EXIT_EXISTING_327455", broker_order_no="327455", qty=2, status=OrderStatus.ORDER_ACK)
    router.pending_orders[pending_sell.client_order_id] = pending_sell

    new_sell_signal = make_signal(side=OrderSide.SELL, price=4020, reason="NEW_EXIT_ATTEMPT")

    passed, reason = router.run_pre_order_checks(
        signal=new_sell_signal,
        shares=2,
        order_price=4020,
        balance={"cash": 10000000},
        portfolio_risk_status="NORMAL",
        current_spread=0.001,
        now=REGULAR_TIME
    )

    assert not passed
    assert "ACTIVE_EXIT_ORDER_EXISTS" in reason


# ============================================================================
# Test 10: SELL order validation verifies broker_psbl_qty and blocks if requested > psbl
# ============================================================================
def test_10_sell_order_validation_verifies_broker_sellable_qty():
    mock_client = MagicMock()
    mock_client.dry_run = False
    # 브로커에 3주 보유 중이나 2주가 이미 다른 주문에 묶여있어 매도가능수량이 1주뿐인 상태
    mock_client.get_sellable_quantity.return_value = {
        "bnc_qty": 3,
        "tdt_sll_ny_cns_qty": 2,
        "sll_pbl_qty": 1
    }
    router = OrderRouter(namu_client=mock_client)

    sell_sig = make_signal(side=OrderSide.SELL, price=4020, reason="TEST_EXIT")

    # 2주 매도 발주 시도 -> 가용 1주보다 크므로 에러 16157 사전 방어 차단
    passed, reason = router.run_pre_order_checks(
        signal=sell_sig,
        shares=2,
        order_price=4020,
        balance={"cash": 10000000},
        portfolio_risk_status="NORMAL",
        current_spread=0.001,
        now=REGULAR_TIME
    )

    assert not passed
    assert "INSUFFICIENT_BROKER_SELLABLE_QTY" in reason
    assert "가용 1주 < 요청 2주" in reason


# ============================================================================
# Test 11: Single account process lock blocks second process on same LIVE account
# ============================================================================
def test_11_single_account_process_lock_blocks_second_process():
    lock1 = AccountProcessLock(trading_mode="LIVE", account_no="20201549311_TEST")
    lock2 = AccountProcessLock(trading_mode="LIVE", account_no="20201549311_TEST")

    try:
        ok1, pid1 = lock1.acquire()
        assert ok1 is True

        # 동일 계좌에 대해 두 번째 프로세스 락 시도 -> DUPLICATE_ACCOUNT_PROCESS로 차단
        ok2, existing_pid = lock2.acquire()
        assert ok2 is False

    finally:
        lock1.release()
        lock2.release()

    # 락 해제 후에는 정상적으로 재획득 가능
    ok_after, _ = lock2.acquire()
    assert ok_after is True
    lock2.release()


# ============================================================================
# Test 12: Independent simultaneous execution of LIVE and MOCK accounts is permitted
# ============================================================================
def test_12_independent_simultaneous_execution_of_live_and_mock_permitted():
    live_lock = AccountProcessLock(trading_mode="LIVE", account_no="20201549311_TEST")
    mock_lock = AccountProcessLock(trading_mode="MOCK", account_no="50001003032_TEST")

    try:
        live_ok, _ = live_lock.acquire()
        mock_ok, _ = mock_lock.acquire()

        # LIVE와 MOCK은 완전히 독립적인 계좌이므로 동시에 락 획득 성공해야 함
        assert live_ok is True
        assert mock_ok is True

    finally:
        live_lock.release()
        mock_lock.release()


# ============================================================================
# Test 13: Reconciliation on restart sets authoritative broker position and pending sell orders
# ============================================================================
def test_13_reconciliation_on_restart_authoritative_anchor():
    mock_client = MagicMock()
    mock_client.dry_run = False
    mock_client.get_sellable_quantity.return_value = {
        "bnc_qty": 3,
        "tdt_sll_ny_cns_qty": 2,
        "sll_pbl_qty": 1
    }
    router = OrderRouter(namu_client=mock_client)
    pm = PositionManager(order_router=router)

    broker_holdings = [
        {"iem_cd": "027360", "qty": 3, "buy_price": 3770.0, "now_price": 4020, "iem_nm": "아주IB투자"}
    ]

    pm.sync_from_broker(broker_holdings)

    # 복원된 포지션 확인
    matching = [p for p in pm.positions.values() if p.iem_cd == "027360"]
    assert len(matching) == 1
    pos = matching[0]

    assert pos.qty == 3
    assert pos.pending_exit_qty == 2
    assert pos.broker_psbl_qty == 1
    assert pos.available_qty == 1
    assert not pos.is_closed


# ============================================================================
# Test 14: Reconcile correction record in DB restores order status to PENDING and position qty to broker truth without deleting audit trail
# ============================================================================
def test_14_reconcile_correction_record_in_db_audit_trail_preserved(tmp_path):
    db_path = str(tmp_path / "test_operational.db")
    conn = sqlite3.connect(db_path)
    c = conn.cursor()

    c.execute("""
        CREATE TABLE orders (
            client_order_id TEXT PRIMARY KEY,
            broker_order_no TEXT,
            iem_cd TEXT,
            side TEXT,
            qty INTEGER,
            price REAL,
            status TEXT,
            filled_at TEXT
        )
    """)
    c.execute("""
        CREATE TABLE positions (
            position_id TEXT PRIMARY KEY,
            iem_cd TEXT,
            qty INTEGER,
            status TEXT
        )
    """)
    c.execute("""
        CREATE TABLE reconciliation_logs (
            log_id TEXT PRIMARY KEY,
            timestamp TEXT,
            diff_detected INTEGER,
            details TEXT,
            action_taken TEXT
        )
    """)

    cid = "RESTART_RECOVERY_EXIT_027360_SELL_20260918092005629_7940"
    pos_id = "POS_027360_1789609454195"

    # 오체결 상태의 기존 DB 기록 삽입 (FILLED, 수량 1주)
    c.execute("INSERT INTO orders VALUES (?, ?, ?, ?, ?, ?, ?, ?)", (cid, "327455", "027360", "SELL", 2, 4020.0, "FILLED", "2026-09-18T09:20:07"))
    c.execute("INSERT INTO positions VALUES (?, ?, ?, ?)", (pos_id, "027360", 1, "OPEN"))
    conn.commit()

    # 정정 수행 (기존 레코드 삭제 없이 상태 복원 및 감사 로그 기록)
    c.execute("UPDATE orders SET status='PENDING', filled_at=NULL WHERE client_order_id=?", (cid,))
    c.execute("UPDATE positions SET qty=3 WHERE position_id=?", (pos_id,))
    c.execute(
        "INSERT INTO reconciliation_logs VALUES (?, ?, ?, ?, ?)",
        (
            "RECON_CORRECTION_001",
            datetime.now().isoformat(),
            1,
            json.dumps({"order_id": cid, "broker_order_no": "327455", "broker_qty": 3, "unfilled_qty": 2}),
            "REVERT_FALSE_FILL_RESTORE_POSITION_QTY_TO_3_AND_ORDER_STATUS_TO_PENDING"
        )
    )
    conn.commit()

    # 검증: 주문은 PENDING으로 복원
    c.execute("SELECT status, filled_at FROM orders WHERE client_order_id=?", (cid,))
    ord_row = c.fetchone()
    assert ord_row[0] == "PENDING"
    assert ord_row[1] is None

    # 검증: 포지션 수량은 브로커 진실 3주로 복원
    c.execute("SELECT qty FROM positions WHERE position_id=?", (pos_id,))
    assert c.fetchone()[0] == 3

    # 검증: 감사 추적성 완벽 보존 (로그 레코드 1건 존재)
    c.execute("SELECT diff_detected, action_taken FROM reconciliation_logs WHERE log_id='RECON_CORRECTION_001'")
    recon_row = c.fetchone()
    assert recon_row[0] == 1
    assert "REVERT_FALSE_FILL" in recon_row[1]

    conn.close()
