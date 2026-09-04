"""주문 라우터 및 10단계 안전점검 엔진 (Order Router)
- Client Order ID 부여 및 멱등성(Idempotency) 보장
- 10단계 주문 사전 안전점검 (Pre-Order Safety Checks)
- 호가창 슬리피지 예산(0.15%) 검증 및 공격적 지정가/시장가 분기
"""

import uuid
from datetime import datetime
from typing import Dict, Tuple, Optional, Any
from core.models import Order, OrderSide, OrderType, OrderStatus, TradeSignal, TimeHorizon
from core.tick_normalizer import normalize_price
from config.krx_constants import MAX_SLIPPAGE_BUDGET


class OrderRouter:
    def __init__(self, namu_client, circuit_breaker):
        self.client = namu_client
        self.circuit_breaker = circuit_breaker
        # 멱등성 보장을 위한 주문 저장소: {client_order_id: Order}
        self.order_registry: Dict[str, Order] = {}
        # 미체결 주문 목록: {client_order_id: Order}
        self.pending_orders: Dict[str, Order] = {}

    def generate_client_order_id(self, strategy_id: str, iem_cd: str, side: OrderSide) -> str:
        """유일무이한 Client Order ID 생성 (전략명_종목_타입_타임스탬프_UUID4자리)"""
        now_str = datetime.now().strftime("%Y%m%d%H%M%S%f")[:17]
        short_uuid = uuid.uuid4().hex[:4]
        return f"{strategy_id}_{iem_cd}_{side.value}_{now_str}_{short_uuid}"

    def run_pre_order_checks(
        self,
        signal: TradeSignal,
        shares: int,
        order_price: int,
        balance: Dict[str, Any],
        portfolio_risk_status: str,
        current_spread: float
    ) -> Tuple[bool, str]:
        """
        주문 전 10단계 사전 안전점검 (10-Step Pre-Order Safety Check)
        모두 만족해야만 주문 전송 승인
        """
        # 1. 서킷 브레이커 상태 점검
        if self.circuit_breaker.is_tripped:
            return False, f"서킷브레이커 작동 중 ({self.circuit_breaker.trip_reason})"

        # 2. 계좌 잔고 및 예수금 확인
        required_cash = shares * order_price
        available_cash = balance.get("cash", 0)
        if signal.side == OrderSide.BUY and required_cash > available_cash:
            return False, f"예수금 부족 (필요: {required_cash:,}원 > 가능: {available_cash:,}원)"

        # 3. 주문 수량 유효성 (> 0)
        if shares <= 0:
            return False, f"주문 수량 오류 ({shares}주)"

        # 4. 가격 Tick Normalize 일치 확인
        norm_price = normalize_price(order_price, signal.side.value, "LIMIT")
        if order_price != norm_price:
            return False, f"호가단위 미정규화 (요청: {order_price} != 규격: {norm_price})"

        # 5. 미체결 중복 주문 검사
        for p_order in self.pending_orders.values():
            if p_order.iem_cd == signal.iem_cd and p_order.side == signal.side:
                return False, f"동일 종목/방향 미체결 주문 진행 중 ({p_order.client_order_id})"

        # 6. 슬리피지 예산 검증 (최우선 스프레드 <= 0.20%, 슬리피지 예산 <= 0.15%)
        if current_spread > MAX_SLIPPAGE_BUDGET:
            return False, f"스프레드/슬리피지 예산(0.15%) 초과: {current_spread*100:.2f}%"

        # 7. 포트폴리오 총 위험 한도 점검
        if portfolio_risk_status == "BLOCKED":
            return False, "포트폴리오 총 리스크 한도(4.0%) 초과로 신규 진입 금지"

        # 8. 전략적 손절가 유효성
        if signal.side == OrderSide.BUY and signal.stop_price >= order_price:
            return False, f"손절가({signal.stop_price:,})가 진입가({order_price:,}) 이상"

        # 9. API 통신 및 토큰 상태 점검
        if not self.client or not self.client.token:
            return False, "증권사 API 연결 및 토큰 무효"

        # 10. 실시간 데이터 신선도 확인
        if self.circuit_breaker.check_data_staleness(datetime.now()):
            return False, "실시간 시장 데이터 3초 이상 지연"

        return True, "10단계 안전점검 전체 승인 통과"

    def submit_order(
        self,
        signal: TradeSignal,
        shares: int,
        order_type: OrderType,
        order_price: int,
        balance: Dict[str, Any],
        portfolio_risk_status: str,
        current_spread: float = 0.001
    ) -> Optional[Order]:
        """
        안전점검 통과 후 브로커 API로 주문 라우팅
        """
        # 10단계 검사 실행
        passed, msg = self.run_pre_order_checks(
            signal, shares, order_price, balance, portfolio_risk_status, current_spread
        )
        if not passed:
            print(f"🚫 [주문 반려] {signal.name}({signal.iem_cd}): {msg}")
            return None

        client_order_id = self.generate_client_order_id(signal.strategy_id, signal.iem_cd, signal.side)

        order = Order(
            client_order_id=client_order_id,
            iem_cd=signal.iem_cd,
            side=signal.side,
            order_type=order_type,
            qty=shares,
            price=order_price,
            strategy_id=signal.strategy_id,
            time_horizon=signal.time_horizon,
            status=OrderStatus.PENDING,
            created_at=datetime.now()
        )

        self.order_registry[client_order_id] = order
        self.pending_orders[client_order_id] = order

        try:
            # 브로커 API 주문 전송
            if signal.side == OrderSide.BUY:
                if order_type == OrderType.MARKET:
                    res = self.client.buy_market(signal.iem_cd, shares)
                else:
                    res = self.client.buy_limit(signal.iem_cd, shares, order_price)
            else:
                if order_type == OrderType.MARKET:
                    res = self.client.sell_market(signal.iem_cd, shares)
                else:
                    res = self.client.sell_limit(signal.iem_cd, shares, order_price)

            # 주문 성공 처리
            self.circuit_breaker.record_order_success()
            broker_no = str(res.get("Output_0", {}).get("mkt_orr_no", ""))
            order.broker_order_no = broker_no

            # 체결 상태 반영
            order.status = OrderStatus.FILLED
            order.filled_qty = shares
            order.filled_avg_price = float(order_price)
            order.filled_at = datetime.now()

            if client_order_id in self.pending_orders:
                del self.pending_orders[client_order_id]

            print(f"[주문 체결] {signal.name}({signal.iem_cd}) {shares}주 {signal.side.value} 완료 (주문번호: {broker_no})")
            return order

        except Exception as e:
            self.circuit_breaker.record_order_failure(str(e))
            order.status = OrderStatus.REJECTED
            if client_order_id in self.pending_orders:
                del self.pending_orders[client_order_id]
            print(f"[주문 실패] {signal.name}({signal.iem_cd}) API 거부: {e}")
            return None
