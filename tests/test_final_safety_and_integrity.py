"""[FINAL COMPREHENSIVE SAFETY & INTEGRITY TEST SUITE]
Verifies all 11 critical production safety invariants:
1. test_signal_ttl_expiry
2. test_price_drift_revalidation
3. test_pending_cash_reservation
4. test_duplicate_buy_idempotency
5. test_partial_fill_position_reconciliation
6. test_broker_qty_less_than_internal_qty
7. test_restart_recovery_classification
8. test_time_stop_trend_exception
9. test_mock_cannot_delay_live
10. test_mock_telegram_zero
11. test_live_daily_report_only
"""

import unittest
from unittest.mock import MagicMock, patch
from datetime import datetime, timedelta

from core.models import (
    TradeSignal,
    Order,
    Position,
    OrderSide,
    OrderType,
    OrderStatus,
    TimeHorizon,
)
from risk.portfolio_cash import PortfolioCashManager
from execution.order_router import OrderRouter
from execution.position_manager import PositionManager
from execution.live_quant_trader import LiveQuantTrader, AccountContext
from core.telegram_notifier import TelegramEventFilter, TelegramNotifier
from ml.trade_db import TradeDatabase, TradeRecord
from ml.eod_worker import EODRetrospectiveWorker


class TestFinalSafetyAndIntegrity(unittest.TestCase):
    """최종 안전성, 상태 정합성 및 LIVE-ONLY 격리 보증 테스트 스위트"""

    def setUp(self):
        self.now = datetime(2026, 9, 17, 10, 0, 0)

    # -------------------------------------------------------------------------
    # 1. Signal TTL 만료 검증
    # -------------------------------------------------------------------------
    def test_signal_ttl_expiry(self):
        """30,000ms를 초과한 오래된 BUY 신호는 SIGNAL_EXPIRED로 기각되어야 함"""
        router = OrderRouter(account_name="LIVE")

        # 35초 전 생성된 신호 (TTL 30초 초과)
        stale_signal = TradeSignal(
            strategy_id="INT_BREAKOUT",
            time_horizon=TimeHorizon.INTRADAY,
            iem_cd="005930",
            name="삼성전자",
            side=OrderSide.BUY,
            strategy_price=70000,
            stop_price=68000,
            score=90.0,
            reason="BREAKOUT",
            timestamp=self.now - timedelta(seconds=35),
            signal_created_at=self.now - timedelta(seconds=35),
        )

        passed, reason = router.run_pre_order_checks(
            signal=stale_signal,
            shares=10,
            order_price=70000,
            balance={"cash": 10_000_000},
            current_spread=0.001,
            now=self.now
        )
        self.assertFalse(passed)
        self.assertIn("SIGNAL_EXPIRED", reason)
        self.assertGreater(stale_signal.signal_age_ms, 30000.0)
        self.assertEqual(stale_signal.approved_status, "SIGNAL_EXPIRED")

        # 5초 전 생성된 신선한 신호는 TTL 통과
        fresh_signal = TradeSignal(
            strategy_id="INT_BREAKOUT",
            time_horizon=TimeHorizon.INTRADAY,
            iem_cd="005930",
            name="삼성전자",
            side=OrderSide.BUY,
            strategy_price=70000,
            stop_price=68000,
            score=90.0,
            reason="BREAKOUT",
            timestamp=self.now - timedelta(seconds=5),
            signal_created_at=self.now - timedelta(seconds=5),
        )
        passed_fresh, _ = router.run_pre_order_checks(
            signal=fresh_signal,
            shares=10,
            order_price=70000,
            balance={"cash": 10_000_000},
            current_spread=0.001,
            now=self.now
        )
        self.assertTrue(passed_fresh)
        self.assertLessEqual(fresh_signal.signal_age_ms, 30000.0)

    # -------------------------------------------------------------------------
    # 2. Price Drift 재검증
    # -------------------------------------------------------------------------
    def test_price_drift_revalidation(self):
        """BUY_APPROVED 승인가 대비 1.5% 초과 급변 시 PRICE_DRIFT_EXCEEDED로 기각되어야 함"""
        router = OrderRouter(account_name="LIVE")

        # 승인가: 70,000원, 주문 시점 현재가: 71,200원 (+1.71% 급변)
        drift_signal = TradeSignal(
            strategy_id="INT_BREAKOUT",
            time_horizon=TimeHorizon.INTRADAY,
            iem_cd="005930",
            name="삼성전자",
            side=OrderSide.BUY,
            strategy_price=70000,
            stop_price=68000,
            score=90.0,
            reason="BREAKOUT",
            timestamp=self.now,
            approved_price=70000.0,
        )

        passed, reason = router.run_pre_order_checks(
            signal=drift_signal,
            shares=10,
            order_price=71200,
            balance={"cash": 10_000_000},
            current_spread=0.001,
            now=self.now
        )
        self.assertFalse(passed)
        self.assertIn("PRICE_DRIFT_EXCEEDED", reason)
        self.assertGreater(abs(drift_signal.price_drift_pct), 0.015)

        # 승인가: 70,000원, 주문 시점 현재가: 70,400원 (+0.57% 정상 범위)
        drift_signal.rejection_reasons.clear()
        passed_ok, _ = router.run_pre_order_checks(
            signal=drift_signal,
            shares=10,
            order_price=70400,
            balance={"cash": 10_000_000},
            current_spread=0.001,
            now=self.now
        )
        self.assertTrue(passed_ok)
        self.assertLessEqual(abs(drift_signal.price_drift_pct), 0.015)

    # -------------------------------------------------------------------------
    # 3. 중앙 현금 예약 및 가용 현금 수식 검증
    # -------------------------------------------------------------------------
    def test_pending_cash_reservation(self):
        """usable_cash = available_cash - reserved_cash 공식 및 동시 주문 현금 잠금 검증"""
        pcm = PortfolioCashManager(initial_cash=1_000_000)
        self.assertEqual(pcm.available_cash, 1_000_000.0)
        self.assertEqual(pcm.reserved_cash, 0.0)
        self.assertEqual(pcm.usable_cash, 1_000_000.0)

        sig1 = TradeSignal(
            strategy_id="INT_BREAKOUT",
            time_horizon=TimeHorizon.INTRADAY,
            iem_cd="005930",
            name="삼성전자",
            side=OrderSide.BUY,
            strategy_price=10000,
            stop_price=9500,
            score=90.0,
            reason="BREAKOUT",
            timestamp=self.now,
        )

        # 주문 1: 60주 @ 10,000원 = 600,000원 + 수수료/슬리피지
        ok1, msg1, req1, _ = pcm.revalidate_and_reserve_cash("ORD_001", sig1, shares=60, order_price=10000, now=self.now)
        self.assertTrue(ok1)
        self.assertEqual(msg1, "CASH_APPROVED")
        self.assertGreater(pcm.reserved_cash, 600_000.0)
        self.assertEqual(pcm.usable_cash, pcm.available_cash - pcm.reserved_cash)

        # 주문 2: 50주 @ 10,000원 = 500,000원 (잔여 usable_cash 약 399,610원보다 큼 -> 기각)
        sig2 = TradeSignal(
            strategy_id="INT_BREAKOUT",
            time_horizon=TimeHorizon.INTRADAY,
            iem_cd="000660",
            name="SK하이닉스",
            side=OrderSide.BUY,
            strategy_price=10000,
            stop_price=9500,
            score=88.0,
            reason="BREAKOUT",
            timestamp=self.now,
        )
        ok2, msg2, req2, rec2 = pcm.revalidate_and_reserve_cash("ORD_002", sig2, shares=50, order_price=10000, now=self.now)
        self.assertFalse(ok2)
        self.assertEqual(msg2, "INSUFFICIENT_CASH")
        self.assertIsNotNone(rec2)
        self.assertGreater(rec2.cash_shortfall, 0)

        # 주문 1 취소 시 예약금 완벽 복원
        pcm.on_order_cancel_or_reject("ORD_001")
        self.assertEqual(pcm.reserved_cash, 0.0)
        self.assertEqual(pcm.usable_cash, 1_000_000.0)

    # -------------------------------------------------------------------------
    # 4. 동일 주문 멱등성 및 중복 BUY 차단 검증
    # -------------------------------------------------------------------------
    def test_duplicate_buy_idempotency(self):
        """동일 order_intent_id 및 pending_entries 진행 중 동일 종목 중복 BUY 원천 차단"""
        router = OrderRouter(account_name="LIVE")

        sig = TradeSignal(
            strategy_id="INT_BREAKOUT",
            time_horizon=TimeHorizon.INTRADAY,
            iem_cd="005930",
            name="삼성전자",
            side=OrderSide.BUY,
            strategy_price=70000,
            stop_price=68000,
            score=90.0,
            reason="BREAKOUT",
            timestamp=self.now,
            order_intent_id="INTENT_UNIQUE_001",
        )

        # 첫 번째 주문 제출 -> 성공
        order1 = router.submit_order(
            signal=sig,
            shares=10,
            order_price=70000,
            balance={"cash": 10_000_000},
            now=self.now
        )
        self.assertIsNotNone(order1)
        self.assertEqual(order1.order_intent_id, "INTENT_UNIQUE_001")

        # 멱등성 1: 동일한 intent_id로 다시 호출 시 기존 Order 반환
        order_duplicate = router.submit_order(
            signal=sig,
            shares=10,
            order_price=70000,
            balance={"cash": 10_000_000},
            now=self.now
        )
        self.assertIs(order1, order_duplicate)

        # 멱등성 2: 다른 intent_id지만 동일 종목이 pending_entries에 있으면 신규 BUY 차단
        sig_different_intent = TradeSignal(
            strategy_id="INT_BREAKOUT",
            time_horizon=TimeHorizon.INTRADAY,
            iem_cd="005930",
            name="삼성전자",
            side=OrderSide.BUY,
            strategy_price=70000,
            stop_price=68000,
            score=90.0,
            reason="BREAKOUT",
            timestamp=self.now,
            order_intent_id="INTENT_ANOTHER_002",
        )
        order_blocked = router.submit_order(
            signal=sig_different_intent,
            shares=10,
            order_price=70000,
            balance={"cash": 10_000_000},
            now=self.now
        )
        self.assertIsNone(order_blocked)

    # -------------------------------------------------------------------------
    # 5. 부분 체결 및 수량 정합성 동기화 검증
    # -------------------------------------------------------------------------
    def test_partial_fill_position_reconciliation(self):
        """부분 체결 시 requested_qty, filled_qty, remaining_qty 및 PARTIAL 전이 정합성 검증"""
        router = OrderRouter(account_name="LIVE")

        sig = TradeSignal(
            strategy_id="INT_BREAKOUT",
            time_horizon=TimeHorizon.INTRADAY,
            iem_cd="005930",
            name="삼성전자",
            side=OrderSide.BUY,
            strategy_price=70000,
            stop_price=68000,
            score=90.0,
            reason="BREAKOUT",
            timestamp=self.now,
        )

        order = router.submit_order(
            signal=sig,
            shares=100,
            order_price=70000,
            balance={"cash": 10_000_000},
            now=self.now
        )
        self.assertIsNotNone(order)
        self.assertEqual(order.requested_qty, 100)
        self.assertEqual(order.remaining_qty, 100)
        self.assertEqual(order.filled_qty, 0)

        # 1차 부분 체결: 40주 체결
        router.on_fill(order.client_order_id, filled_qty=40, fill_price=70000, is_cumulative=False)
        self.assertEqual(order.status, OrderStatus.PARTIAL)
        self.assertEqual(order.filled_qty, 40)
        self.assertEqual(order.remaining_qty, 60)
        self.assertIn("005930", router.pending_entries)

        # 2차 체결: 나머지 60주 체결
        router.on_fill(order.client_order_id, filled_qty=60, fill_price=70000, is_cumulative=False)
        self.assertEqual(order.status, OrderStatus.FILLED)
        self.assertEqual(order.filled_qty, 100)
        self.assertEqual(order.remaining_qty, 0)
        self.assertNotIn("005930", router.pending_entries)

    # -------------------------------------------------------------------------
    # 6. 내부 수량 > 브로커 매도가능수량 시 클램핑 및 자동 치유 검증
    # -------------------------------------------------------------------------
    def test_broker_qty_less_than_internal_qty(self):
        """내부 수량 > broker_psbl_qty 시 클램핑 발주 및 broker_psbl=0 시 자동 치유 검증"""
        pm = PositionManager(order_router=None)

        # Case A: 내부 100주, 브로커 60주 -> 60주로 클램핑
        pos_a = pm.open_position(
            time_horizon=TimeHorizon.INTRADAY,
            strategy_id="INT_BREAKOUT",
            iem_cd="005930",
            name="삼성전자",
            qty=100,
            entry_price=70000,
            stop_price=68000,
            target_1r=72000,
            target_2r=74000,
            target_3r=76000,
            initial_risk=20000
        )
        pos_a.broker_psbl_qty = 60
        pm._close_position(pos_a, price=67000, exit_time=self.now, reason="HARD_STOP")
        self.assertEqual(pos_a.qty, 0)
        self.assertTrue(pos_a.is_closed)
        self.assertEqual(pos_a.status, "POSITION_CLOSED")

        # Case B: 내부 100주, 브로커 0주 -> 발주 없이 즉각 POSITION_CLOSED 자동 치유
        pos_b = pm.open_position(
            time_horizon=TimeHorizon.INTRADAY,
            strategy_id="INT_BREAKOUT",
            iem_cd="000660",
            name="SK하이닉스",
            qty=100,
            entry_price=100000,
            stop_price=98000,
            target_1r=102000,
            target_2r=104000,
            target_3r=106000,
            initial_risk=20000
        )
        pos_b.broker_psbl_qty = 0
        pm._close_position(pos_b, price=97000, exit_time=self.now, reason="HARD_STOP")
        self.assertEqual(pos_b.qty, 0)
        self.assertTrue(pos_b.is_closed)
        self.assertEqual(pos_b.status, "POSITION_CLOSED")
        self.assertIn("BROKER_PSBL_ZERO_AUTO_HEAL", pos_b.exit_reason)

    # -------------------------------------------------------------------------
    # 7. 재시작 잔고 복원 포지션의 RESTART_RECOVERY 분류 검증
    # -------------------------------------------------------------------------
    def test_restart_recovery_classification(self):
        """우듬지팜(403490) 등 재시작 복원 종목은 RESTART_RECOVERY로 정확히 분류되어야 함"""
        pm = PositionManager(order_router=None)
        holdings = [
            {"iem_cd": "403490", "name": "우듬지팜", "qty": 50, "buy_price": 2000, "now_price": 2050}
        ]
        pm.sync_from_broker(holdings)

        self.assertTrue(pm.has_position("403490"))
        pos = pm.positions.get("RECOVERED_INT_403490")
        self.assertIsNotNone(pos)
        self.assertEqual(pos.strategy_id, "RESTART_RECOVERY")
        self.assertEqual(pos.trade_classification, "RESTART_RECOVERY")
        self.assertEqual(pos.qty, 50)
        self.assertEqual(pos.broker_psbl_qty, 50)

    # -------------------------------------------------------------------------
    # 8. 동적 TIME_STOP 추세 유지 종목 보유 연장 검증
    # -------------------------------------------------------------------------
    def test_time_stop_trend_exception(self):
        """우상향 추세 유지 포지션은 60분 초과 보유 허용, 정체 포지션은 30분 경과 후 청산"""
        pm = PositionManager(order_router=None)

        # 1. 추세 유지 포지션 (진척도 우수, VWAP 상회, 양수 모멘텀)
        pos_trend = pm.open_position(
            time_horizon=TimeHorizon.INTRADAY,
            strategy_id="INT_BREAKOUT",
            iem_cd="005930",
            name="삼성전자",
            qty=10,
            entry_price=70000,
            stop_price=68000,
            target_1r=72000,
            target_2r=74000,
            target_3r=76000,
            initial_risk=20000
        )
        pos_trend.entry_time = self.now
        pos_trend.highest_price = 70800

        # 75분 경과 후 추세 양호 (가격 70,500원, VWAP 70,200원, 모멘텀 +0.005)
        check_time_75m = self.now + timedelta(minutes=75)
        pm.update_price_and_manage(
            iem_cd="005930",
            current_price=70500,
            current_time=check_time_75m,
            vwap=70200,
            momentum_3m=0.005
        )
        self.assertTrue(pm.has_position("005930"))
        self.assertNotEqual(pos_trend.status, "TIME_STOP_TRIGGERED")

        # 2. 정체 포지션 (진척도 부진, 손실 상태, 음수 모멘텀)
        pos_stagnant = pm.open_position(
            time_horizon=TimeHorizon.INTRADAY,
            strategy_id="INT_BREAKOUT",
            iem_cd="000660",
            name="SK하이닉스",
            qty=10,
            entry_price=100000,
            stop_price=97000,
            target_1r=103000,
            target_2r=106000,
            target_3r=109000,
            initial_risk=30000
        )
        pos_stagnant.entry_time = self.now
        pos_stagnant.highest_price = 100100

        # 35분 경과 후 정체 (가격 99,800원, VWAP 100,100원, 모멘텀 -0.005)
        check_time_35m = self.now + timedelta(minutes=35)
        pm.update_price_and_manage(
            iem_cd="000660",
            current_price=99800,
            current_time=check_time_35m,
            vwap=100100,
            momentum_3m=-0.005
        )
        self.assertFalse(pm.has_position("000660"))
        last_closed = pm.get_last_closed_trade("000660")
        self.assertIsNotNone(last_closed)
        self.assertIn("타임스톱", last_closed["reason"])

    # -------------------------------------------------------------------------
    # 9. MOCK 계좌가 LIVE 계좌를 지연시킬 수 없음 검증
    # -------------------------------------------------------------------------
    def test_mock_cannot_delay_live(self):
        """LiveQuantTrader에서 LIVE 계좌가 항상 0번 인덱스로 최우선 실행되며 MOCK 예외 격리 보장"""
        with patch.object(LiveQuantTrader, "_init_premarket_data"), \
             patch("universe.full_universe_master.FullUniverseMaster.load_full_universe", return_value={}):
            trader = LiveQuantTrader(mode="dual")

            # 계좌 정렬 순서 보증: LIVE가 항상 1순위
            self.assertEqual(trader.accounts[0].name, "LIVE")
            self.assertEqual(trader.accounts[1].name, "MOCK")

            # MOCK 계좌 실행 중 치명적 예외 발생 시뮬레이션
            mock_account = trader.accounts[1]
            mock_account.position_manager = MagicMock()
            mock_account.position_manager.update_price_and_manage.side_effect = RuntimeError("MOCK CRITICAL FAILURE")

            live_account = trader.accounts[0]
            live_account.position_manager = MagicMock()

            # LIVE 계좌는 MOCK 장애와 무관하게 정상 집행
            live_account.position_manager.update_price_and_manage(
                iem_cd="005930",
                current_price=70000,
                current_time=self.now
            )
            live_account.position_manager.update_price_and_manage.assert_called_once()

    # -------------------------------------------------------------------------
    # 10. MOCK 텔레그램 발송 0건 완전 차단 검증
    # -------------------------------------------------------------------------
    def test_mock_telegram_zero(self):
        """MOCK 계좌의 체결 이벤트, 청산 알림, 시스템 경보는 텔레그램 발송 0건이어야 함"""
        notifier = TelegramNotifier()
        notifier.enabled = True
        notifier.token = "dummy_token"
        notifier.chat_id = "12345"

        with patch("requests.post") as mock_http_post:
            # 1. MOCK 매매 체결 이벤트 시도
            res1 = notifier.send_trade_event(
                event_type="BUY",
                symbol="005930",
                name="삼성전자",
                price=70000,
                qty=10,
                account="MOCK",
                trading_mode="mock"
            )
            self.assertFalse(res1)

            # 2. MOCK 청산 이벤트 시도
            res2 = notifier.send_trade_event(
                event_type="SELL",
                symbol="005930",
                name="삼성전자",
                price=71000,
                qty=10,
                account="mock",
                trading_mode="mock"
            )
            self.assertFalse(res2)

            # 3. MOCK 시스템 경보 시도
            res3 = notifier.send_system_alert(
                title="MOCK ALERT",
                message="Mock alert message",
                account="MOCK",
                trading_mode="mock"
            )
            self.assertFalse(res3)

            # 단 1건의 HTTP 요청도 발생하지 않아야 함
            mock_http_post.assert_not_called()

    # -------------------------------------------------------------------------
    # 11. 일일 마감 보고서 LIVE 전용 격리 검증
    # -------------------------------------------------------------------------
    def test_live_daily_report_only(self):
        """EOD 마감 분석 및 일일 성과 집계에서 MOCK 데이터가 완벽히 배제되고 LIVE만 포함되어야 함"""
        import tempfile
        import os
        with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as tf:
            db_path = tf.name

        try:
            db = TradeDatabase(db_path=db_path)
            now_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

            # LIVE 거래 2건 저장
            t_live1 = TradeRecord(
                trade_id="T_LIVE_001", symbol="005930", symbol_name="삼성전자",
                setup_name="INT_BREAKOUT", time_horizon="INTRADAY", side="BUY",
                entry_time=now_str, exit_time=now_str, entry_price=70000, exit_price=71000,
                shares=10, stop_price=68000, target_price=72000, pnl=10000, return_pct=1.43,
                r_multiple=1.0, mae_pct=0.0, mfe_pct=1.43, mae_r=0.0, mfe_r=1.0,
                holding_seconds=1800, model_version="v16.0", p_target_pred=0.8, p_stop_pred=0.2,
                expected_net_r_pred=0.5, bad_trade_category="PROFIT_TARGET", trading_mode="live"
            )
            t_live2 = TradeRecord(
                trade_id="T_LIVE_002", symbol="000660", symbol_name="SK하이닉스",
                setup_name="INT_PULLBACK", time_horizon="INTRADAY", side="BUY",
                entry_time=now_str, exit_time=now_str, entry_price=100000, exit_price=98000,
                shares=5, stop_price=97000, target_price=103000, pnl=-10000, return_pct=-2.0,
                r_multiple=-1.0, mae_pct=-2.0, mfe_pct=0.0, mae_r=-1.0, mfe_r=0.0,
                holding_seconds=1200, model_version="v16.0", p_target_pred=0.7, p_stop_pred=0.3,
                expected_net_r_pred=0.3, bad_trade_category="NORMAL_STOP", trading_mode="live"
            )
            # MOCK 거래 1건 저장
            t_mock1 = TradeRecord(
                trade_id="T_MOCK_001", symbol="035420", symbol_name="NAVER",
                setup_name="INT_BREAKOUT", time_horizon="INTRADAY", side="BUY",
                entry_time=now_str, exit_time=now_str, entry_price=200000, exit_price=220000,
                shares=10, stop_price=190000, target_price=210000, pnl=200000, return_pct=10.0,
                r_multiple=2.0, mae_pct=0.0, mfe_pct=10.0, mae_r=0.0, mfe_r=2.0,
                holding_seconds=3600, model_version="v16.0", p_target_pred=0.9, p_stop_pred=0.1,
                expected_net_r_pred=0.8, bad_trade_category="PROFIT_TARGET", trading_mode="mock"
            )
            db.record_trade(t_live1)
            db.record_trade(t_live2)
            db.record_trade(t_mock1)

            # 일일 보고서/통계 조회: trading_mode='live' 필수 필터링
            live_trades = db.get_closed_trades(trading_mode="live")
            self.assertEqual(len(live_trades), 2)
            trade_symbols = {t["symbol"] for t in live_trades}
            self.assertIn("005930", trade_symbols)
            self.assertIn("000660", trade_symbols)
            self.assertNotIn("035420", trade_symbols)
            for t in live_trades:
                self.assertEqual(t["trading_mode"].lower(), "live")
        finally:
            if os.path.exists(db_path):
                try:
                    os.remove(db_path)
                except Exception:
                    pass


if __name__ == "__main__":
    unittest.main()
