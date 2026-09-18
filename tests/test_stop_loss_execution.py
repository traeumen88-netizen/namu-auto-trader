import unittest
import os
from datetime import datetime
from typing import Dict, Any

from core.models import (
    Position, TimeHorizon, OrderSide, OrderType, OrderStatus, TradeSignal
)
from execution.position_manager import PositionManager
from execution.order_router import OrderRouter
from execution.persistence_manager import PersistenceManager
from risk.circuit_breaker import CircuitBreaker


class MockBrokerClient:
    def __init__(self):
        self.orders = []
        self.dry_run = False

    def sell_market(self, iem_cd: str, qty: int) -> Dict[str, Any]:
        self.orders.append({"side": "SELL", "order_type": "MARKET", "iem_cd": iem_cd, "qty": qty})
        return {"Output_0": {"mkt_orr_no": f"SELL_MKT_{iem_cd}"}}

    def sell_limit(self, iem_cd: str, qty: int, price: int) -> Dict[str, Any]:
        self.orders.append({"side": "SELL", "order_type": "LIMIT", "iem_cd": iem_cd, "qty": qty, "price": price})
        return {"Output_0": {"mkt_orr_no": f"SELL_LMT_{iem_cd}"}}

    def buy_market(self, iem_cd: str, qty: int) -> Dict[str, Any]:
        self.orders.append({"side": "BUY", "order_type": "MARKET", "iem_cd": iem_cd, "qty": qty})
        return {"Output_0": {"mkt_orr_no": f"BUY_MKT_{iem_cd}"}}


class TestStopLossExecution(unittest.TestCase):
    def setUp(self):
        self.test_db = "data/test_stop_loss_op.db"
        if os.path.exists(self.test_db):
            os.remove(self.test_db)
        self.client = MockBrokerClient()
        self.circuit_breaker = CircuitBreaker()
        self.circuit_breaker.update_data_heartbeat(datetime.now())
        self.router = OrderRouter(namu_client=self.client, circuit_breaker=self.circuit_breaker)
        self.persistence = PersistenceManager(db_path=self.test_db)
        self.pm = PositionManager(order_router=self.router, persistence_manager=self.persistence)

    def tearDown(self):
        if os.path.exists(self.test_db):
            try:
                os.remove(self.test_db)
            except Exception:
                pass

    def test_01_get_held_codes_and_has_position(self):
        pos = self.pm.open_position(
            time_horizon=TimeHorizon.INTRADAY,
            strategy_id="TEST_STRAT",
            iem_cd="005930",
            name="삼성전자",
            qty=10,
            entry_price=70000,
            stop_price=68500,
            target_1r=71500,
            target_2r=73000,
            target_3r=74500,
            initial_risk=15000
        )
        self.assertTrue(pos.position_id.startswith("INT_005930_"))
        self.assertIn(pos.position_id, self.pm.positions)
        self.assertNotIn("005930", self.pm.positions)

        self.assertEqual(self.pm.get_held_codes(), ["005930"])
        self.assertTrue(self.pm.has_position("005930"))
        self.assertFalse(self.pm.has_position("000660"))

    def test_02_stop_loss_execution_and_persistence(self):
        self.pm.open_position(
            time_horizon=TimeHorizon.INTRADAY,
            strategy_id="TEST_INT",
            iem_cd="342870",
            name="프로이천",
            qty=13,
            entry_price=3715,
            stop_price=3622,
            target_1r=3808,
            target_2r=3901,
            target_3r=3994,
            initial_risk=1209
        )
        market_now = datetime(2026, 9, 10, 10, 30, 0)
        self.pm.update_price_and_manage("342870", 3650, market_now)
        self.assertEqual(len(self.client.orders), 0)
        self.assertTrue(self.pm.has_position("342870"))

        # 가격이 3620원(<= 손절가 3622원) 도달 시 손절 발동
        self.pm.update_price_and_manage("342870", 3620, market_now)
        self.assertEqual(len(self.client.orders), 1)
        self.assertEqual(self.client.orders[0]["side"], "SELL")
        self.assertEqual(self.client.orders[0]["order_type"], "MARKET")
        self.assertEqual(self.client.orders[0]["iem_cd"], "342870")
        self.assertEqual(self.client.orders[0]["qty"], 13)

        self.assertFalse(self.pm.has_position("342870"))
        self.assertEqual(len(self.pm.closed_positions), 1)

        with self.persistence._get_conn() as conn:
            stops = conn.execute("SELECT * FROM stops").fetchall()
            self.assertEqual(len(stops), 1)
            self.assertEqual(stops[0]["iem_cd"], "342870")
            self.assertEqual(stops[0]["stop_price"], 3622.0)
            self.assertEqual(stops[0]["current_price"], 3620.0)

            orders = conn.execute("SELECT * FROM orders WHERE side='SELL'").fetchall()
            self.assertEqual(len(orders), 1)
            self.assertEqual(orders[0]["iem_cd"], "342870")
            self.assertEqual(orders[0]["status"], "FILLED")

    def test_03_circuit_breaker_blocks_buy_but_allows_stop_loss(self):
        self.circuit_breaker.trip("테스트 비상 정지")
        self.assertTrue(self.circuit_breaker.is_tripped)

        buy_sig = TradeSignal(
            strategy_id="TEST_BUY",
            time_horizon=TimeHorizon.INTRADAY,
            iem_cd="005930",
            name="삼성전자",
            side=OrderSide.BUY,
            strategy_price=70000,
            stop_price=68000,
            score=80.0,
            reason="테스트",
            timestamp=datetime.now()
        )
        res_buy = self.router.submit_order(buy_sig, 10, OrderType.MARKET, 70000)
        self.assertIsNone(res_buy)

        self.pm.open_position(
            time_horizon=TimeHorizon.INTRADAY,
            strategy_id="TEST_INT",
            iem_cd="005930",
            name="삼성전자",
            qty=10,
            entry_price=70000,
            stop_price=68500,
            target_1r=71500,
            target_2r=73000,
            target_3r=74500,
            initial_risk=15000
        )
        self.pm.update_price_and_manage("005930", 68000, datetime.now())

        self.assertEqual(len(self.client.orders), 1)
        self.assertEqual(self.client.orders[0]["side"], "SELL")
        self.assertEqual(self.client.orders[0]["iem_cd"], "005930")
        self.assertFalse(self.pm.has_position("005930"))


if __name__ == "__main__":
    unittest.main()
