import unittest
from datetime import datetime, timedelta
from execution.order_quote_manager import OrderQuoteManager, OrderQuoteSnapshot
from core.models import TradeSignal, OrderSide, TimeHorizon


class DummyClient:
    def __init__(self, price=70000, valid=True, delay_sec=0.0):
        self.price = price
        self.valid = valid
        self.delay_sec = delay_sec

    def get_current_price(self, symbol: str):
        if not self.valid:
            return {"price": 0, "is_valid": False}
        ts = datetime.now() - timedelta(seconds=self.delay_sec)
        return {
            "iem_cd": symbol,
            "price": self.price,
            "bid": self.price - 100,
            "ask": self.price + 100,
            "is_valid": True,
            "timestamp": ts
        }


class TestOrderQuoteManager(unittest.TestCase):
    def setUp(self):
        self.qm = OrderQuoteManager(max_order_age_sec=3.0)

    def test_fresh_quote_passed(self):
        client = DummyClient(price=50000, delay_sec=0.5)
        snap = self.qm.sync_fresh_quote(client, "005930", signal_price=50000)
        self.assertTrue(snap.is_fresh)
        self.assertEqual(snap.current_price, 50000)
        self.assertLessEqual(snap.order_quote_age_ms, 3000.0)
        self.assertEqual(self.qm.fresh_quote_passed, 1)

    def test_stale_quote_rejected(self):
        client = DummyClient(price=50000, delay_sec=4.5)  # 4.5초 지연
        snap = self.qm.sync_fresh_quote(client, "005930", signal_price=50000)
        self.assertFalse(snap.is_fresh)
        self.assertIn("ORDER_QUOTE_AGE_EXCEEDED", snap.staleness_reason)
        self.assertEqual(self.qm.data_stale_count, 1)
        self.assertEqual(self.qm.age_distribution["3~5초"], 1)

    def test_invalid_quote_rejected(self):
        client = DummyClient(valid=False)
        snap = self.qm.sync_fresh_quote(client, "005930", signal_price=50000)
        self.assertFalse(snap.is_fresh)
        self.assertEqual(self.qm.data_stale_count, 1)
        self.assertEqual(self.qm.age_distribution["30초 이상"], 1)


if __name__ == "__main__":
    unittest.main()
