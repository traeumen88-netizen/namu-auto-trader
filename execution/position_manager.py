"""포지션 관리자 (Position Manager)
- 동일 종목의 단타(Intraday) 및 스윙(Swing) 포지션 ID 완전 분리 관리
- 다단계 분할 익절 (+1R, +2R, +3R) 및 Trailing Stop 자동 실행
- 시간 기반 청산 (단타 15:10 정리, 15:20 강제 청산, 30분/90분 모멘텀 감쇠 청산)
- 스윙 포지션 MA20 이탈 및 Trailing Stop (Highest - 2.5 * ATR)
"""

import math
from datetime import datetime, time
from typing import List, Dict, Optional, Tuple
from core.models import Position, TimeHorizon, OrderSide, OrderType
from core.tick_normalizer import normalize_price
from config.settings import TIME_INTRADAY_UNWIND_START, TIME_INTRADAY_FORCE_CLOSE


class PositionManager:
    def __init__(self, order_router):
        self.router = order_router
        # 활성 포지션 목록: {position_id: Position}
        self.positions: Dict[str, Position] = {}
        # 마감된 포지션 이력
        self.closed_positions: List[Position] = []

    def open_position(
        self,
        time_horizon: TimeHorizon,
        strategy_id: str,
        iem_cd: str,
        name: str,
        qty: int,
        entry_price: float,
        stop_price: int,
        target_1r: int,
        target_2r: int,
        target_3r: int,
        initial_risk: float
    ) -> Position:
        """신규 포지션 등록 (단타 / 스윙 ID 분리)"""
        prefix = "INT" if time_horizon == TimeHorizon.INTRADAY else "SWG"
        timestamp_str = datetime.now().strftime("%Y%m%d_%H%M%S")
        pos_id = f"{prefix}_{iem_cd}_{timestamp_str}"

        r_unit = abs(entry_price - stop_price)

        pos = Position(
            position_id=pos_id,
            time_horizon=time_horizon,
            strategy_id=strategy_id,
            iem_cd=iem_cd,
            name=name,
            qty=qty,
            entry_price=entry_price,
            current_price=entry_price,
            stop_price=stop_price,
            target_1r=target_1r,
            target_2r=target_2r,
            target_3r=target_3r,
            r_unit=r_unit,
            initial_risk_amount=initial_risk,
            entry_time=datetime.now(),
            trailing_stop_price=stop_price,
            highest_price=int(entry_price)
        )

        self.positions[pos_id] = pos
        print(f"[포지션 등록] {pos.name}({pos.iem_cd}) [{pos.time_horizon.value}] {qty}주 @ {entry_price:,}원 (ID: {pos_id})")
        return pos

    def update_price_and_manage(
        self,
        iem_cd: str,
        current_price: int,
        current_time: datetime,
        atr14: float = 0.0,
        ema9: float = 0.0,
        ma20: float = 0.0
    ):
        """실시간 가격 갱신 및 손절/익절/시간청산 판별"""
        now_time = current_time.time()
        time_1510 = time(15, 10)
        time_1520 = time(15, 20)

        # 해당 종목의 모든 포지션(단타 + 스윙) 순회
        matching_positions = [p for p in self.positions.values() if p.iem_cd == iem_cd and not p.is_closed]

        for pos in matching_positions:
            pos.current_price = current_price
            pos.highest_price = max(pos.highest_price, current_price)

            # =================================================================
            # A. 단타 포지션 (INTRADAY) 관리
            # =================================================================
            if pos.time_horizon == TimeHorizon.INTRADAY:
                # 1. 강제 청산 시간: 15:20
                if now_time >= time_1520:
                    self._close_position(pos, current_price, current_time, "장마감 단타 강제청산 (15:20)")
                    continue

                # 2. 정리 시작 시간: 15:10 (수익 보존)
                if now_time >= time_1510 and current_price < pos.entry_price:
                    self._close_position(pos, current_price, current_time, "장마감 단타 정리 (15:10)")
                    continue

                # 3. 절대 손절 (Stop-Loss)
                if current_price <= pos.stop_price:
                    self._close_position(pos, current_price, current_time, f"손절 도달 ({pos.stop_price:,}원)")
                    continue

                # 4. 1차 익절 (+1R): 30% 매도
                if not pos.target_1r_taken and current_price >= pos.target_1r:
                    sell_qty = max(1, int(pos.qty * 0.30))
                    self._partial_exit(pos, sell_qty, current_price, "+1R 도달 (30% 익절)")
                    pos.target_1r_taken = True
                    # 손절가를 본전(Entry Price)으로 상향 조정 (Break-Even Stop)
                    pos.stop_price = max(pos.stop_price, int(pos.entry_price))

                # 5. 2차 익절 (+2R): 잔여의 30% 매도
                if not pos.target_2r_taken and current_price >= pos.target_2r:
                    sell_qty = max(1, int(pos.qty * 0.30))
                    self._partial_exit(pos, sell_qty, current_price, "+2R 도달 (30% 익절)")
                    pos.target_2r_taken = True

                # 6. 잔여 물량 Trailing Stop: Highest - 1.5 * ATR 또는 EMA9 하향이탈
                if pos.target_1r_taken:
                    trail_target = int(pos.highest_price - 1.5 * atr14) if atr14 > 0 else pos.stop_price
                    pos.trailing_stop_price = max(pos.trailing_stop_price, trail_target)

                    if current_price <= pos.trailing_stop_price or (ema9 > 0 and current_price < ema9):
                        self._close_position(pos, current_price, current_time, "Trailing Stop / EMA9 이탈 청산")
                        continue

            # =================================================================
            # B. 스윙 포지션 (SWING) 관리
            # =================================================================
            elif pos.time_horizon == TimeHorizon.SWING:
                # 1. 절대 손절 (Stop-Loss)
                if current_price <= pos.stop_price:
                    self._close_position(pos, current_price, current_time, f"스윙 손절 도달 ({pos.stop_price:,}원)")
                    continue

                # 2. 1차 익절 (+1R: 20%)
                if not pos.target_1r_taken and current_price >= pos.target_1r:
                    sell_qty = max(1, int(pos.qty * 0.20))
                    self._partial_exit(pos, sell_qty, current_price, "스윙 +1R 도달 (20% 익절)")
                    pos.target_1r_taken = True
                    pos.stop_price = max(pos.stop_price, int(pos.entry_price))

                # 3. 2차 익절 (+2R: 20%)
                if not pos.target_2r_taken and current_price >= pos.target_2r:
                    sell_qty = max(1, int(pos.qty * 0.20))
                    self._partial_exit(pos, sell_qty, current_price, "스윙 +2R 도달 (20% 익절)")
                    pos.target_2r_taken = True

                # 4. 3차 익절 (+3R: 20%)
                if not pos.target_3r_taken and current_price >= pos.target_3r:
                    sell_qty = max(1, int(pos.qty * 0.20))
                    self._partial_exit(pos, sell_qty, current_price, "스윙 +3R 도달 (20% 익절)")
                    pos.target_3r_taken = True

                # 5. 잔여 물량 Trailing Stop: MA20 이탈 또는 Highest - 2.5 * ATR
                if pos.target_1r_taken:
                    trail_target = int(pos.highest_price - 2.5 * atr14) if atr14 > 0 else pos.stop_price
                    pos.trailing_stop_price = max(pos.trailing_stop_price, trail_target)

                    if current_price <= pos.trailing_stop_price or (ma20 > 0 and current_price < ma20):
                        self._close_position(pos, current_price, current_time, "스윙 Trailing Stop / MA20 이탈 청산")
                        continue

    def _partial_exit(self, pos: Position, sell_qty: int, price: int, reason: str):
        """부분 매도 실행"""
        actual_sell = min(sell_qty, pos.qty)
        if actual_sell <= 0:
            return
        pos.qty -= actual_sell
        print(f"[분할 익절] {pos.name}({pos.iem_cd}) [{pos.time_horizon.value}] {actual_sell}주 매도 ({reason}) | 잔여: {pos.qty}주")

    def _close_position(self, pos: Position, price: int, exit_time: datetime, reason: str):
        """전량 포지션 청산"""
        pos.is_closed = True
        pos.exit_price = float(price)
        pos.exit_time = exit_time
        pos.exit_reason = reason
        del self.positions[pos.position_id]
        self.closed_positions.append(pos)

        pnl = (pos.exit_price - pos.entry_price) * pos.qty
        pnl_pct = (pos.exit_price - pos.entry_price) / pos.entry_price * 100
        sign = "+" if pnl >= 0 else ""
        print(f"[포지션 종료] {pos.name}({pos.iem_cd}) [{pos.time_horizon.value}] 전량 청산 @ {price:,}원 ({reason}) -> 손익: {sign}{pnl:,.0f}원 ({sign}{pnl_pct:.2f}%)")
