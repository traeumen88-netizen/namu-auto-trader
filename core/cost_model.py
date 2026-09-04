"""실행 수준 거래비용(Transaction Cost) 모델
- Broker Fee (위탁수수료)
- Securities Transaction Tax (증권거래세)
- Agricultural Special Tax (농특세)
- Slippage (체결 슬리피지: NORMAL / STRESS 2x / WORST 3x)
- Bid-Ask Spread Cost (스프레드 비용)
"""

from typing import Literal
from config.krx_constants import COST_SCHEDULES, STRESS_SCENARIOS, CostSchedule
from core.models import CostBreakdown, OrderSide


class CostModel:
    def __init__(self, market: str = "KOSPI", custom_schedule: CostSchedule = None):
        self.market = market.upper()
        self.schedule = custom_schedule or COST_SCHEDULES.get(self.market, COST_SCHEDULES["KOSPI"])

    def calculate_cost(
        self,
        side: OrderSide,
        price: float,
        qty: int,
        scenario: Literal["NORMAL", "STRESS", "WORST"] = "NORMAL",
        spread_ratio: float = 0.001
    ) -> CostBreakdown:
        """
        주문 1건에 대한 상세 거래비용 산출
        :param side: BUY 또는 SELL
        :param price: 체결 가격
        :param qty: 체결 수량
        :param scenario: 슬리피지 시나리오 ("NORMAL", "STRESS", "WORST")
        :param spread_ratio: 당시 최우선 호가 스프레드 비율
        :return: CostBreakdown 객체
        """
        trade_amount = float(price * qty)
        if trade_amount <= 0:
            return CostBreakdown(0.0, 0.0, 0.0, 0.0, 0.0, 0.0, scenario)

        # 1. 위탁 수수료 (매수, 매도 양방향 부과)
        broker_fee = trade_amount * self.schedule.broker_fee_rate

        # 2. 거래세 및 농특세 (매도 시에만 부과)
        if side == OrderSide.SELL:
            sec_tax = trade_amount * self.schedule.transaction_tax_rate
            agri_tax = trade_amount * self.schedule.agricultural_tax_rate
        else:
            sec_tax = 0.0
            agri_tax = 0.0

        # 3. 슬리피지 (스트레스 배수 반영)
        mult = STRESS_SCENARIOS.get(scenario, 1.0)
        slippage_cost = trade_amount * (self.schedule.base_slippage_rate * mult)

        # 4. 스프레드 비용 (반호가 체결 가정: spread_ratio / 2)
        spread_cost = trade_amount * (spread_ratio / 2.0)

        total_cost = broker_fee + sec_tax + agri_tax + slippage_cost + spread_cost

        return CostBreakdown(
            broker_fee=broker_fee,
            securities_tax=sec_tax,
            agricultural_tax=agri_tax,
            slippage_cost=slippage_cost,
            spread_cost=spread_cost,
            total_cost=total_cost,
            scenario=scenario
        )

    def calculate_roundtrip_cost(
        self,
        entry_price: float,
        exit_price: float,
        qty: int,
        scenario: Literal["NORMAL", "STRESS", "WORST"] = "NORMAL",
        spread_ratio: float = 0.001
    ) -> float:
        """왕복(Round-trip) 총 거래비용 계산 (매수비용 + 매도비용)"""
        buy_cost = self.calculate_cost(OrderSide.BUY, entry_price, qty, scenario, spread_ratio)
        sell_cost = self.calculate_cost(OrderSide.SELL, exit_price, qty, scenario, spread_ratio)
        return buy_cost.total_cost + sell_cost.total_cost
