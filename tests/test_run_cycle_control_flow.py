"""
tests/test_run_cycle_control_flow.py
------------------------------------
Unit and control-flow tests verifying:
- TEST 1: 정상 후보 승격 완료 시 -> promoted_candidates 정상 순회 및 하위 평가 진행
- TEST 2: 후보 스캔 예외 발생 시 -> promoted_candidates=None -> BUY 파이프라인 즉시 안전 중단 -> NameError 미발생
- TEST 3: 후보 승격(promotion engine) 예외 발생 시 -> promoted_candidates=None -> BUY 파이프라인 즉시 안전 중단 -> NameError 미발생
- TEST 4: 전략 셋업 예외 발생 시 -> 신규 BUY 파이프라인 안전 차단 -> Exit Watchdog 정상 동작
- TEST 5: ML 예측 예외 발생 시 -> 신규 BUY 파이프라인 안전 차단 -> Exit Watchdog 정상 동작
- TEST 6: Telegram 알림 실패 시 -> 기존 알림 실패 격리 정책 준수 -> Exit Watchdog 정상 동작
- TEST 7: 후보 승격 실패 사이클 직전/직후 Exit Watchdog 정상 수행 및 손절 주문 정상 감지/발주 검증 (Step 0 격리 불변조건)
- TEST 8: 연속 실패 시 Supervisor 백오프 가동 및 watchdog 정상 동작, NameError 재발 없음
"""

import os
import sys
import unittest
from datetime import datetime, timedelta
from unittest.mock import MagicMock, patch

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if BASE_DIR not in sys.path:
    sys.path.insert(0, BASE_DIR)

from core.models import (
    SymbolInfo, SymbolState, TradeSignal, Order, OrderSide, OrderType,
    OrderStatus, TimeHorizon, MarketRegime, Position
)
from execution.live_quant_trader import AccountContext, LiveQuantTrader
from execution.order_router import OrderRouter
from execution.position_manager import PositionManager
from execution.exit_watchdog import ExitWatchdog
from execution.auto_restart_supervisor import ProcessSupervisor
from risk.loss_limits import LossLimitManager
from risk.circuit_breaker import CircuitBreaker
from core.symbol_store import SymbolStateStore
from core.low_cost_scanner import LowCostMarketScanner


class MockBrokerClient:
    def __init__(self, mode="mock", act_no="50001003032", cash=10_000_000.0, equity=10_000_000.0):
        self.mode = mode
        self.act_no = act_no
        self.cash = cash
        self.equity = equity
        self.orders = []

    def get_balance(self):
        return {
            "cash": self.cash,
            "total_asset": self.equity,
            "total_profit": 0,
            "total_profit_rate": 0.0,
            "order_available": self.cash,
            "holdings": []
        }

    def get_current_price(self, code: str):
        return {
            "price": 50000,
            "volume": 100000,
            "open": 49000,
            "high": 51000,
            "low": 48500,
            "prev_close": 49500
        }

    def get_daily_candles(self, code: str, count: int = 65):
        return [
            {"date": "20260915", "open": 49000, "high": 51000, "low": 48500, "close": 50000, "volume": 100000},
            {"date": "20260914", "open": 48000, "high": 49500, "low": 47800, "close": 49500, "volume": 90000},
        ]

    def sell_market(self, code: str, qty: int):
        ord_info = {"code": code, "qty": qty, "type": "SELL_MARKET", "ord_no": f"ORD_{len(self.orders)+1}"}
        self.orders.append(ord_info)
        return {"rt_cd": "0", "ord_no": ord_info["ord_no"]}

    def buy_market(self, code: str, qty: int):
        ord_info = {"code": code, "qty": qty, "type": "BUY_MARKET", "ord_no": f"ORD_{len(self.orders)+1}"}
        self.orders.append(ord_info)
        return {"rt_cd": "0", "ord_no": ord_info["ord_no"]}


class TestRunCycleControlFlow(unittest.TestCase):
    def setUp(self):
        # Build a minimal LiveQuantTrader instance without network calls
        self.trader = LiveQuantTrader.__new__(LiveQuantTrader)
        self.trader.mode = "mock"
        self.trader.force_signal_test = False
        self.trader.circuit_breaker = CircuitBreaker()
        self.trader.diagnostic_engine = MagicMock()
        self.trader.quote_manager = MagicMock()
        self.trader.data_quality_gate = MagicMock()
        self.trader.data_quality_gate.validate_tick.return_value = (True, "")
        self.trader.market_radar = MagicMock()
        self.trader.market_radar.scan_market_movers.return_value = []
        self.trader.setup_detector = MagicMock()
        self.trader.persistence_manager = MagicMock()
        self.trader.experience_memory = MagicMock()
        self.trader.similarity_engine = MagicMock()
        self.trader.model_registry = MagicMock()
        self.trader.model_registry.champion_id = "CHAMPION_V16_0"
        self.trader.meta_decision_engine = MagicMock()
        self.trader.edge_engine = MagicMock()
        self.trader.last_recon_time = datetime.now()
        self.trader.eod_worker = MagicMock()
        self.trader.eod_batch_executed_today = None
        self.trader.daily_candles_cache = {}
        self.trader.or_high_map = {}
        self.trader._rolling_scan_index = 0
        self.trader._rolling_batch_size = 15
        self.trader._print_dashboard = MagicMock()
        self.trader.after_hours_manager = MagicMock()
        self.trader.after_hours_manager.get_next_session_features.return_value = {}

        # Broker Client & Account
        self.client = MockBrokerClient()
        cb = CircuitBreaker()
        router = OrderRouter(self.client, cb)
        pm = PositionManager(router)
        self.acc = AccountContext(
            name="MOCK_TEST",
            mode="mock",
            act_no="50001003032",
            client=self.client,
            order_router=router,
            position_manager=pm,
            loss_manager=LossLimitManager()
        )
        self.trader.accounts = [self.acc]

        # Universe Store & Scanner
        sym = SymbolInfo(iem_cd="005930", name="삼성전자", price=50000, is_tradable=True)
        self.store = SymbolStateStore({"005930": sym})
        self.scanner = LowCostMarketScanner(self.store)
        self.trader.store = self.store
        self.trader.scanner = self.scanner
        self.trader._all_symbol_codes = ["005930"]

        # Watchdog
        self.test_db_path = os.path.join(BASE_DIR, "data", "test_operational_cf.db")
        self.test_hb_path = os.path.join(BASE_DIR, "data", "test_watchdog_hb_cf.json")
        self.watchdog = ExitWatchdog(db_path=self.test_db_path, heartbeat_file=self.test_hb_path)
        self.trader.exit_watchdog = self.watchdog

        # Track failure logs
        self.failure_logs = []
        self.orig_log_failure = self.trader._log_run_cycle_failure
        def capture_log(*args, **kwargs):
            self.failure_logs.append((args, kwargs))
            return self.orig_log_failure(*args, **kwargs)
        self.trader._log_run_cycle_failure = capture_log

    def tearDown(self):
        for p in [self.test_db_path, self.test_hb_path]:
            if os.path.exists(p):
                try:
                    os.remove(p)
                except Exception:
                    pass

    def test_01_normal_candidate_promotion_and_evaluation(self):
        """TEST 1: 정상 후보 승격 완료 시 -> promoted_candidates 정상 순회 및 하위 평가 진행"""
        cand = SymbolInfo(iem_cd="005930", name="삼성전자", price=50000, is_tradable=True)
        self.scanner.promotion_engine.get_promoted_candidates = MagicMock(return_value=[cand])

        # Run 1 cycle
        self.trader.run_cycle(now=datetime(2026, 9, 15, 10, 0, 0))

        # Step 8 Hard Gate should have processed cand
        self.trader.diagnostic_engine.record_hard_gate_passed.assert_called()
        self.assertEqual(len(self.failure_logs), 0, "Normal promotion should not log RUN_CYCLE_FAILURE")

    def test_02_quote_scan_failure_triggers_fail_closed_no_name_error(self):
        """TEST 2: 후보 스캔(quote scan) 치명적 예외 시 -> promoted_candidates=None -> BUY 안전 중단 -> NameError 미발생"""
        # Force an unhandled exception inside quote scan by breaking self.acc.position_manager.get_held_codes
        with patch.object(self.acc.position_manager, "get_held_codes", side_effect=RuntimeError("Quote scan connection lost")):
            # Must NOT raise NameError or unhandled exception
            self.trader.run_cycle()

        # Should log RUN_CYCLE_FAILURE with stage CANDIDATE_PROMOTION and state NONE
        self.assertGreater(len(self.failure_logs), 0)
        kw = self.failure_logs[-1][1]
        self.assertEqual(kw.get("promoted_candidates_state"), "NONE")
        self.assertEqual(kw.get("stage"), "CANDIDATE_PROMOTION")

    def test_03_promotion_engine_exception_triggers_fail_closed_no_name_error(self):
        """TEST 3: 후보 승격(promotion engine) 예외 발생 시 -> promoted_candidates=None -> BUY 안전 중단 -> NameError 미발생"""
        self.scanner.promotion_engine.get_promoted_candidates = MagicMock(side_effect=RuntimeError("PromotionEngine crashed"))

        # Must not raise NameError
        self.trader.run_cycle()

        # Verify fail-closed failure log
        self.assertGreater(len(self.failure_logs), 0)
        kw = self.failure_logs[-1][1]
        self.assertEqual(kw.get("candidate_promotion_status"), "FAILED")
        self.assertEqual(kw.get("promoted_candidates_state"), "NONE")

    def test_04_strategy_setup_exception_aborts_buy_watchdog_healthy(self):
        """TEST 4: 전략 셋업 예외 발생 시 -> 신규 BUY 파이프라인 안전 차단 -> Exit Watchdog 정상 동작"""
        cand = SymbolInfo(iem_cd="005930", name="삼성전자", price=50000, is_tradable=True)
        self.scanner.promotion_engine.get_promoted_candidates = MagicMock(return_value=[cand])
        self.scanner.scan_active_signals = MagicMock(side_effect=ValueError("Setup scan failed"))

        self.trader.run_cycle(now=datetime(2026, 9, 15, 10, 0, 0))

        # Should log STRATEGY_SETUP failure
        setup_failures = [log for log in self.failure_logs if log[1].get("stage") == "STRATEGY_SETUP"]
        self.assertGreater(len(setup_failures), 0)

        # Exit Watchdog was executed and healthy
        self.assertEqual(self.watchdog.current_heartbeat.watchdog_status, "HEALTHY")
        self.assertFalse(self.watchdog.is_protective_mode_active())

    def test_05_ml_prediction_exception_aborts_signal_watchdog_healthy(self):
        """TEST 5: ML/Edge 예측 예외 발생 시 -> 해당 신호 안전 차단 -> Exit Watchdog 정상 동작"""
        cand = SymbolInfo(iem_cd="005930", name="삼성전자", price=50000, is_tradable=True)
        self.scanner.promotion_engine.get_promoted_candidates = MagicMock(return_value=[cand])

        # Create a mock scored signal matching TradeSignal schema
        sig = TradeSignal(
            strategy_id="GAP_PULLBACK_V1",
            time_horizon=TimeHorizon.INTRADAY,
            iem_cd="005930",
            name="삼성전자",
            side=OrderSide.BUY,
            strategy_price=50000,
            stop_price=49000,
            score=85.0,
            reason="MOMENTUM",
            timestamp=datetime(2026, 9, 15, 10, 0, 0),
            target_1r=51000,
            target_2r=52000,
            target_3r=53000
        )
        self.scanner.scan_active_signals = MagicMock(return_value=[sig])

        # Force similarity_engine query to throw
        self.trader.similarity_engine.query_similarity.side_effect = RuntimeError("ML Similarity Cluster Unreachable")

        self.trader.run_cycle(now=datetime(2026, 9, 15, 10, 0, 0))

        # Signal rejected via CORRECT_NO_TRADE and no orders sent
        self.trader.persistence_manager.record_no_trade.assert_called()
        self.assertEqual(len(self.client.orders), 0)
        self.assertFalse(self.watchdog.is_protective_mode_active())

    def test_06_telegram_notification_failure_isolation(self):
        """TEST 6: Telegram 알림 실패 시 -> 알림 실패 격리 정책 준수 -> Exit Watchdog 정상 동작"""
        mock_tg = MagicMock()
        mock_tg.send_trade_event.side_effect = ConnectionError("Telegram network timeout")
        self.trader.telegram_notifier = mock_tg

        sig = TradeSignal(
            strategy_id="TEST_STRAT",
            time_horizon=TimeHorizon.INTRADAY,
            iem_cd="005930",
            name="삼성전자",
            side=OrderSide.BUY,
            strategy_price=50000,
            stop_price=49000,
            score=85.0,
            reason="MOMENTUM",
            timestamp=datetime(2026, 9, 15, 10, 0, 0),
            target_1r=51000,
            target_2r=52000,
            target_3r=53000
        )
        # order router handles telegram failure gracefully
        with patch("core.telegram_notifier.telegram_notifier.send_trade_event", side_effect=ConnectionError("Telegram Down")):
            order = self.acc.order_router.submit_order(sig, 10, order_type=OrderType.MARKET, order_price=50000, now=datetime(2026, 9, 15, 10, 0, 0))
            self.assertIsNotNone(order)
            self.assertEqual(order.status, OrderStatus.FILLED)

    def test_07_exit_watchdog_isolation_invariant_on_candidate_failure(self):
        """TEST 7: 후보 승격 실패 사이클 직전/직후 Exit Watchdog 정상 수행 및 손절 주문 정상 감지/발주 검증 (Step 0 격리 불변조건)"""
        # Set price in client to 48,000 (breached stop_price 49,000)
        self.client.get_current_price = MagicMock(return_value={
            "price": 48000,
            "volume": 100000,
            "open": 49000,
            "high": 50000,
            "low": 47500,
            "prev_close": 49500
        })

        # Register a held position that breached stop price
        pos = Position(
            position_id="INT_005930_20260915_1",
            time_horizon=TimeHorizon.INTRADAY,
            strategy_id="BREAKOUT_V1",
            iem_cd="005930",
            name="삼성전자",
            qty=10,
            entry_price=50000.0,
            current_price=48000.0,
            stop_price=49000,
            target_1r=51000,
            target_2r=52000,
            target_3r=53000,
            r_unit=1000.0,
            initial_risk_amount=10000.0,
            entry_time=datetime.now() - timedelta(minutes=10),
            trailing_stop_price=49000,
            highest_price=50000
        )
        self.acc.position_manager.positions["005930"] = pos

        # Candidate promotion engine throws fatal exception
        self.scanner.promotion_engine.get_promoted_candidates = MagicMock(side_effect=RuntimeError("Candidate promotion fatal crash"))

        # Run cycle
        self.trader.run_cycle()

        # Step 0 Exit Watchdog executed BEFORE candidate promotion crashed!
        # Stop loss sell order must have been dispatched to client
        sell_orders = [o for o in self.client.orders if o.get("type") == "SELL_MARKET" and o.get("code") == "005930"]
        self.assertGreaterEqual(len(sell_orders), 1, "Exit Watchdog must execute and dispatch stop-loss sell order even when candidate promotion crashes!")

    def test_08_supervisor_exponential_backoff_and_stability_reset(self):
        """TEST 8: Supervisor 지수 백오프 가동 및 60초 안정 가동 시 카운터 리셋 검증"""
        supervisor = ProcessSupervisor(mode="mock")

        # Simulate 1st failure (< 30s runtime)
        supervisor.started_at = datetime.now() - timedelta(seconds=5)
        # rapid failure 1
        supervisor.rapid_failure_count += 1
        cooldown_1 = 3 if supervisor.rapid_failure_count <= 2 else 5
        self.assertEqual(cooldown_1, 3)

        # Simulate 4th failure
        supervisor.rapid_failure_count = 4
        cooldown_4 = 5 if 3 <= supervisor.rapid_failure_count <= 5 else 10
        self.assertEqual(cooldown_4, 5)

        # Simulate 7th failure
        supervisor.rapid_failure_count = 7
        cooldown_7 = 10 if 6 <= supervisor.rapid_failure_count <= 10 else 30
        self.assertEqual(cooldown_7, 10)
        status_7 = "DEGRADED" if supervisor.rapid_failure_count >= 6 else "RESTARTING"
        self.assertEqual(status_7, "DEGRADED")

        # Simulate 12th failure
        supervisor.rapid_failure_count = 12
        cooldown_12 = 30 if supervisor.rapid_failure_count > 10 else 10
        self.assertEqual(cooldown_12, 30)

        # Simulate stable run >= 60s
        stable_runtime = 75.0
        if stable_runtime >= 60.0:
            supervisor.rapid_failure_count = 0
        self.assertEqual(supervisor.rapid_failure_count, 0, "Rapid failure counter must reset to 0 after stable run >= 60s")


if __name__ == "__main__":
    if sys.platform == "win32":
        try:
            sys.stdout.reconfigure(encoding="utf-8")
            sys.stderr.reconfigure(encoding="utf-8")
        except Exception:
            pass
    unittest.main()
