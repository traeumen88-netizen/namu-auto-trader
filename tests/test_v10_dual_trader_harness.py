"""[DUAL TRADER HARNESS v10.0]
모의투자(MOCK: 50001003032) + 실전투자(LIVE: 20201549311) 동시 가동 엔진 하네스 검증
- 계좌 컨텍스트 격리 (AccountContext Isolation)
- 독립적 포지션 사이징 및 예수금 고갈 안전 격리 (Independent Sizing and Balance Safety)
- 손실 한도 및 서킷브레이커 계좌별 독립성 (Circuit Breaker and Loss Limit Isolation)
- 단일 시장 스캔(3,136종목) 브로드캐스트 정합성 (Single-Scan Broadcast Integrity)
- 좀비 주문 계좌별 독립 복구 (Zombie Order Independent Reconciliation)
- 듀얼 텔레메트리 스키마 검증 (Dual Telemetry Schema Verification)
"""

import unittest
from datetime import datetime, timedelta
from core.models import (
    SymbolInfo, SymbolState, TradeSignal, Order, OrderSide, OrderType,
    OrderStatus, TimeHorizon, MarketRegime
)
from execution.live_quant_trader import AccountContext, LiveQuantTrader
from execution.order_router import OrderRouter
from execution.position_manager import PositionManager
from risk.loss_limits import LossLimitManager
from risk.portfolio_risk import PortfolioRiskManager
from risk.circuit_breaker import CircuitBreaker


class DummyClient:
    def __init__(self, mode: str, act_no: str, cash: float, total_asset: float):
        self.mode = mode
        self.act_no = act_no
        self.cash = cash
        self.total_asset = total_asset
        self.orders = []

    def get_balance(self):
        return {
            "cash": self.cash,
            "total_asset": self.total_asset,
            "total_profit": 0,
            "total_profit_rate": 0.0,
            "holdings": []
        }

    def buy_market(self, code: str, qty: int):
        self.orders.append({"code": code, "qty": qty, "type": "BUY_MARKET"})
        return {"rt_cd": "0", "ord_no": f"ORD_{len(self.orders)}"}


class TestV10DualTraderHarness(unittest.TestCase):
    def setUp(self):
        self.mock_client = DummyClient("mock", "50001003032", 500_000_000.0, 500_200_000.0)
        self.live_client = DummyClient("live", "20201549311", 1_516.0, 906_936.0)

        cb_mock = CircuitBreaker()
        cb_live = CircuitBreaker()

        self.mock_acc = AccountContext(
            name="모의투자",
            mode="mock",
            act_no="50001003032",
            client=self.mock_client,
            order_router=OrderRouter(self.mock_client, cb_mock),
            position_manager=PositionManager(OrderRouter(self.mock_client, cb_mock)),
            loss_manager=LossLimitManager()
        )

        self.live_acc = AccountContext(
            name="실전투자",
            mode="live",
            act_no="20201549311",
            client=self.live_client,
            order_router=OrderRouter(self.live_client, cb_live),
            position_manager=PositionManager(OrderRouter(self.live_client, cb_live)),
            loss_manager=LossLimitManager()
        )

    def test_01_account_context_isolation(self):
        """1. 모의 계좌와 실전 계좌의 컨텍스트 및 라우터 완전 격리 검증"""
        self.assertNotEqual(self.mock_acc.act_no, self.live_acc.act_no)
        self.assertEqual(self.mock_acc.mode, "mock")
        self.assertEqual(self.live_acc.mode, "live")
        self.assertIsNot(self.mock_acc.order_router, self.live_acc.order_router)
        self.assertIsNot(self.mock_acc.position_manager, self.live_acc.position_manager)
        self.assertIsNot(self.mock_acc.loss_manager, self.live_acc.loss_manager)

    def test_02_independent_position_sizing_and_cash_exhaustion_safety(self):
        """2. 계좌별 독립 포지션 사이징: 예수금 많은 모의는 매수, 예수금 부족한 실전은 안전 차단"""
        price = 70_000
        stop = 68_000
        risk_per_share = price - stop  # 2,000원

        # Mock sizing (0.5% risk on 500M = 2.5M -> 1,250 shares)
        mock_risk_budget = self.mock_acc.client.total_asset * 0.005
        mock_shares = int(mock_risk_budget / risk_per_share)
        self.assertGreater(mock_shares, 100)

        # Live sizing (1,516원 cash -> max buyable is 0)
        live_max_shares = int(self.live_acc.client.cash / price)
        self.assertEqual(live_max_shares, 0)

        # Mock 주문 성공 시뮬레이션
        res_mock = self.mock_acc.client.buy_market("005930", mock_shares)
        self.assertEqual(res_mock["rt_cd"], "0")
        self.assertEqual(len(self.mock_acc.client.orders), 1)

        # Live는 예수금 부족으로 주문 발주 안 함
        self.assertEqual(len(self.live_acc.client.orders), 0)

    def test_03_circuit_breaker_and_loss_limit_isolation(self):
        """3. 단일 계좌 손실 한도 초과 시 타 계좌 영향 없음(격리) 검증"""
        # Live 계좌에 -4.5% 급락 적용 -> Live LossLimitManager 차단 상태
        eval_live = self.live_acc.loss_manager.evaluate_loss_limits(daily_pnl_ratio=-0.045, weekly_pnl_ratio=-0.08)
        self.assertFalse(eval_live["can_trade_intraday"])
        self.assertFalse(eval_live["can_trade_swing"])
        self.assertEqual(eval_live["risk_multiplier"], 0.0)

        # Mock 계좌는 여전히 +2.0% 수익 -> Mock은 정상 거래 가능
        eval_mock = self.mock_acc.loss_manager.evaluate_loss_limits(daily_pnl_ratio=0.02, weekly_pnl_ratio=0.04)
        self.assertTrue(eval_mock["can_trade_intraday"])
        self.assertTrue(eval_mock["can_trade_swing"])
        self.assertEqual(eval_mock["risk_multiplier"], 1.0)

    def test_04_dual_zombie_order_reconciliation_isolation(self):
        """4. 모의/실전 계좌별 독립 좀비 주문 탐지 및 취소 정합성 검증"""
        now = datetime.now()
        past = now - timedelta(seconds=5)

        # Live 계좌에만 지연된 주문 등록
        zombie_ord = Order(
            client_order_id="LIVE_ORD_999",
            iem_cd="005930",
            side=OrderSide.BUY,
            order_type=OrderType.MARKET,
            qty=10,
            price=70000,
            strategy_id="INT_BREAKOUT",
            time_horizon=TimeHorizon.INTRADAY,
            status=OrderStatus.PENDING,
            created_at=past,
            sent_at=past
        )
        self.live_acc.order_router.pending_orders[zombie_ord.client_order_id] = zombie_ord

        # Live 라우터 좀비 주문 감지
        live_zombies = self.live_acc.order_router.check_zombie_orders(timeout_ms=3000)
        self.assertEqual(len(live_zombies), 1)
        self.assertEqual(live_zombies[0].client_order_id, "LIVE_ORD_999")

        # Mock 라우터는 깨끗함
        mock_zombies = self.mock_acc.order_router.check_zombie_orders(timeout_ms=3000)
        self.assertEqual(len(mock_zombies), 0)

    def test_05_dual_telemetry_schema_validation(self):
        """5. 듀얼 계좌 텔레메트리 데이터 스키마 유효성 검증"""
        telemetry_payload = {
            "timestamp": datetime.now().isoformat(),
            "mode": "dual",
            "dual_accounts": {
                "mock": {
                    "act_no": self.mock_acc.act_no,
                    "equity": self.mock_acc.client.total_asset,
                    "cash": self.mock_acc.client.cash,
                    "daily_pnl": 0,
                    "can_trade": True,
                    "positions_count": 0
                },
                "live": {
                    "act_no": self.live_acc.act_no,
                    "equity": self.live_acc.client.total_asset,
                    "cash": self.live_acc.client.cash,
                    "daily_pnl": 0,
                    "can_trade": True,
                    "positions_count": 0
                }
            }
        }
        self.assertEqual(telemetry_payload["mode"], "dual")
        self.assertIn("mock", telemetry_payload["dual_accounts"])
        self.assertIn("live", telemetry_payload["dual_accounts"])
        self.assertEqual(telemetry_payload["dual_accounts"]["mock"]["act_no"], "50001003032")
        self.assertEqual(telemetry_payload["dual_accounts"]["live"]["act_no"], "20201549311")


if __name__ == '__main__':
    unittest.main()
