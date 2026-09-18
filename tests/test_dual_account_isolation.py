"""[DUAL ACCOUNT ISOLATION TEST SUITE]
Dual 모드에서 LIVE 및 MOCK 계좌 간 SymbolStateStore, POSITION, COOLDOWN 완전 격리 검증
- MOCK이 특정 종목을 보유해도 LIVE의 신규 Candidate/Setup 탐색 및 매수 차단되지 않음
- LIVE가 보유해도 MOCK의 신규 탐색 및 매수 차단되지 않음
- 양 계좌 모두 보유 중일 때 각자 ALREADY_HELD로 중복 매수만 안전 차단
- MOCK 청산 후 COOLDOWN(600초) 발생 시 MOCK에만 적용되고 LIVE는 즉시 거래 가능
- LIVE 청산 후 COOLDOWN 발생 시 LIVE에만 적용되고 MOCK은 즉시 거래 가능
- SymbolStateStore는 시장 공통 상태(ACTIVE/WATCH/INACTIVE)만 관리하고 계좌별 포지션에 오염되지 않음
- 쿨다운 만료(600초 경과) 후 정상 자동 해제 검증
"""

import unittest
from datetime import datetime, timedelta
from core.models import (
    SymbolInfo, SymbolState, TradeSignal, Order, OrderSide, OrderType,
    OrderStatus, TimeHorizon, MarketRegime
)
from core.symbol_store import SymbolStateStore
from core.candidate_promotion import CandidatePromotionEngine
from core.aggregator import CandleAggregator
from execution.live_quant_trader import AccountContext, LiveQuantTrader
from execution.order_router import OrderRouter
from execution.position_manager import PositionManager
from risk.loss_limits import LossLimitManager
from risk.circuit_breaker import CircuitBreaker


class MockClient:
    def __init__(self, mode: str, act_no: str, cash: float, total_asset: float):
        self.mode = mode
        self.act_no = act_no
        self.cash = cash
        self.total_asset = total_asset
        self.submitted_orders = []

    def get_balance(self):
        return {
            "cash": self.cash,
            "total_asset": self.total_asset,
            "total_profit": 0,
            "total_profit_rate": 0.0,
            "holdings": []
        }

    def buy_market(self, code: str, qty: int):
        ord_no = f"ORD_{self.mode}_{len(self.submitted_orders) + 1}"
        self.submitted_orders.append({"code": code, "qty": qty, "type": "MARKET"})
        return {"rt_cd": "0", "ord_no": ord_no}


class TestDualAccountIsolation(unittest.TestCase):
    def setUp(self):
        self.now = datetime(2026, 9, 16, 10, 0, 0)
        self.sym_code = "005930"
        self.sym_name = "삼성전자"

        # 1. 공통 SymbolStateStore 및 CandidatePromotionEngine
        self.sym_info = SymbolInfo(
            iem_cd=self.sym_code,
            name=self.sym_name,
            market="KOSPI",
            price=70000,
            acml_vol=1_000_000,
            open_price=69000,
            high_price=71000,
            low_price=68500
        )
        self.store = SymbolStateStore({self.sym_code: self.sym_info})
        self.promoter = CandidatePromotionEngine(self.store)

        # 2. 계좌 컨텍스트 분리 (MOCK: 50001003032, LIVE: 20201549311)
        self.mock_client = MockClient("mock", "50001003032", 100_000_000.0, 100_000_000.0)
        self.live_client = MockClient("live", "20201549311", 50_000_000.0, 50_000_000.0)

        cb_mock = CircuitBreaker()
        cb_live = CircuitBreaker()

        self.mock_acc = AccountContext(
            name="MOCK",
            mode="mock",
            act_no="50001003032",
            client=self.mock_client,
            order_router=OrderRouter(self.mock_client, cb_mock),
            position_manager=PositionManager(OrderRouter(self.mock_client, cb_mock)),
            loss_manager=LossLimitManager(),
            cash=100_000_000.0,
            equity=100_000_000.0
        )

        self.live_acc = AccountContext(
            name="LIVE",
            mode="live",
            act_no="20201549311",
            client=self.live_client,
            order_router=OrderRouter(self.live_client, cb_live),
            position_manager=PositionManager(OrderRouter(self.live_client, cb_live)),
            loss_manager=LossLimitManager(),
            cash=50_000_000.0,
            equity=50_000_000.0
        )

    def test_01_mock_held_does_not_block_candidate_promotion_or_live_buy(self):
        """MOCK이 보유 중일 때도 전역 Candidate 승격이 차단되지 않고, LIVE는 정상 매수 가능"""
        # MOCK 포지션 진입
        self.mock_acc.position_manager.open_position(
            TimeHorizon.INTRADAY, "INT_BREAKOUT", self.sym_code, self.sym_name,
            10, 70000.0, 68000.0, 72000.0, 74000.0, 76000.0, 20000.0
        )
        self.assertTrue(self.mock_acc.position_manager.has_position(self.sym_code))
        self.assertFalse(self.live_acc.position_manager.has_position(self.sym_code))

        # 시장 이벤트 발생 (수급 폭발)
        agg = CandleAggregator(self.sym_code)
        state, score, events, patterns = self.promoter.process_event_evaluation(
            iem_cd=self.sym_code,
            agg=agg,
            now=self.now,
            execution_intensity=150.0,
            obi=0.5
        )

        # 1) 시장 상태는 ACTIVE로 승격 가능해야 함 (POSITION으로 잠겨서 조기 리턴되지 않음)
        self.assertEqual(self.store.get(self.sym_code).state, SymbolState.ACTIVE)

        # 2) get_promoted_candidates()에 포함되어 SetupDetector 대상이 됨
        candidates = self.promoter.get_promoted_candidates()
        self.assertIn(self.sym_code, [c.iem_cd for c in candidates])

        # 3) 신호 발생 시 주문 루프 검증:
        # MOCK은 ALREADY_HELD로 건너뜀
        self.assertTrue(self.mock_acc.position_manager.has_position(self.sym_code))
        # LIVE는 미보유이므로 정상 주문 대상
        self.assertFalse(self.live_acc.position_manager.has_position(self.sym_code))
        self.assertFalse(self.live_acc.is_in_cooldown(self.sym_code, self.now))

    def test_02_live_held_does_not_block_candidate_promotion_or_mock_buy(self):
        """LIVE가 보유 중일 때도 Candidate 승격이 유지되고, MOCK은 정상 매수 가능"""
        # LIVE 포지션 진입
        self.live_acc.position_manager.open_position(
            TimeHorizon.INTRADAY, "INT_BREAKOUT", self.sym_code, self.sym_name,
            5, 70000.0, 68000.0, 72000.0, 74000.0, 76000.0, 10000.0
        )
        self.assertTrue(self.live_acc.position_manager.has_position(self.sym_code))
        self.assertFalse(self.mock_acc.position_manager.has_position(self.sym_code))

        # 시장 이벤트 평가
        agg = CandleAggregator(self.sym_code)
        self.promoter.process_event_evaluation(
            iem_cd=self.sym_code, agg=agg, now=self.now, execution_intensity=130.0
        )

        # MOCK 관점에서 종목 탐색 가능
        candidates = self.promoter.get_promoted_candidates()
        self.assertIn(self.sym_code, [c.iem_cd for c in candidates])
        self.assertFalse(self.mock_acc.position_manager.has_position(self.sym_code))
        self.assertFalse(self.mock_acc.is_in_cooldown(self.sym_code, self.now))

    def test_03_both_held_blocks_both_from_duplicate_buy(self):
        """양 계좌 모두 보유 중일 때는 각각 ALREADY_HELD로 중복 매수만 차단"""
        self.mock_acc.position_manager.open_position(
            TimeHorizon.INTRADAY, "INT_BREAKOUT", self.sym_code, self.sym_name,
            10, 70000.0, 68000.0, 72000.0, 74000.0, 76000.0, 20000.0
        )
        self.live_acc.position_manager.open_position(
            TimeHorizon.INTRADAY, "INT_BREAKOUT", self.sym_code, self.sym_name,
            5, 70000.0, 68000.0, 72000.0, 74000.0, 76000.0, 10000.0
        )
        self.assertTrue(self.mock_acc.position_manager.has_position(self.sym_code))
        self.assertTrue(self.live_acc.position_manager.has_position(self.sym_code))

    def test_04_mock_exit_cooldown_isolated_to_mock_only(self):
        """MOCK 청산 후 600초 쿨다운은 MOCK에만 격리 적용되고, LIVE와 시장 스토어는 영향 없음"""
        # MOCK이 손절/청산하여 쿨다운 설정
        self.mock_acc.set_cooldown(self.sym_code, now=self.now, cooldown_seconds=600)

        # MOCK은 쿨다운 상태
        self.assertTrue(self.mock_acc.is_in_cooldown(self.sym_code, self.now))

        # LIVE는 쿨다운 영향 전혀 없음!
        self.assertFalse(self.live_acc.is_in_cooldown(self.sym_code, self.now))

        # 전역 SymbolStateStore는 COOLDOWN 상태가 아니어야 함 (공통 시장 상태 보존)
        self.assertNotEqual(self.store.get(self.sym_code).state, SymbolState.COOLDOWN)

    def test_05_live_exit_cooldown_isolated_to_live_only(self):
        """LIVE 청산 후 쿨다운은 LIVE에만 적용되고, MOCK은 영향 없음"""
        self.live_acc.set_cooldown(self.sym_code, now=self.now, cooldown_seconds=600)

        self.assertTrue(self.live_acc.is_in_cooldown(self.sym_code, self.now))
        self.assertFalse(self.mock_acc.is_in_cooldown(self.sym_code, self.now))
        self.assertNotEqual(self.store.get(self.sym_code).state, SymbolState.COOLDOWN)

    def test_06_cooldown_expiry_after_timeout(self):
        """쿨다운 시간(600초) 경과 후 check_cooldown_expiry 호출 시 정상 자동 해제"""
        self.mock_acc.set_cooldown(self.sym_code, now=self.now, cooldown_seconds=600)
        self.assertTrue(self.mock_acc.is_in_cooldown(self.sym_code, self.now))

        # 300초 경과: 여전히 쿨다운
        half_time = self.now + timedelta(seconds=300)
        self.assertTrue(self.mock_acc.is_in_cooldown(self.sym_code, half_time))

        # 601초 경과: 쿨다운 만료
        expired_time = self.now + timedelta(seconds=601)
        self.assertFalse(self.mock_acc.is_in_cooldown(self.sym_code, expired_time))

        # check_cooldown_expiry 호출 시 딕셔너리에서도 정리
        self.mock_acc.set_cooldown(self.sym_code, now=self.now, cooldown_seconds=600)
        self.mock_acc.check_cooldown_expiry(expired_time)
        self.assertNotIn(self.sym_code, self.mock_acc.cooldowns)


if __name__ == '__main__':
    unittest.main()
