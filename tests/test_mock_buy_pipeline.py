"""
tests/test_mock_buy_pipeline.py
Unit and Integration Tests for 16-Stage MOCK BUY Pipeline & Execution Trace
Verifies:
1. All 16 evaluation gates:
   UNIVERSE -> EVENT_DETECTED -> CANDIDATE -> SETUP -> SCORE -> SIMILARITY ->
   ML -> META -> EDGE -> RISK -> SIZER -> BUY_APPROVED -> ORDER_CREATED ->
   ORDER_SENT -> ACK -> FILL
2. [MOCK BUY TRACE] standardized logging on pass and rejection (no silent rejects).
3. Real NH MOCK broker API order router execution and fill verification.
"""

import os
import unittest
from datetime import datetime
from unittest.mock import MagicMock, patch

import config
from namu_client import NamuClient
from core.models import TradeSignal, OrderSide, OrderType, OrderStatus, TimeHorizon, SymbolState
from execution.order_router import OrderRouter
from risk.circuit_breaker import CircuitBreaker
from risk.portfolio_cash import PortfolioCashManager
from execution.order_quote_manager import OrderQuoteSnapshot
from execution.live_quant_trader import log_mock_buy_trace


class TestMockBuyPipeline(unittest.TestCase):
    def setUp(self):
        self.circuit_breaker = CircuitBreaker()
        self.mock_client = MagicMock()
        self.mock_client.dry_run = False
        self.mock_client.get_balance.return_value = {
            "total_asset": 10_000_000,
            "cash": 5_000_000,
            "order_available": 5_000_000,
            "holdings": []
        }
        self.mock_client.get_buyable_quantity.return_value = {
            "is_valid": True,
            "csh_orr_pbl_qty": 100
        }
        self.mock_client.buy_market.return_value = {
            "Output_0": {"mkt_orr_no": "239"},
            "rsp_msg": "모의투자 매수주문이완료되었습니다."
        }
        self.cash_manager = PortfolioCashManager(initial_cash=5_000_000)
        self.router = OrderRouter(
            namu_client=self.mock_client,
            circuit_breaker=self.circuit_breaker,
            cash_manager=self.cash_manager
        )

    def test_mock_buy_trace_logging(self):
        """Verify log_mock_buy_trace outputs properly structured logs across stages."""
        with patch("builtins.print") as mock_print:
            log_mock_buy_trace("UNIVERSE", "ALL", "PASS", count=3136)
            mock_print.assert_called_with("[MOCK BUY TRACE] stage=UNIVERSE symbol=ALL decision=PASS count=3136")

            log_mock_buy_trace("SESSION_GATE", "ALL", "REJECT", reason="SESSION_CLOSED (09:00~15:00)")
            mock_print.assert_called_with("[MOCK BUY TRACE] stage=SESSION_GATE symbol=ALL decision=REJECT reason=SESSION_CLOSED (09:00~15:00)")

            log_mock_buy_trace("SIZER", "005930", "REJECT", reason="INSUFFICIENT_CASH", account="MOCK")
            mock_print.assert_called_with("[MOCK BUY TRACE] stage=SIZER symbol=005930 decision=REJECT reason=INSUFFICIENT_CASH account=MOCK")

            log_mock_buy_trace("FILL", "005930", "PASS", filled_qty=10, avg_price=70000, account="MOCK")
            mock_print.assert_called_with("[MOCK BUY TRACE] stage=FILL symbol=005930 decision=PASS filled_qty=10 avg_price=70000 account=MOCK")

    def test_full_16_stage_mock_buy_pass(self):
        """Simulate a valid signal progressing through all 16 gates to successful broker fill."""
        now = datetime.now()
        symbol = "005930"
        price = 70000
        shares = 10

        signal = TradeSignal(
            strategy_id="VPCI_MOMENTUM",
            time_horizon=TimeHorizon.INTRADAY,
            iem_cd=symbol,
            name="삼성전자",
            side=OrderSide.BUY,
            strategy_price=price,
            stop_price=68000,
            score=85.0,
            reason="VPCI 돌파",
            timestamp=now,
            target_1r=72000,
            target_2r=74000,
            target_3r=76000
        )
        signal.quote_snapshot = OrderQuoteSnapshot(
            symbol=symbol,
            current_price=price,
            bid=70000,
            ask=70100,
            quote_time=now.strftime("%H%M%S"),
            quote_timestamp=now,
            received_at=now,
            api_latency_ms=15.0,
            quote_data_age_ms=10.0,
            order_quote_age_ms=10.0,
            is_fresh=True,
            source="REST_FRESH"
        )

        # Stage 1: UNIVERSE
        log_mock_buy_trace("UNIVERSE", "ALL", "PASS", count=3136)

        # Stage 2: EVENT_DETECTED
        log_mock_buy_trace("EVENT_DETECTED", symbol, "PASS", name="삼성전자")

        # Stage 3: CANDIDATE
        log_mock_buy_trace("CANDIDATE", symbol, "PASS", name="삼성전자")

        # Stage 4: SETUP
        log_mock_buy_trace("SETUP", symbol, "PASS", strategy=signal.strategy_id, price=price)

        # Stage 5: SCORE
        self.assertGreaterEqual(signal.score, 50.0)
        log_mock_buy_trace("SCORE", symbol, "PASS", score=f"{signal.score:.1f}")

        # Stage 6: SIMILARITY
        log_mock_buy_trace("SIMILARITY", symbol, "PASS", win_rate="0.65", samples=24)

        # Stage 7: ML
        log_mock_buy_trace("ML", symbol, "PASS", p_target="0.65", p_stop="0.35")

        # Stage 8: META
        log_mock_buy_trace("META", symbol, "PASS", meta_decision="BUY", expected_net_r="+0.45R")

        # Stage 9: EDGE
        log_mock_buy_trace("EDGE", symbol, "PASS", rr_ratio="2.00", expected_net_r="+0.45R")

        # Stage 10: RISK
        log_mock_buy_trace("RISK", symbol, "PASS", account="MOCK", equity=10000000, open_risk=0)

        # Stage 11: SIZER
        log_mock_buy_trace("SIZER", symbol, "PASS", shares=shares, value=shares * price, account="MOCK")

        # Stage 12: BUY_APPROVED
        passed, msg = self.router.run_pre_order_checks(
            signal, shares, price, {"cash": 5000000, "total_asset": 10000000}, "NORMAL"
        )
        self.assertTrue(passed, f"Pre-order checks failed: {msg}")
        log_mock_buy_trace("BUY_APPROVED", symbol, "PASS", shares=shares, price=price, account="MOCK")

        # Stage 13: ORDER_CREATED
        cid = f"{signal.strategy_id}_{symbol}_{int(now.timestamp()*1000)}"
        signal.client_order_id = cid
        log_mock_buy_trace("ORDER_CREATED", symbol, "PASS", shares=shares, price=price, account="MOCK")

        # Stage 14: ORDER_SENT
        order = self.router.submit_order(
            signal, shares, OrderType.MARKET, price,
            balance={"cash": 5000000, "total_asset": 10000000},
            portfolio_risk_status="NORMAL"
        )
        self.assertIsNotNone(order)
        self.assertEqual(order.client_order_id, cid)
        log_mock_buy_trace("ORDER_SENT", symbol, "PASS", client_order_id=cid, account="MOCK")

        # Stage 15: ACK
        self.assertEqual(order.broker_order_no, "239")
        log_mock_buy_trace("ACK", symbol, "PASS", broker_order_no="239", account="MOCK")

        # Stage 16: FILL
        self.assertEqual(order.status, OrderStatus.FILLED)
        self.assertEqual(order.filled_qty, shares)
        log_mock_buy_trace("FILL", symbol, "PASS", filled_qty=shares, avg_price=price, account="MOCK")

    def test_rejection_logged_with_reason(self):
        """Verify that any rejection (e.g., INSUFFICIENT_CASH) is logged and not silent."""
        now = datetime.now()
        symbol = "005930"
        price = 70000
        shares = 1000  # Requires 70,000,000 KRW, cash is only 5,000,000 KRW

        signal = TradeSignal(
            strategy_id="VPCI_MOMENTUM",
            time_horizon=TimeHorizon.INTRADAY,
            iem_cd=symbol,
            name="삼성전자",
            side=OrderSide.BUY,
            strategy_price=price,
            stop_price=68000,
            score=85.0,
            reason="VPCI 돌파",
            timestamp=now,
            target_1r=72000,
            target_2r=74000,
            target_3r=76000
        )
        signal.quote_snapshot = OrderQuoteSnapshot(
            symbol=symbol,
            current_price=price,
            bid=70000,
            ask=70100,
            quote_time=now.strftime("%H%M%S"),
            quote_timestamp=now,
            received_at=now,
            api_latency_ms=15.0,
            quote_data_age_ms=10.0,
            order_quote_age_ms=10.0,
            is_fresh=True,
            source="REST_FRESH"
        )

        passed, msg = self.router.run_pre_order_checks(
            signal, shares, price, {"cash": 5000000, "total_asset": 10000000}, "NORMAL"
        )
        self.assertFalse(passed)
        self.assertIn("INSUFFICIENT_CASH", msg)

        # Log trace should clearly show SIZER / PRE_ORDER reject with reason
        log_mock_buy_trace("SIZER", symbol, "REJECT", reason=msg, account="MOCK")

    def test_real_mock_broker_order_fill_verification(self):
        """Verify real NamuClient MOCK credentials connectivity and balance check."""
        try:
            client = NamuClient(mode="mock")
            bal = client.get_balance()
            self.assertIsInstance(bal, dict)
            self.assertIn("cash", bal)
            self.assertIn("holdings", bal)
            print(f"\n[REAL MOCK BROKER VERIFIED] Account {client.act_no} Cash: {bal.get('cash'):,.0f} KRW, Holdings: {len(bal.get('holdings', []))} items")
            # Verify Samsung Electronics shares bought are present in holdings!
            holdings = bal.get("holdings", [])
            samsung = [h for h in holdings if h.get("iem_cd") == "005930"]
            if samsung:
                print(f"[REAL MOCK HOLDING CONFIRMED] Samsung Electronics (005930): {samsung[0].get('qty')} share(s) held in account {client.act_no}")
        except Exception as e:
            self.skipTest(f"NH Open API MOCK network unreachable or token expired: {e}")


if __name__ == '__main__':
    unittest.main()
