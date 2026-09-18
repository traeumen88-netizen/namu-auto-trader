"""
Test Execution Re-Fetch Parity and Fail-Safe Verification
- Validates the 2-step execution architecture:
    Stale Quote Detected -> 1-time On-demand Re-fetch -> Freshness Re-verification (<= 3.0s)
    -> 5-point Pre-order Checks -> Order Submission & Fill
- Verifies today's 3 specific orders:
    1. 062970 한국프랜지 (Stale quote -> Re-fetch -> Filled)
    2. 418620 이엔셀 (Stale quote -> Re-fetch -> Filled)
    3. 005160 동방 (Swing budget auto-downsizing + Re-fetch -> Filled, 30% intraday reserve preserved)
- Verifies strict Fail-Safe preservation:
    4. Re-fetch still stale -> BLOCKED
    5. Price jumped > 1.5% -> BLOCKED
    6. R:R < 1.45 -> BLOCKED
    7. Spread > budget -> BLOCKED
"""

import unittest
from datetime import datetime, timedelta
from typing import Dict, Any, Optional

from core.models import (
    TradeSignal, OrderSide, OrderType, OrderStatus, TimeHorizon, Order
)
from execution.order_router import OrderRouter
from execution.order_quote_manager import OrderQuoteSnapshot, OrderQuoteManager
from risk.portfolio_cash import PortfolioCashManager


class MockCircuitBreaker:
    is_tripped = False
    trip_reason = ""
    last_data_timestamp = None

    def record_order_success(self):
        pass

    def record_order_failure(self, err):
        pass


class MockBrokerClient:
    def __init__(self, dry_run: bool = True):
        self.dry_run = dry_run
        self.quotes: Dict[str, Dict[str, Any]] = {}
        self.buy_calls = []

    def set_quote(self, symbol: str, price: int, bid: int, ask: int, timestamp: Optional[datetime] = None):
        self.quotes[symbol] = {
            "is_valid": True,
            "price": price,
            "bid": bid,
            "ask": ask,
            "timestamp": timestamp or datetime.now(),
            "quote_time": (timestamp or datetime.now()).strftime("%H:%M:%S")
        }

    def get_current_price(self, symbol: str) -> Dict[str, Any]:
        return self.quotes.get(symbol, {"is_valid": False, "price": 0})

    def get_buyable_quantity(self, iem_cd: str, price: int, order_type: str = "01") -> Dict[str, Any]:
        return {"is_valid": True, "csh_orr_pbl_qty": 100000}

    def buy_limit(self, symbol: str, shares: int, price: int) -> Dict[str, Any]:
        self.buy_calls.append({"symbol": symbol, "shares": shares, "price": price})
        return {"Output_0": {"mkt_orr_no": f"MOCK_{symbol}_{shares}"}}


class TestExecutionRefetchParity(unittest.TestCase):
    def setUp(self):
        self.now = datetime(2026, 9, 14, 10, 0, 0)
        self.cb = MockCircuitBreaker()
        self.client = MockBrokerClient(dry_run=True)

    def test_01_korea_flange_062970_refetch_parity(self):
        """Case 1: 한국프랜지(062970) - Stale Quote -> 1-time Re-fetch -> Paper Fill 성공"""
        router = OrderRouter(namu_client=self.client, circuit_breaker=self.cb)
        
        # 1. 초기 4.2초 지연된 Stale Quote Snapshot 설정
        stale_time = self.now - timedelta(seconds=4.2)
        stale_quote = OrderQuoteSnapshot(
            symbol="062970",
            current_price=2115,
            bid=2110,
            ask=2115,
            quote_time=stale_time.strftime("%H:%M:%S"),
            quote_timestamp=stale_time,
            received_at=stale_time,
            api_latency_ms=50.0,
            quote_data_age_ms=4200.0,
            order_quote_age_ms=4200.0,
            is_fresh=False,
            staleness_reason="ORDER_QUOTE_AGE_EXCEEDED (4.2s > 3.0s)"
        )
        
        sig = TradeSignal(
            strategy_id="INT_MOMENTUM",
            time_horizon=TimeHorizon.INTRADAY,
            iem_cd="062970",
            name="한국프랜지",
            side=OrderSide.BUY,
            strategy_price=2115,
            stop_price=2080,
            score=85.0,
            reason="MOMENTUM_BREAKOUT",
            timestamp=self.now,
            target_1r=2150,
            target_2r=2185
        )
        sig.quote_snapshot = stale_quote

        # 2. 브로커 최신 호가 Mock (신선한 호가 공급: age 0.1초)
        self.client.set_quote("062970", price=2115, bid=2110, ask=2115, timestamp=self.now - timedelta(milliseconds=100))

        # 3. 주문 제출 실행
        order = router.submit_order(
            signal=sig,
            shares=100,
            order_price=2115,
            balance={"cash": 10_000_000},
            now=self.now
        )

        # 4. 검증: Re-fetch를 통해 신선한 호가로 교체되고 정상 Paper 체결 완료
        self.assertIsNotNone(order, "Order must not be None after successful re-fetch")
        self.assertEqual(order.status, OrderStatus.FILLED)
        self.assertEqual(order.filled_qty, 100)
        self.assertAlmostEqual(order.filled_avg_price, 2115, delta=5)
        self.assertTrue(sig.quote_snapshot.is_fresh)
        self.assertEqual(sig.approved_status, "FILLED")
        print("\n[Parity PASS 1] 한국프랜지(062970): Stale 호가(4.2s) -> Re-fetch 갱신 성공 -> 100주 체결 완료")

    def test_02_encell_418620_refetch_parity(self):
        """Case 2: 이엔셀(418620) - Stale Quote -> 1-time Re-fetch -> Paper Fill 성공"""
        router = OrderRouter(namu_client=self.client, circuit_breaker=self.cb)
        
        # 1. 초기 3.5초 지연된 Stale Quote Snapshot 설정
        stale_time = self.now - timedelta(seconds=3.5)
        stale_quote = OrderQuoteSnapshot(
            symbol="418620",
            current_price=678,
            bid=677,
            ask=678,
            quote_time=stale_time.strftime("%H:%M:%S"),
            quote_timestamp=stale_time,
            received_at=stale_time,
            api_latency_ms=60.0,
            quote_data_age_ms=3500.0,
            order_quote_age_ms=3500.0,
            is_fresh=False,
            staleness_reason="ORDER_QUOTE_AGE_EXCEEDED (3.5s > 3.0s)"
        )
        
        sig = TradeSignal(
            strategy_id="INT_BREAKOUT",
            time_horizon=TimeHorizon.INTRADAY,
            iem_cd="418620",
            name="이엔셀",
            side=OrderSide.BUY,
            strategy_price=678,
            stop_price=665,
            score=82.0,
            reason="BREAKOUT_DAY_HIGH",
            timestamp=self.now,
            target_1r=691,
            target_2r=704
        )
        sig.quote_snapshot = stale_quote

        # 2. 브로커 최신 호가 Mock
        self.client.set_quote("418620", price=678, bid=677, ask=678, timestamp=self.now - timedelta(milliseconds=150))

        # 3. 주문 제출
        order = router.submit_order(
            signal=sig,
            shares=200,
            order_price=678,
            balance={"cash": 10_000_000},
            now=self.now
        )

        # 4. 검증
        self.assertIsNotNone(order)
        self.assertEqual(order.status, OrderStatus.FILLED)
        self.assertEqual(order.filled_qty, 200)
        self.assertEqual(sig.approved_status, "FILLED")
        print("[Parity PASS 2] 이엔셀(418620): Stale 호가(3.5s) -> Re-fetch 갱신 성공 -> 200주 체결 완료")

    def test_03_dongbang_005160_swing_cash_downsizing_and_refetch(self):
        """Case 3: 동방(005160) - 스윙 예산 다운사이징 + Re-fetch -> Paper Fill 성공 (단타 30% 유보 보호)"""
        # 총 예수금 1,000,000원 -> 단타 유보 30%(300,000원), 스윙 가용 70%(700,000원)
        cash_mgr = PortfolioCashManager(initial_cash=1_000_000.0)
        router = OrderRouter(namu_client=self.client, circuit_breaker=self.cb, cash_manager=cash_mgr)

        stale_time = self.now - timedelta(seconds=4.0)
        stale_quote = OrderQuoteSnapshot(
            symbol="005160",
            current_price=3180,
            bid=3175,
            ask=3180,
            quote_time=stale_time.strftime("%H:%M:%S"),
            quote_timestamp=stale_time,
            received_at=stale_time,
            api_latency_ms=40.0,
            quote_data_age_ms=4000.0,
            order_quote_age_ms=4000.0,
            is_fresh=False
        )

        sig = TradeSignal(
            strategy_id="SWING_TREND",
            time_horizon=TimeHorizon.SWING,
            iem_cd="005160",
            name="동방",
            side=OrderSide.BUY,
            strategy_price=3180,
            stop_price=3100,
            score=88.0,
            reason="SWING_MOMENTUM",
            timestamp=self.now,
            target_1r=3260,
            target_2r=3340
        )
        sig.quote_snapshot = stale_quote

        # 300주 요청: 300 * 3,180 = 954,000원 (+비용 약 620원 = 954,620원) -> 스윙 한도 700,000원 초과
        # 자동 다운사이징되어 약 219주 (약 696,420원)로 축소 발주되어야 함
        self.client.set_quote("005160", price=3180, bid=3175, ask=3180, timestamp=self.now - timedelta(milliseconds=200))

        order = router.submit_order(
            signal=sig,
            shares=300,
            order_price=3180,
            balance={"cash": 1_000_000},
            now=self.now
        )

        self.assertIsNotNone(order, "Order should succeed via automatic swing downsizing")
        self.assertEqual(order.status, OrderStatus.FILLED)
        self.assertLessEqual(order.filled_qty, 220)
        self.assertGreaterEqual(order.filled_qty, 215)
        # 단타 30% 현금 유보(300,000원) 침범 없이 잔여 유효 현금이 보존되었는지 검증
        self.assertGreaterEqual(cash_mgr.effective_available_cash, 300_000.0)
        print(f"[Parity PASS 3] 동방(005160): 스윙 300주 -> {order.filled_qty}주 자동 다운사이징, 단타 30% 유보 보호 및 체결 완료")

    def test_04_failsafe_refetch_still_stale_strictly_blocks(self):
        """Fail-Safe 1: Re-fetch 후에도 Quote가 Stale(>3.0s)이면 엄격히 주문 차단"""
        router = OrderRouter(namu_client=self.client, circuit_breaker=self.cb)
        
        stale_time = self.now - timedelta(seconds=10.0)
        stale_quote = OrderQuoteSnapshot(
            symbol="005930",
            current_price=70000,
            bid=69900,
            ask=70000,
            quote_time=stale_time.strftime("%H:%M:%S"),
            quote_timestamp=stale_time,
            received_at=stale_time,
            api_latency_ms=50.0,
            quote_data_age_ms=10000.0,
            order_quote_age_ms=10000.0,
            is_fresh=False
        )

        sig = TradeSignal(
            strategy_id="INT_BREAKOUT",
            time_horizon=TimeHorizon.INTRADAY,
            iem_cd="005930",
            name="삼성전자",
            side=OrderSide.BUY,
            strategy_price=70000,
            stop_price=69000,
            score=75.0,
            reason="TEST_FAILSAFE",
            timestamp=self.now,
            target_1r=71000,
            target_2r=72000
        )
        sig.quote_snapshot = stale_quote

        # 브로커 재조회 시세도 5.0초 전 시세로 여전히 stale
        self.client.set_quote("005930", price=70000, bid=69900, ask=70000, timestamp=self.now - timedelta(seconds=5.0))

        order = router.submit_order(
            signal=sig,
            shares=10,
            order_price=70000,
            balance={"cash": 10_000_000},
            now=self.now
        )

        self.assertIsNone(order, "Order must be blocked when re-fetch is still stale")
        self.assertEqual(sig.approved_status, "REJECTED")
        self.assertTrue(any("과도한 지연" in r or "RE_FETCH_STALE" in r for r in sig.rejection_reasons))
        print("[Fail-Safe PASS 4] Re-fetch 후에도 지연 상태(5.0s > 3.0s)인 경우 주문 엄격 차단 확인")

    def test_05_failsafe_refetch_price_jump_strictly_blocks(self):
        """Fail-Safe 2: Re-fetch 후 가격이 1.5% 이상 급변동한 경우 슬리피지 보호 차단"""
        router = OrderRouter(namu_client=self.client, circuit_breaker=self.cb)
        
        stale_time = self.now - timedelta(seconds=3.2)
        stale_quote = OrderQuoteSnapshot(
            symbol="005930",
            current_price=70000,
            bid=69900,
            ask=70000,
            quote_time=stale_time.strftime("%H:%M:%S"),
            quote_timestamp=stale_time,
            received_at=stale_time,
            api_latency_ms=50.0,
            quote_data_age_ms=3200.0,
            order_quote_age_ms=3200.0,
            is_fresh=False
        )

        sig = TradeSignal(
            strategy_id="INT_BREAKOUT",
            time_horizon=TimeHorizon.INTRADAY,
            iem_cd="005930",
            name="삼성전자",
            side=OrderSide.BUY,
            strategy_price=70000,
            stop_price=69000,
            score=75.0,
            reason="TEST_FAILSAFE",
            timestamp=self.now,
            target_1r=71000,
            target_2r=72000
        )
        sig.quote_snapshot = stale_quote

        # 재조회 시세: 71,500원 (+2.14% 급등 > 1.5% 상한)
        self.client.set_quote("005930", price=71500, bid=71400, ask=71500, timestamp=self.now - timedelta(milliseconds=50))

        order = router.submit_order(
            signal=sig,
            shares=10,
            order_price=70000,
            balance={"cash": 10_000_000},
            now=self.now
        )

        self.assertIsNone(order, "Order must be blocked when price jumped > 1.5%")
        self.assertEqual(sig.approved_status, "REJECTED")
        self.assertTrue(any("가격 급변동 초과" in r for r in sig.rejection_reasons))
        print("[Fail-Safe PASS 5] Re-fetch 후 가격 급등(+2.14% > 1.5%) 감지 시 주문 엄격 차단 확인")

    def test_06_failsafe_refetch_rr_insufficient_strictly_blocks(self):
        """Fail-Safe 3: Re-fetch 후 R:R이 1.45 미만으로 붕괴된 경우 주문 차단"""
        router = OrderRouter(namu_client=self.client, circuit_breaker=self.cb)
        
        stale_time = self.now - timedelta(seconds=3.2)
        stale_quote = OrderQuoteSnapshot(
            symbol="005930",
            current_price=70000,
            bid=69900,
            ask=70000,
            quote_time=stale_time.strftime("%H:%M:%S"),
            quote_timestamp=stale_time,
            received_at=stale_time,
            api_latency_ms=50.0,
            quote_data_age_ms=3200.0,
            order_quote_age_ms=3200.0,
            is_fresh=False
        )

        # 진입 70,000, 손절 69,000 (위험 1,000원), 목표가 71,000원 (보상 1,000원 -> R:R 1.0 < 1.45)
        sig = TradeSignal(
            strategy_id="INT_BREAKOUT",
            time_horizon=TimeHorizon.INTRADAY,
            iem_cd="005930",
            name="삼성전자",
            side=OrderSide.BUY,
            strategy_price=70000,
            stop_price=69000,
            score=75.0,
            reason="TEST_FAILSAFE",
            timestamp=self.now,
            target_1r=70500,
            target_2r=71000
        )
        sig.quote_snapshot = stale_quote

        self.client.set_quote("005930", price=70000, bid=69900, ask=70000, timestamp=self.now - timedelta(milliseconds=50))

        order = router.submit_order(
            signal=sig,
            shares=10,
            order_price=70000,
            balance={"cash": 10_000_000},
            now=self.now
        )

        self.assertIsNone(order, "Order must be blocked when R:R < 1.45")
        self.assertEqual(sig.approved_status, "REJECTED")
        self.assertTrue(any("R:R 미달" in r for r in sig.rejection_reasons))
        print("[Fail-Safe PASS 6] Re-fetch 후 R:R 1.0 < 1.45 미달 감지 시 주문 차단 확인")

    def test_07_failsafe_refetch_spread_excessive_strictly_blocks(self):
        """Fail-Safe 4: Re-fetch 후 스프레드가 예산(0.15%)을 초과한 경우 주문 차단"""
        router = OrderRouter(namu_client=self.client, circuit_breaker=self.cb)
        
        stale_time = self.now - timedelta(seconds=3.2)
        stale_quote = OrderQuoteSnapshot(
            symbol="005930",
            current_price=70000,
            bid=69500,
            ask=70000,
            quote_time=stale_time.strftime("%H:%M:%S"),
            quote_timestamp=stale_time,
            received_at=stale_time,
            api_latency_ms=50.0,
            quote_data_age_ms=3200.0,
            order_quote_age_ms=3200.0,
            is_fresh=False
        )

        sig = TradeSignal(
            strategy_id="INT_BREAKOUT",
            time_horizon=TimeHorizon.INTRADAY,
            iem_cd="005930",
            name="삼성전자",
            side=OrderSide.BUY,
            strategy_price=70000,
            stop_price=69000,
            score=75.0,
            reason="TEST_FAILSAFE",
            timestamp=self.now,
            target_1r=71500,
            target_2r=72500
        )
        sig.quote_snapshot = stale_quote

        # 스프레드: ask 70,000, bid 69,500 -> spread = (70000-69500)/70000 = 0.71% > 0.15% (MAX_SLIPPAGE_BUDGET)
        self.client.set_quote("005930", price=70000, bid=69500, ask=70000, timestamp=self.now - timedelta(milliseconds=50))

        order = router.submit_order(
            signal=sig,
            shares=10,
            order_price=70000,
            balance={"cash": 10_000_000},
            now=self.now
        )

        self.assertIsNone(order, "Order must be blocked when spread > 0.15%")
        self.assertEqual(sig.approved_status, "REJECTED")
        self.assertTrue(any("스프레드 예산 초과" in r for r in sig.rejection_reasons))
        print("[Fail-Safe PASS 7] Re-fetch 후 스프레드(0.71% > 0.15%) 초과 감지 시 주문 차단 확인")


if __name__ == "__main__":
    unittest.main()
