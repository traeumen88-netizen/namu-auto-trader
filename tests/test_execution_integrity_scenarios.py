# -*- coding: utf-8 -*-
"""[Item 16: Additional Validation Scenarios 1 ~ 5]
Execution Integrity & Order State Machine Comprehensive Scenario Tests:
1. 주문 전송 직후 강제종료 → 재기동 복구 테스트 (Kill right after order sent/ACK -> restart recovery)
2. 부분체결 → 재기동 복구 테스트 (Partial fill -> restart recovery)
3. CANCELED / REJECTED / EXPIRED 상태 불변조건 검증
4. 동일 체결 이벤트 중복 처리 방지 테스트 (Duplicate Execution Idempotency)
5. 브로커 API 장애/지연 및 장애 주입 테스트 (Fault Injection & Delayed Recovery)

All tests are completely isolated using mocks. No live broker accounts or real orders are affected.
Trading strategies, BUY gates, entry filters, and scoring logic are strictly untouched.
"""

import os
import sys
import time
import pytest
from datetime import datetime, timedelta
from unittest.mock import MagicMock, patch

from core.models import Position, TradeSignal, Order, OrderSide, OrderType, OrderStatus, TimeHorizon
from execution.order_router import OrderRouter
from execution.position_manager import PositionManager

REGULAR_TIME = datetime(2026, 9, 18, 10, 30, 0)


def make_signal(side=OrderSide.SELL, shares=2, price=4020, reason="TEST_EXIT_REASON", iem_cd="027360"):
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


def make_order(client_order_id="ORD_TEST_001", broker_order_no="327455", side=OrderSide.SELL, qty=2, price=4020, status=OrderStatus.ORDER_ACK, iem_cd="027360"):
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
# Scenario 1: 주문 전송 직후 강제종료 → 재기동 복구 테스트
# ============================================================================
def test_scenario_1_kill_after_order_sent_restart_recovery():
    """
    주문이 브로커에 정상 접수(ACK)된 직후 프로세스가 비정상 종료되는 상황 시뮬레이션.
    재기동 시 브로커 실제 상태(미체결)를 기준으로 내부 상태가 복구되는지 검증.
    불변조건: ACK만으로 FILLED 처리되거나 PositionManager 수량이 차감되면 안 됨.
    """
    broker_order_no = "327455"
    client_order_id = "RESTART_RECOVERY_EXIT_027360_SELL_TEST_001"

    # [1단계: 종료 전 상태 시뮬레이션]
    mock_client_pre = MagicMock()
    mock_client_pre.dry_run = False
    mock_client_pre.act_no = "20201549311"
    mock_client_pre.sell_limit.return_value = {"Output_0": {"mkt_orr_no": broker_order_no}}
    mock_client_pre.get_sellable_quantity.return_value = {
        "bnc_qty": 3,
        "tdt_sll_ny_cns_qty": 0,
        "sll_pbl_qty": 3
    }

    router_pre = OrderRouter(namu_client=mock_client_pre)
    pm_pre = PositionManager(order_router=router_pre)
    pos_pre = make_position(qty=3, pending_exit_qty=0)
    pm_pre.positions[pos_pre.position_id] = pos_pre

    sig = make_signal(shares=2, price=4020)
    order_pre = router_pre.submit_order(
        signal=sig,
        shares=2,
        order_type=OrderType.LIMIT,
        order_price=4020,
        now=REGULAR_TIME
    )

    # ACK 시점의 검증 (종료 전)
    assert order_pre is not None
    assert order_pre.status == OrderStatus.ORDER_ACK
    assert order_pre.filled_qty == 0
    assert order_pre.remaining_qty == 2
    assert order_pre.broker_order_no == broker_order_no
    assert order_pre.client_order_id in router_pre.pending_orders

    # PositionManager 수량은 보존되어야 함
    pos_pre.pending_exit_qty = 2
    assert pos_pre.qty == 3  # 절대 1로 차감되면 안 됨!
    assert pos_pre.available_qty == 1

    # [2단계: 프로세스 강제종료 및 재기동 시뮬레이션]
    # 메모리 인스턴스 전면 폐기
    del router_pre
    del pm_pre
    del pos_pre

    # [3단계: 재기동 후 브로커 Ground Truth 기반 복구]
    # 브로커 실제 상태: 2주 매도 미체결 대기 중, 총 보유 3주, 체결 0주
    mock_client_restart = MagicMock()
    mock_client_restart.dry_run = False
    mock_client_restart.act_no = "20201549311"
    mock_client_restart.get_sellable_quantity.return_value = {
        "bnc_qty": 3,
        "tdt_sll_ny_cns_qty": 2,
        "sll_pbl_qty": 1
    }
    mock_client_restart.get_daily_order_execution.return_value = [
        {
            "orr_no": broker_order_no,
            "itg_orr_no": broker_order_no,
            "tot_cns_qty": "0",
            "cns_qty": "0",
            "can_qty": "0",
            "cns_avg_uit_pr": "4020.0"
        }
    ]

    router_post = OrderRouter(namu_client=mock_client_restart)
    pm_post = PositionManager(order_router=router_post)

    # 재기동 시 영구 저장소에서 미체결 주문 복원
    restored_order = make_order(
        client_order_id=client_order_id,
        broker_order_no=broker_order_no,
        qty=2,
        price=4020,
        status=OrderStatus.ORDER_ACK
    )
    router_post.pending_orders[client_order_id] = restored_order
    router_post.order_registry[client_order_id] = restored_order

    # 브로커 잔고 및 체결 동기화
    broker_holdings = [
        {"iem_cd": "027360", "qty": 3, "buy_price": 3770.0, "now_price": 4020, "iem_nm": "아주IB투자"}
    ]
    pm_post.sync_from_broker(broker_holdings)
    recon_result = router_post.reconcile_orders(mock_client_restart)

    # [4단계: 최종 정합성 검증]
    matching_positions = [p for p in pm_post.positions.values() if p.iem_cd == "027360"]
    assert len(matching_positions) == 1
    recovered_pos = matching_positions[0]

    # Invariant 검증
    assert restored_order.status != OrderStatus.FILLED
    assert restored_order.status in (OrderStatus.ORDER_ACK, OrderStatus.PENDING)
    assert restored_order.filled_qty == 0
    assert restored_order.remaining_qty == 2
    assert recovered_pos.qty == 3  # 브로커 실보유 3주 일치
    assert recovered_pos.pending_exit_qty == 2  # 브로커 미체결 매도 2주 일치
    assert recovered_pos.available_qty == 1  # 브로커 매도가능 1주 일치
    assert not recovered_pos.is_closed
    assert recon_result["filled_count"] == 0


# ============================================================================
# Scenario 2: 부분체결 → 재기동 복구 테스트
# ============================================================================
def test_scenario_2_partial_fill_restart_recovery():
    """
    2주 매도 주문 중 1주만 부분체결(PARTIAL_FILL)된 상태에서 프로세스 종료 후 재기동.
    브로커 Ground Truth: 체결 1주, 미체결 1주, 잔고 2주.
    검증:
    - 브로커 체결수량 = 1
    - 내부 filled_qty = 1, remaining_qty = 1, status = PARTIAL_FILL
    - PositionManager 보유수량 = 2주, pending_exit_qty = 1주
    - 중복 체결 처리 및 중복 텔레그램 알림 없음
    """
    broker_order_no = "B_PARTIAL_327456"
    client_order_id = "EXIT_PARTIAL_TEST_002"

    # [1단계: 종료 전 1주 부분체결 발생 시뮬레이션]
    mock_client_pre = MagicMock()
    mock_client_pre.dry_run = False
    mock_client_pre.get_daily_order_execution.return_value = [
        {
            "orr_no": broker_order_no,
            "itg_orr_no": broker_order_no,
            "tot_cns_qty": "1",
            "cns_qty": "1",
            "can_qty": "0",
            "cns_avg_uit_pr": "4020.0"
        }
    ]

    router_pre = OrderRouter(namu_client=mock_client_pre)
    pm_pre = PositionManager(order_router=router_pre)
    pos_pre = make_position(qty=3, pending_exit_qty=2)
    pm_pre.positions[pos_pre.position_id] = pos_pre

    order_pre = make_order(
        client_order_id=client_order_id,
        broker_order_no=broker_order_no,
        qty=2,
        price=4020,
        status=OrderStatus.ORDER_ACK
    )
    router_pre.pending_orders[client_order_id] = order_pre
    router_pre.order_registry[client_order_id] = order_pre

    with patch.object(router_pre, "_send_fill_telegram") as mock_tg_pre:
        router_pre.reconcile_orders(mock_client_pre)
        assert mock_tg_pre.call_count == 1  # 1주 체결 알림 1회 발송

    assert order_pre.status == OrderStatus.PARTIAL_FILL
    assert order_pre.filled_qty == 1
    assert order_pre.remaining_qty == 1
    assert pos_pre.qty == 2
    assert pos_pre.pending_exit_qty == 1

    # [2단계: 프로세스 강제 종료 및 재기동]
    del router_pre
    del pm_pre
    del pos_pre

    # [3단계: 재기동 복구]
    mock_client_restart = MagicMock()
    mock_client_restart.dry_run = False
    mock_client_restart.get_sellable_quantity.return_value = {
        "bnc_qty": 2,
        "tdt_sll_ny_cns_qty": 1,
        "sll_pbl_qty": 1
    }
    mock_client_restart.get_daily_order_execution.return_value = [
        {
            "orr_no": broker_order_no,
            "itg_orr_no": broker_order_no,
            "tot_cns_qty": "1",
            "cns_qty": "1",
            "can_qty": "0",
            "cns_avg_uit_pr": "4020.0"
        }
    ]

    router_post = OrderRouter(namu_client=mock_client_restart)
    pm_post = PositionManager(order_router=router_post)

    # 복원된 주문 상태 (이전 상태 1주 체결된 상태로 로드)
    restored_order = make_order(
        client_order_id=client_order_id,
        broker_order_no=broker_order_no,
        qty=2,
        price=4020,
        status=OrderStatus.PARTIAL_FILL
    )
    restored_order.filled_qty = 1
    restored_order.remaining_qty = 1
    router_post.pending_orders[client_order_id] = restored_order
    router_post.order_registry[client_order_id] = restored_order

    broker_holdings = [
        {"iem_cd": "027360", "qty": 2, "buy_price": 3770.0, "now_price": 4020, "iem_nm": "아주IB투자"}
    ]
    pm_post.sync_from_broker(broker_holdings)

    with patch.object(router_post, "_send_fill_telegram") as mock_tg_post:
        recon_result = router_post.reconcile_orders(mock_client_restart)
        # 재기동 대사 시 추가 체결량이 없으므로 텔레그램 재발송 금지!
        assert mock_tg_post.call_count == 0

    matching_positions = [p for p in pm_post.positions.values() if p.iem_cd == "027360"]
    assert len(matching_positions) == 1
    pos_post = matching_positions[0]

    # [4단계: 최종 불변조건 검증]
    assert restored_order.status == OrderStatus.PARTIAL_FILL
    assert restored_order.filled_qty == 1
    assert restored_order.remaining_qty == 1
    assert pos_post.qty == 2  # 브로커 실잔고 2주와 완전 일치
    assert pos_post.pending_exit_qty == 1  # 잔여 미체결 매도 1주
    assert pos_post.available_qty == 1
    assert not pos_post.is_closed


# ============================================================================
# Scenario 3: CANCELED / REJECTED / EXPIRED 상태 검증
# ============================================================================
def test_scenario_3_canceled_rejected_expired_invariants():
    """
    CANCELED, REJECTED, EXPIRED 각 상태를 독립 시뮬레이션하여:
    - FILLED로 전이되지 않음
    - PositionManager 보유수량(pos.qty) 차감 금지
    - pending_exit_qty 정상 해제
    - 잘못된 텔레그램 체결 알림 발송 금지
    - pending_orders에서 정상 제거
    """
    mock_client = MagicMock()
    mock_client.dry_run = False
    router = OrderRouter(namu_client=mock_client)
    pm = PositionManager(order_router=router)

    # ------------------------------------------------------------------------
    # Case 3-A: CANCELED (미체결 주문 취소)
    # ------------------------------------------------------------------------
    pos_a = make_position(qty=3, pending_exit_qty=2, iem_cd="005930")
    pm.positions[pos_a.position_id] = pos_a

    order_a = make_order(client_order_id="ORD_CANCEL_001", broker_order_no="B_CAN_001", qty=2, iem_cd="005930")
    pos_a.active_exit_order_id = order_a.client_order_id
    router.pending_orders[order_a.client_order_id] = order_a

    with patch.object(router, "_send_fill_telegram") as mock_tg_cancel:
        res = router.cancel_order(order_a.client_order_id, reason="USER_CANCELLED")
        assert res is True
        assert mock_tg_cancel.call_count == 0

    assert order_a.status == OrderStatus.CANCELLED
    assert order_a.client_order_id not in router.pending_orders
    assert pos_a.qty == 3  # 수량 차감 없음!
    assert pos_a.pending_exit_qty == 0  # 대기 수량 완전 해제!
    assert pos_a.available_qty == 3
    assert not pos_a.is_closed

    # ------------------------------------------------------------------------
    # Case 3-B: REJECTED (브로커 주문 거부)
    # ------------------------------------------------------------------------
    pos_b = make_position(qty=3, pending_exit_qty=0, iem_cd="000660")
    pm.positions[pos_b.position_id] = pos_b

    # 브로커가 16157 에러를 반환하는 상황
    mock_client.sell_limit.side_effect = Exception("[16157] 주문가능수량이 부족합니다.")
    mock_client.get_sellable_quantity.return_value = {"bnc_qty": 3, "tdt_sll_ny_cns_qty": 0, "sll_pbl_qty": 3}

    sig_b = make_signal(shares=2, price=180000, iem_cd="000660")
    with patch.object(router, "_send_fill_telegram") as mock_tg_reject:
        order_b = router.submit_order(
            signal=sig_b,
            shares=2,
            order_type=OrderType.LIMIT,
            order_price=180000,
            now=REGULAR_TIME
        )
        assert mock_tg_reject.call_count == 0

    assert order_b is None
    saved_order_b = [o for o in router.order_registry.values() if o.iem_cd == "000660"]
    assert len(saved_order_b) == 1
    assert saved_order_b[0].status == OrderStatus.REJECTED
    assert saved_order_b[0].client_order_id not in router.pending_orders
    assert pos_b.qty == 3  # 수량 보존!
    assert pos_b.pending_exit_qty == 0
    assert not pos_b.is_closed
    assert any("16157" in r for r in sig_b.rejection_reasons)

    # ------------------------------------------------------------------------
    # Case 3-C: EXPIRED (주문 만료)
    # ------------------------------------------------------------------------
    pos_c = make_position(qty=3, pending_exit_qty=2, iem_cd="035720")
    pm.positions[pos_c.position_id] = pos_c

    order_c = make_order(client_order_id="ORD_EXPIRE_001", broker_order_no="B_EXP_001", qty=2, iem_cd="035720")
    pos_c.active_exit_order_id = order_c.client_order_id
    router.pending_orders[order_c.client_order_id] = order_c

    order_c.status = OrderStatus.EXPIRED
    if order_c.client_order_id in router.pending_orders:
        del router.pending_orders[order_c.client_order_id]
    pm.on_order_cancel_or_reject(order_c, reason="SESSION_EXPIRED")

    assert order_c.status == OrderStatus.EXPIRED
    assert order_c.client_order_id not in router.pending_orders
    assert pos_c.qty == 3  # 수량 차감 없음!
    assert pos_c.pending_exit_qty == 0  # 대기 수량 완전 해제!
    assert not pos_c.is_closed


# ============================================================================
# Scenario 4: 동일 체결 이벤트 중복 처리 방지 테스트 (Duplicate Execution Idempotency)
# ============================================================================
def test_scenario_4_duplicate_execution_idempotency():
    """
    브로커 체결조회 API가 동일한 체결내역을 여러 번(N회) 연속 반환하는 상황 시뮬레이션.
    - 1차 조회: 1주 체결
    - 2차 조회: 동일한 1주 체결내역 재조회
    - 3차 조회: 동일한 1주 체결내역 재조회
    - 4차 조회: 2주 전량 체결
    - 5차 조회: 2주 전량 체결내역 재조회
    검증:
    - filled_qty가 중복 증가하지 않음
    - PositionManager.qty가 중복 차감되지 않음
    - Telegram 체결 알림이 정확히 신규 체결 수량만큼만 발송됨
    """
    broker_order_no = "B_DUP_TEST_001"
    client_order_id = "ORD_DUP_TEST_001"

    mock_client = MagicMock()
    mock_client.dry_run = False
    router = OrderRouter(namu_client=mock_client)
    pm = PositionManager(order_router=router)

    pos = make_position(qty=2, pending_exit_qty=2, iem_cd="027360")
    pm.positions[pos.position_id] = pos

    order = make_order(
        client_order_id=client_order_id,
        broker_order_no=broker_order_no,
        qty=2,
        price=4020,
        status=OrderStatus.ORDER_ACK
    )
    router.pending_orders[client_order_id] = order
    router.order_registry[client_order_id] = order

    with patch.object(router, "_send_fill_telegram") as mock_tg:
        # 1차 조회: 1주 체결
        mock_client.get_daily_order_execution.return_value = [
            {"orr_no": broker_order_no, "tot_cns_qty": "1", "cns_qty": "1", "can_qty": "0", "cns_avg_uit_pr": "4020.0"}
        ]
        router.reconcile_orders(mock_client)

        assert order.filled_qty == 1
        assert order.remaining_qty == 1
        assert pos.qty == 1
        assert pos.pending_exit_qty == 1
        assert mock_tg.call_count == 1  # 1회 발송

        # 2차 조회: 동일한 1주 체결 재수신 (중복)
        router.reconcile_orders(mock_client)
        assert order.filled_qty == 1  # 중복 증가 없음!
        assert order.remaining_qty == 1
        assert pos.qty == 1  # 중복 차감 없음!
        assert pos.pending_exit_qty == 1
        assert mock_tg.call_count == 1  # 발송 횟수 유지!

        # 3차 조회: 동일한 1주 체결 재수신 (중복)
        router.reconcile_orders(mock_client)
        assert order.filled_qty == 1
        assert pos.qty == 1
        assert mock_tg.call_count == 1

        # 4차 조회: 2주차 추가 체결 (누적 2주 전량 체결)
        mock_client.get_daily_order_execution.return_value = [
            {"orr_no": broker_order_no, "tot_cns_qty": "2", "cns_qty": "2", "can_qty": "0", "cns_avg_uit_pr": "4020.0"}
        ]
        router.reconcile_orders(mock_client)
        assert order.filled_qty == 2
        assert order.remaining_qty == 0
        assert order.status == OrderStatus.FILLED
        assert pos.qty == 0
        assert pos.is_closed is True
        assert mock_tg.call_count == 2  # 2주차 신규 체결분 1회 추가 발송

        # 5차 조회: 이미 FILLED 된 주문에 대해 동일 체결내역 재수신 (중복)
        router.reconcile_orders(mock_client)
        assert order.filled_qty == 2
        assert pos.qty == 0
        assert mock_tg.call_count == 2  # 추가 발송 없음


# ============================================================================
# Scenario 5: 브로커 API 장애/지연 및 장애 주입 테스트
# ============================================================================
def test_scenario_5_broker_api_fault_injection_and_recovery():
    """
    브로커 API 장애 상황 주입:
    A. 주문조회 API Timeout/ConnectionError
    B. 체결조회 API HTTP 500 / 빈 응답
    C. 잔고조회 API 오류
    D. API 응답 지연 (High Latency)
    E. 장애 중 프로세스 재기동 후 브로커 정상화 시 수렴 복구
    기본 원칙: '확인되지 않은 체결은 체결로 간주하지 않는다.'
    """
    broker_order_no = "B_FAULT_001"
    client_order_id = "ORD_FAULT_001"

    mock_client = MagicMock()
    mock_client.dry_run = False
    router = OrderRouter(namu_client=mock_client)
    pm = PositionManager(order_router=router)

    pos = make_position(qty=3, pending_exit_qty=2, iem_cd="027360")
    pm.positions[pos.position_id] = pos

    order = make_order(
        client_order_id=client_order_id,
        broker_order_no=broker_order_no,
        qty=2,
        price=4020,
        status=OrderStatus.ORDER_ACK
    )
    router.pending_orders[client_order_id] = order
    router.order_registry[client_order_id] = order

    # ------------------------------------------------------------------------
    # A. 주문/체결조회 API 일시 장애 (Timeout / ConnectionError)
    # ------------------------------------------------------------------------
    mock_client.get_daily_order_execution.side_effect = TimeoutError("Broker connection timed out (3000ms)")

    recon_res_a = router.reconcile_orders(mock_client)
    # 장애 발생 시 주문을 FILLED로 추정하거나 취소하지 않고 안전하게 보존
    assert order.status == OrderStatus.ORDER_ACK
    assert order.filled_qty == 0
    assert order.remaining_qty == 2
    assert pos.qty == 3
    assert client_order_id in router.pending_orders
    assert recon_res_a["filled_count"] == 0

    # ------------------------------------------------------------------------
    # B. 체결조회 API HTTP 500 오류
    # ------------------------------------------------------------------------
    mock_client.get_daily_order_execution.side_effect = RuntimeError("HTTP 500 Internal Server Error from broker gateway")

    recon_res_b = router.reconcile_orders(mock_client)
    assert order.status == OrderStatus.ORDER_ACK
    assert order.filled_qty == 0
    assert pos.qty == 3
    assert client_order_id in router.pending_orders

    # ------------------------------------------------------------------------
    # C. 잔고조회 API 오류
    # ------------------------------------------------------------------------
    mock_client.get_sellable_quantity.side_effect = Exception("HTTP 503 Service Unavailable")
    # 잔고 조회 실패 시 기존 포지션을 임의로 0으로 만들거나 삭제하지 않음
    pm.sync_from_broker([{"iem_cd": "027360", "qty": 3, "buy_price": 3770.0, "iem_nm": "아주IB투자"}])
    assert pos.qty == 3

    # ------------------------------------------------------------------------
    # D. API 지연 (Simulated Latency)
    # ------------------------------------------------------------------------
    def delayed_execution(*args, **kwargs):
        time.sleep(0.05)  # 짧은 지연 시뮬레이션
        return [{"orr_no": broker_order_no, "tot_cns_qty": "1", "cns_qty": "1", "can_qty": "0", "cns_avg_uit_pr": "4020.0"}]

    mock_client.get_daily_order_execution.side_effect = delayed_execution
    router.reconcile_orders(mock_client)
    assert order.status == OrderStatus.PARTIAL_FILL
    assert order.filled_qty == 1
    assert pos.qty == 2

    # ------------------------------------------------------------------------
    # E. 장애 중 프로세스 종료 후 브로커 정상화 복구
    # ------------------------------------------------------------------------
    del router
    del pm
    del pos

    # 새 프로세스 기동
    mock_client_recovered = MagicMock()
    mock_client_recovered.dry_run = False
    mock_client_recovered.get_sellable_quantity.return_value = {
        "bnc_qty": 1,
        "tdt_sll_ny_cns_qty": 0,
        "sll_pbl_qty": 1
    }
    # 브로커 장애 해소 후 최종 상태: 2주 전량 체결 완료
    mock_client_recovered.get_daily_order_execution.return_value = [
        {"orr_no": broker_order_no, "tot_cns_qty": "2", "cns_qty": "2", "can_qty": "0", "cns_avg_uit_pr": "4020.0"}
    ]

    router_rec = OrderRouter(namu_client=mock_client_recovered)
    pm_rec = PositionManager(order_router=router_rec)

    # 1주 체결 상태였던 주문 복원
    recovered_order = make_order(
        client_order_id=client_order_id,
        broker_order_no=broker_order_no,
        qty=2,
        price=4020,
        status=OrderStatus.PARTIAL_FILL
    )
    recovered_order.filled_qty = 1
    recovered_order.remaining_qty = 1
    router_rec.pending_orders[client_order_id] = recovered_order
    router_rec.order_registry[client_order_id] = recovered_order

    recovered_pos = make_position(qty=2, pending_exit_qty=1, iem_cd="027360")
    pm_rec.positions[recovered_pos.position_id] = recovered_pos

    # 브로커 정상화 후 재조회 대사 실행
    router_rec.reconcile_orders(mock_client_recovered)
    pm_rec.sync_from_broker([{"iem_cd": "027360", "qty": 1, "buy_price": 3770.0, "iem_nm": "아주IB투자"}])

    # 최종 상태가 브로커 Ground Truth(총 3주 중 2주 매도 완료 -> 잔여 1주)로 수렴 확인
    assert recovered_order.status == OrderStatus.FILLED
    assert recovered_order.filled_qty == 2
    assert recovered_order.remaining_qty == 0
    assert client_order_id not in router_rec.pending_orders
    assert recovered_pos.qty == 1  # 3 - 2 = 1주
    assert recovered_pos.pending_exit_qty == 0  # 미체결 0주
    assert recovered_pos.available_qty == 1
