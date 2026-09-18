"""[POSITION TRACKER v1.0] Realistic Position State Machine & Exit Watchdog
(backtester/position_tracker.py)

100% LIVE Parity State Machine for position tracking:
- HARD_STOP (immediate low touch, gap-down execution via ExecutionSimulator)
- TARGET_1R (30% scale-out, Break-Even Stop adjustment)
- TARGET_2R (30% scale-out)
- TRAILING_STOP (Highest - 1.5*ATR or EMA9 breakdown for intraday, 2.5*ATR for swing)
- TIME_STOP (20-minute holding + stagnation progress < 0.20)
- EOD Force Liquidation (15:10 loss exit, 15:20 all intraday market exit)
"""

import logging
from datetime import datetime, time as dtime
from typing import Dict, List, Optional, Any
from dataclasses import dataclass, field

from core.models import Position, TimeHorizon, OrderSide, OrderType
from core.tick_normalizer import normalize_price
from backtester.execution_simulator import RealisticExecutionSimulator, ExecutionResult, OrderFillStatus

logger = logging.getLogger("PositionTracker")


@dataclass
class ClosedTradeRecord:
    """
    Exit Event (개별 청산 이벤트 단위):
    - 단일 청산 주문(1R Scale-Out, 2R Scale-Out, Trailing Stop 등)의 체결 결과 기록
    """
    trade_id: str
    symbol: str
    name: str
    strategy_id: str
    time_horizon: str
    entry_time: datetime
    exit_time: datetime
    entry_price: float
    exit_price: float
    qty: int
    gross_pnl: float
    fee_amount: float
    tax_amount: float
    slippage_amount: float
    net_pnl: float
    return_pct: float
    r_multiple: float
    exit_reason: str
    holding_minutes: float
    highest_price: float
    lowest_price: float
    is_scale_out: bool = False
    exit_fee: float = 0.0
    entry_metadata: Dict[str, Any] = field(default_factory=dict)


@dataclass
class RoundTripTrade:
    """
    Round Trip Trade (완결 거래 단위):
    - 포지션 진입(Entry)부터 분할 청산(Scale-Out)을 거쳐 잔여 수량이 0이 되는 최종 청산까지 전체 사이클을 1개 거래로 집계
    - Section 5, 6, 7, 8 회계 표준 및 슬리피지/노셔널/아웃라이어 완전 추적
    """
    trade_id: str
    symbol: str
    entry_time: str
    entry_price: float
    initial_qty: int
    initial_stop: float
    initial_risk_per_share: float
    initial_risk_amount: float

    # Notional & Benchmark Accounting (Section 7, 8, 9)
    entry_notional: float
    exit_notional: float
    benchmark_gross_pnl: float
    slippage_cost: float
    slippage_adjustment: float

    scale_out_qty: int
    scale_out_price: float
    scale_out_pnl: float

    target2_qty: int
    target2_price: float
    target2_pnl: float

    final_exit_qty: int
    final_exit_price: float
    final_exit_pnl: float

    gross_pnl: float
    fees: float
    commission: float
    tax: float
    slippage: float
    net_pnl: float
    trade_r: float
    is_outlier: bool = False
    outlier_reason: str = ""


class PositionTracker:
    """
    [Section 5: Event & Trade Taxonomy Definitions]
    1. Order (주문):
       - 매수 또는 매도 의사결정에 의해 생성된 요청 (Side, Type, Requested Price, Requested Qty)
    2. Fill (체결):
       - 호가/유동성 시뮬레이터에 의해 체결된 단위 (Filled Qty, Filled Price, Fee, Tax, Slippage)
       - 한 주문에 대해 완전 체결(FILLED) 또는 부분 체결(PARTIAL_FILL) 발생 가능
    3. Exit Event (개별 청산 이벤트):
       - 포지션의 일부 또는 전량을 청산하는 개별 이벤트 (1R 익절 30%, 2R 익절 30%, Trailing Stop 40%)
    4. Round Trip Trade (완결 거래 단위):
       - Entry Position Created -> Partial Exits -> Final Qty = 0 까지를 하나의 Trade로 집계
       - Trade Count = 1 (체결 이벤트나 청산 이벤트가 여러 개여도 단 1회 거래로 계산)
       - trade_r = net_pnl / initial_risk_amount (부분청산 R과 최종 R을 단순 합산하지 않음)
    """

    def __init__(self, execution_simulator: RealisticExecutionSimulator):
        self.sim = execution_simulator
        self.active_positions: Dict[str, Position] = {}
        self.closed_trades: List[ClosedTradeRecord] = []
        self.round_trip_trades: List[RoundTripTrade] = []
        self.outlier_trades: List[Dict[str, Any]] = []
        self._trade_counter = 0

    @property
    def positions(self) -> Dict[str, Position]:
        return self.active_positions

    def open_position(
        self,
        symbol: str,
        name: str,
        strategy_id: str,
        time_horizon: TimeHorizon,
        qty: int,
        entry_price: float,
        stop_price: float,
        target_1r: float,
        target_2r: float,
        target_3r: float,
        entry_time: datetime,
        entry_fee: float = 0.0,
        entry_slippage: float = 0.0,
        entry_metadata: Optional[Dict[str, Any]] = None
    ) -> Position:
        """Opens a new simulated position."""
        self._trade_counter += 1
        pos_id = f"POS_{symbol}_{int(entry_time.timestamp())}_{self._trade_counter}"

        if stop_price >= entry_price or (entry_price - stop_price) <= 0:
            stop_price = float(normalize_price(int(entry_price * 0.98), OrderSide.BUY))

        r_unit = max(1.0, float(entry_price - stop_price))

        pos = Position(
            position_id=pos_id,
            time_horizon=time_horizon,
            strategy_id=strategy_id,
            iem_cd=symbol,
            name=name,
            qty=qty,
            entry_price=entry_price,
            current_price=entry_price,
            stop_price=int(stop_price),
            target_1r=int(target_1r),
            target_2r=int(target_2r),
            target_3r=int(target_3r),
            r_unit=r_unit,
            initial_risk_amount=r_unit * qty,
            entry_time=entry_time,
            trailing_stop_price=int(stop_price),
            highest_price=int(entry_price),
            status="ACTIVE"
        )
        # Store metadata for RoundTripTrade accounting (Section 5 & 6)
        pos.initial_qty = qty
        pos.initial_stop = float(stop_price)
        pos.initial_risk_per_share = float(r_unit)
        pos.initial_risk_amount = float(r_unit * qty)
        pos.scale_outs = []
        pos.target2_outs = []
        pos.final_exit = None
        pos.entry_fee = entry_fee
        pos.entry_slippage = entry_slippage
        pos.entry_metadata = entry_metadata or {}

        self.active_positions[pos_id] = pos
        return pos

    def update_and_manage(
        self,
        symbol: str,
        bar: Dict[str, Any],
        current_time: datetime,
        atr14: float = 0.0,
        ema9: float = 0.0,
        ma20: float = 0.0
    ) -> List[ClosedTradeRecord]:
        """
        Evaluates open positions against the latest bar and executes exits.
        Evaluates: HARD_STOP, TARGET_1R, TARGET_2R, TRAILING_STOP, TIME_STOP, EOD.
        """
        matching_positions = [
            p for p in list(self.active_positions.values())
            if p.iem_cd == symbol and not p.is_closed
        ]
        if not matching_positions:
            return []

        bar_open = float(bar.get("open", bar.get("close", 0)))
        bar_high = float(bar.get("high", bar_open))
        bar_low = float(bar.get("low", bar_open))
        bar_close = float(bar.get("close", bar_open))

        now_time = current_time.time()
        time_1510 = dtime(15, 10)
        time_1520 = dtime(15, 20)

        newly_closed: List[ClosedTradeRecord] = []

        for pos in matching_positions:
            pos.current_price = bar_close
            pos.highest_price = max(pos.highest_price, int(bar_high))

            # =================================================================
            # A. Intraday Position Management
            # =================================================================
            if pos.time_horizon == TimeHorizon.INTRADAY:
                # 1. EOD Force Liquidation (15:20)
                if now_time >= time_1520:
                    rec = self._execute_full_exit(
                        pos, bar, current_time, "장마감 단타 강제청산 (15:20)",
                        order_type=OrderType.MARKET
                    )
                    newly_closed.append(rec)
                    continue

                # 2. Pre-close Loss Liquidation (15:10 if in loss)
                if now_time >= time_1510 and bar_close < pos.entry_price:
                    rec = self._execute_full_exit(
                        pos, bar, current_time, "장마감 단타 손실정리 (15:10)",
                        order_type=OrderType.MARKET
                    )
                    newly_closed.append(rec)
                    continue

                # 3. Absolute HARD_STOP (Low <= stop_price)
                if bar_low <= pos.stop_price:
                    rec = self._execute_full_exit(
                        pos, bar, current_time, f"스톱로스 도달 ({pos.stop_price:,}원)",
                        order_type=OrderType.MARKET, stop_price=pos.stop_price
                    )
                    newly_closed.append(rec)
                    continue

                # 4. TARGET_1R (+1R Scale-out: 30%)
                just_took_1r = False
                if not pos.target_1r_taken and bar_high >= pos.target_1r:
                    sell_qty = max(1, int(pos.qty * 0.30))
                    rec = self._execute_partial_exit(
                        pos, sell_qty, bar, current_time, "+1R 도달 (30% 익절)",
                        target_price=pos.target_1r
                    )
                    if rec:
                        newly_closed.append(rec)
                    pos.target_1r_taken = True
                    just_took_1r = True
                    # Move stop to Break-Even (Entry Price)
                    pos.stop_price = max(pos.stop_price, int(pos.entry_price))

                # 5. TARGET_2R (+2R Scale-out: 30%)
                if not pos.target_2r_taken and bar_high >= pos.target_2r:
                    sell_qty = max(1, int(pos.qty * 0.30))
                    rec = self._execute_partial_exit(
                        pos, sell_qty, bar, current_time, "+2R 도달 (30% 익절)",
                        target_price=pos.target_2r
                    )
                    if rec:
                        newly_closed.append(rec)
                    pos.target_2r_taken = True

                # 6. Trailing Stop (Highest - 1.5*ATR or EMA9 breakdown)
                # Only evaluate trailing stop on subsequent bars after target_1r was reached
                if pos.target_1r_taken and not just_took_1r:
                    trail_target = int(pos.highest_price - 1.5 * atr14) if atr14 > 0 else pos.stop_price
                    pos.trailing_stop_price = max(pos.trailing_stop_price, trail_target)

                    if bar_low <= pos.trailing_stop_price or (ema9 > 0 and bar_close < ema9):
                        rec = self._execute_full_exit(
                            pos, bar, current_time, "Trailing Stop / EMA9 이탈 청산",
                            order_type=OrderType.MARKET, stop_price=pos.trailing_stop_price
                        )
                        newly_closed.append(rec)
                        continue

                # 7. TIME_STOP (Stagnation > 20 min)
                if self._check_time_stop(pos, bar_close, current_time, atr14):
                    rec = self._execute_full_exit(
                        pos, bar, current_time, "TIME_STOP: 복합 정체 포지션 청산",
                        order_type=OrderType.MARKET
                    )
                    newly_closed.append(rec)
                    continue

            # =================================================================
            # B. Swing Position Management
            # =================================================================
            elif pos.time_horizon == TimeHorizon.SWING:
                # 1. HARD_STOP (Low <= stop_price)
                if bar_low <= pos.stop_price:
                    rec = self._execute_full_exit(
                        pos, bar, current_time, f"스윙 손절 도달 ({pos.stop_price:,}원)",
                        order_type=OrderType.MARKET, stop_price=pos.stop_price
                    )
                    newly_closed.append(rec)
                    continue

                # 2. TARGET_1R (+1R: 20%)
                just_took_1r_swing = False
                if not pos.target_1r_taken and bar_high >= pos.target_1r:
                    sell_qty = max(1, int(pos.qty * 0.20))
                    rec = self._execute_partial_exit(
                        pos, sell_qty, bar, current_time, "스윙 +1R 도달 (20% 익절)",
                        target_price=pos.target_1r
                    )
                    if rec:
                        newly_closed.append(rec)
                    pos.target_1r_taken = True
                    just_took_1r_swing = True
                    pos.stop_price = max(pos.stop_price, int(pos.entry_price))

                # 3. TARGET_2R (+2R: 20%)
                if not pos.target_2r_taken and bar_high >= pos.target_2r:
                    sell_qty = max(1, int(pos.qty * 0.20))
                    rec = self._execute_partial_exit(
                        pos, sell_qty, bar, current_time, "스윙 +2R 도달 (20% 익절)",
                        target_price=pos.target_2r
                    )
                    if rec:
                        newly_closed.append(rec)
                    pos.target_2r_taken = True

                # 4. Trailing Stop (Highest - 2.5*ATR or MA20 breakdown)
                if pos.target_1r_taken and not just_took_1r_swing:
                    trail_target = int(pos.highest_price - 2.5 * atr14) if atr14 > 0 else pos.stop_price
                    pos.trailing_stop_price = max(pos.trailing_stop_price, trail_target)

                    if bar_low <= pos.trailing_stop_price or (ma20 > 0 and bar_close < ma20):
                        rec = self._execute_full_exit(
                            pos, bar, current_time, "스윙 Trailing Stop / MA20 이탈 청산",
                            order_type=OrderType.MARKET, stop_price=pos.trailing_stop_price
                        )
                        newly_closed.append(rec)
                        continue

        return newly_closed

    def _check_time_stop(
        self,
        pos: Position,
        current_price: float,
        current_time: datetime,
        atr14: float = 0.0
    ) -> bool:
        """
        복합 조건 기반 정체 포지션 타임스톱 (TIME_STOP) 평가
        LIVE execution/position_manager.py Section 5와 100% 동일 로직:
        1. 보유 시간 (Holding Duration)
        2. 진입가 대비 가격 변화율 (Price Change since Entry)
        3. 목표가 도달 진척도 (Target 1R Progress)
        4. ATR 정규화 가격 움직임 (Normalized Volatility Movement)
        5. 상태 전이: ACTIVE -> STALE_POSITION (20분) -> TIME_STOP_TRIGGERED (30분)
        """
        if not hasattr(pos, "entry_time") or not isinstance(pos.entry_time, datetime):
            return False

        elapsed_sec = (current_time - pos.entry_time).total_seconds()
        elapsed_min = elapsed_sec / 60.0
        pct_change = abs(current_price - pos.entry_price) / pos.entry_price if pos.entry_price > 0 else 0.0

        # 1R 목표가 도달 진척도 (0.0 ~ 1.0)
        target_dist = (pos.target_1r - pos.entry_price) if pos.target_1r > pos.entry_price else (pos.entry_price * 0.02)
        progress = (current_price - pos.entry_price) / target_dist if target_dist > 0 else 0.0

        # ATR 정규화 이동폭
        ref_atr = atr14 if atr14 > 0 else (pos.r_unit if getattr(pos, 'r_unit', 0) > 0 else (pos.entry_price * 0.015))
        atr_move = abs(current_price - pos.entry_price) / ref_atr if ref_atr > 0 else 0.0

        # [단타 포지션 (INTRADAY)]
        if getattr(pos, "time_horizon", TimeHorizon.INTRADAY) == TimeHorizon.INTRADAY:
            # 1단계: 20분 경과 시 STALE_POSITION 상태 전이 평가
            if elapsed_min >= 20.0 and getattr(pos, "status", "ACTIVE") == "ACTIVE":
                if pct_change <= 0.0030 and progress < 0.20:
                    pos.status = "STALE_POSITION"
                    pos.stale_detected_at = current_time

            # 2단계: 30분 경과 시 복합 타임스톱 실행 평가
            if elapsed_min >= 30.0:
                # 목표가를 향해 유의미하게 상승 중이거나 진척도 35% 이상인 경우 타임스톱 면제
                if progress >= 0.35 or (current_price >= pos.entry_price * 1.008 and current_price >= getattr(pos, "highest_price", current_price) * 0.995):
                    return False

                # 정체 조건 충족: 진척도 25% 미만 AND (가격변화 0.20% 이하 또는 STALE 상태 유지 또는 ATR 대비 0.3 미만)
                is_stagnant = (
                    progress < 0.25 and (
                        pct_change <= 0.0020
                        or getattr(pos, "status", "ACTIVE") == "STALE_POSITION"
                        or atr_move < 0.30
                    )
                )
                if is_stagnant:
                    pos.status = "TIME_STOP_TRIGGERED"
                    pos.time_stop_triggered_at = current_time
                    return True

        # [스윙 포지션 (SWING)]
        elif getattr(pos, "time_horizon", TimeHorizon.INTRADAY) == TimeHorizon.SWING:
            elapsed_days = elapsed_sec / 86400.0
            if elapsed_days >= 3.0:
                if pct_change <= 0.0050 and atr_move < 0.50 and progress < 0.20:
                    pos.status = "TIME_STOP_TRIGGERED"
                    pos.time_stop_triggered_at = current_time
                    return True

        return False

    def _execute_partial_exit(
        self,
        pos: Position,
        qty: int,
        bar: Dict[str, Any],
        current_time: datetime,
        reason: str,
        target_price: Optional[float] = None
    ) -> Optional[ClosedTradeRecord]:
        """Executes a partial exit (Scale-Out)."""
        exec_res = self.sim.simulate_exit_execution(
            symbol=pos.iem_cd,
            qty=min(qty, pos.qty),
            exit_reason=reason,
            bar=bar,
            order_type=OrderType.LIMIT,
            target_price=target_price,
            timestamp=current_time
        )
        if exec_res.status == OrderFillStatus.UNFILLED or exec_res.filled_qty <= 0:
            return None

        filled_qty = exec_res.filled_qty
        exit_price = exec_res.filled_avg_price

        # Pro-rata entry fee for this partial exit (Section 7 & 8)
        init_q = getattr(pos, "initial_qty", pos.qty)
        slice_entry_fee = pos.entry_fee * (filled_qty / init_q) if init_q > 0 else 0.0
        pos.allocated_entry_fee = getattr(pos, "allocated_entry_fee", 0.0) + slice_entry_fee

        total_trade_fee = exec_res.fee_amount + slice_entry_fee
        gross_pnl = (exit_price - pos.entry_price) * filled_qty
        net_pnl = gross_pnl - total_trade_fee - exec_res.tax_amount
        ret_pct = (exit_price - pos.entry_price) / pos.entry_price if pos.entry_price > 0 else 0.0
        r_mult = (exit_price - pos.entry_price) / pos.r_unit if pos.r_unit > 0 else 0.0

        holding_min = (current_time - pos.entry_time).total_seconds() / 60.0

        record = ClosedTradeRecord(
            trade_id=f"SO_{pos.position_id}_{int(current_time.timestamp())}",
            symbol=pos.iem_cd,
            name=pos.name,
            strategy_id=f"{pos.strategy_id}_SCALE_OUT",
            time_horizon=pos.time_horizon.value if hasattr(pos.time_horizon, "value") else str(pos.time_horizon),
            entry_time=pos.entry_time,
            exit_time=current_time,
            entry_price=pos.entry_price,
            exit_price=exit_price,
            qty=filled_qty,
            gross_pnl=gross_pnl,
            fee_amount=total_trade_fee,
            tax_amount=exec_res.tax_amount,
            slippage_amount=exec_res.slippage_amount,
            net_pnl=net_pnl,
            return_pct=ret_pct,
            r_multiple=r_mult,
            exit_reason=reason,
            holding_minutes=holding_min,
            highest_price=float(pos.highest_price),
            lowest_price=float(bar.get("low", exit_price)),
            is_scale_out=True,
            exit_fee=exec_res.fee_amount,
            entry_metadata=getattr(pos, "entry_metadata", {})
        )

        # Track slice for RoundTripTrade (Section 5 & 6)
        slice_data = {
            "qty": filled_qty,
            "price": exit_price,
            "ref_price": float(target_price or exit_price),
            "gross_pnl": gross_pnl,
            "fees": exec_res.fee_amount,
            "tax": exec_res.tax_amount,
            "slippage": exec_res.slippage_amount,
            "net_pnl": net_pnl
        }
        if not hasattr(pos, "scale_outs"):
            pos.scale_outs = []
        if not hasattr(pos, "target2_outs"):
            pos.target2_outs = []

        if "2R" in reason:
            pos.target2_outs.append(slice_data)
        else:
            pos.scale_outs.append(slice_data)

        pos.qty -= filled_qty
        if pos.qty <= 0:
            pos.is_closed = True
            pos.exit_price = exit_price
            pos.exit_time = current_time
            pos.exit_reason = reason
            self.active_positions.pop(pos.position_id, None)
            self._finalize_round_trip(pos)

        self.closed_trades.append(record)
        return record

    def _execute_full_exit(
        self,
        pos: Position,
        bar: Dict[str, Any],
        current_time: datetime,
        reason: str,
        order_type: OrderType = OrderType.MARKET,
        stop_price: Optional[float] = None
    ) -> ClosedTradeRecord:
        """Executes full liquidation of remaining shares."""
        exec_res = self.sim.simulate_exit_execution(
            symbol=pos.iem_cd,
            qty=pos.qty,
            exit_reason=reason,
            bar=bar,
            order_type=order_type,
            stop_price=stop_price,
            timestamp=current_time
        )
        filled_qty = exec_res.filled_qty if exec_res.filled_qty > 0 else pos.qty
        exit_price = exec_res.filled_avg_price if exec_res.filled_avg_price > 0 else float(bar.get("close", pos.entry_price))

        # Remaining entry fee allocated to full exit (Section 7 & 8)
        rem_entry_fee = max(0.0, getattr(pos, "entry_fee", 0.0) - getattr(pos, "allocated_entry_fee", 0.0))
        total_trade_fee = exec_res.fee_amount + rem_entry_fee

        gross_pnl = (exit_price - pos.entry_price) * filled_qty
        net_pnl = gross_pnl - total_trade_fee - exec_res.tax_amount
        ret_pct = (exit_price - pos.entry_price) / pos.entry_price if pos.entry_price > 0 else 0.0
        r_mult = (exit_price - pos.entry_price) / pos.r_unit if pos.r_unit > 0 else 0.0
        holding_min = (current_time - pos.entry_time).total_seconds() / 60.0

        record = ClosedTradeRecord(
            trade_id=f"EXIT_{pos.position_id}",
            symbol=pos.iem_cd,
            name=pos.name,
            strategy_id=pos.strategy_id,
            time_horizon=pos.time_horizon.value if hasattr(pos.time_horizon, "value") else str(pos.time_horizon),
            entry_time=pos.entry_time,
            exit_time=current_time,
            entry_price=pos.entry_price,
            exit_price=exit_price,
            qty=filled_qty,
            gross_pnl=gross_pnl,
            fee_amount=total_trade_fee,
            tax_amount=exec_res.tax_amount,
            slippage_amount=exec_res.slippage_amount,
            net_pnl=net_pnl,
            return_pct=ret_pct,
            r_multiple=r_mult,
            exit_reason=reason,
            holding_minutes=holding_min,
            highest_price=float(pos.highest_price),
            lowest_price=float(bar.get("low", exit_price)),
            is_scale_out=False,
            exit_fee=exec_res.fee_amount,
            entry_metadata=getattr(pos, "entry_metadata", {})
        )

        pos.final_exit = {
            "qty": filled_qty,
            "price": exit_price,
            "ref_price": float(stop_price or bar.get("open", exit_price)),
            "gross_pnl": gross_pnl,
            "fees": exec_res.fee_amount,
            "tax": exec_res.tax_amount,
            "slippage": exec_res.slippage_amount,
            "net_pnl": net_pnl,
            "reason": reason
        }

        pos.qty -= filled_qty
        if pos.qty <= 0:
            pos.is_closed = True
            pos.qty = 0
            pos.exit_price = exit_price
            pos.exit_time = current_time
            pos.exit_reason = reason
            self.active_positions.pop(pos.position_id, None)
            self._finalize_round_trip(pos)

        self.closed_trades.append(record)
        return record

    def _finalize_round_trip(self, pos: Position) -> RoundTripTrade:
        """
        Finalizes an entire Round Trip Trade from entry to full liquidation (Section 5 & 6).
        Calculates unified Trade R = net_pnl / initial_risk_amount without double-counting.
        Full notional and slippage reconciliation (Section 7, 8, 9).
        """
        initial_qty = getattr(pos, "initial_qty", pos.qty)
        initial_stop = getattr(pos, "initial_stop", float(pos.stop_price))
        initial_risk_per_share = getattr(pos, "initial_risk_per_share", float(pos.r_unit))
        initial_risk_amount = getattr(pos, "initial_risk_amount", float(pos.r_unit * initial_qty))

        scale_outs = getattr(pos, "scale_outs", [])
        so_qty = sum(x["qty"] for x in scale_outs)
        so_price = (sum(x["price"] * x["qty"] for x in scale_outs) / so_qty) if so_qty > 0 else 0.0
        so_pnl = sum(x["gross_pnl"] for x in scale_outs)

        target2s = getattr(pos, "target2_outs", [])
        t2_qty = sum(x["qty"] for x in target2s)
        t2_price = (sum(x["price"] * x["qty"] for x in target2s) / t2_qty) if t2_qty > 0 else 0.0
        t2_pnl = sum(x["gross_pnl"] for x in target2s)

        f_exit = getattr(pos, "final_exit", None) or {}
        fn_qty = f_exit.get("qty", 0)
        fn_price = f_exit.get("price", 0.0)
        fn_pnl = f_exit.get("gross_pnl", 0.0)

        all_exits = scale_outs + target2s + ([f_exit] if f_exit else [])

        # Notional and Accounting Reconciliation (Section 7, 8)
        entry_notional = float(pos.entry_price * initial_qty)
        exit_notional = float(sum(x["price"] * x["qty"] for x in all_exits))
        gross_pnl = float(exit_notional - entry_notional)

        # Benchmark Gross PnL (Theoretical / Reference price based)
        benchmark_exit_notional = float(sum(x.get("ref_price", x["price"]) * x["qty"] for x in all_exits))
        benchmark_gross_pnl = float(benchmark_exit_notional - entry_notional)

        # Slippage Cost & Adjustment:
        # slippage_cost > 0: adverse slippage (execution drag)
        # slippage_cost < 0: price improvement
        slippage_cost = float(benchmark_gross_pnl - gross_pnl)
        slippage_adjustment = float(- slippage_cost)
        # Exact identity: gross_pnl == benchmark_gross_pnl + slippage_adjustment

        entry_fee = getattr(pos, "entry_fee", 0.0)
        commission = float(sum(x.get("fees", 0.0) for x in all_exits) + entry_fee)
        tax = float(sum(x.get("tax", 0.0) for x in all_exits))
        net_pnl = float(gross_pnl - commission - tax)
        trade_r = float(net_pnl / initial_risk_amount) if initial_risk_amount > 0 else 0.0

        # Section 12: Extreme R Outlier Detection
        is_outlier = abs(trade_r) > 10.0
        outlier_reason = ""
        if is_outlier:
            outlier_reason = f"EXTREME_R_OUTLIER (|trade_r|={abs(trade_r):.2f}R > 10R)"
            logger.warning(
                f"[EXTREME_R_OUTLIER] Trade {pos.position_id} ({pos.iem_cd}) "
                f"R={trade_r:+.2f}R, Net PnL={net_pnl:+,.0f}원, Risk Amount={initial_risk_amount:,.0f}원"
            )
            exit_time_val = pos.exit_time if hasattr(pos, "exit_time") and isinstance(pos.exit_time, datetime) else datetime.now()
            entry_time_val = pos.entry_time if hasattr(pos, "entry_time") and isinstance(pos.entry_time, datetime) else exit_time_val
            holding_min = (exit_time_val - entry_time_val).total_seconds() / 60.0

            self.outlier_trades.append({
                "trade_id": pos.position_id,
                "symbol": pos.iem_cd,
                "entry": float(pos.entry_price),
                "stop": float(initial_stop),
                "exit": float(f_exit.get("price", pos.entry_price)),
                "holding_period": round(holding_min, 1),
                "gap": 0.0,
                "trade_r": round(trade_r, 4),
                "reason": outlier_reason
            })

        rt = RoundTripTrade(
            trade_id=pos.position_id,
            symbol=pos.iem_cd,
            entry_time=pos.entry_time.isoformat() if isinstance(pos.entry_time, datetime) else str(pos.entry_time),
            entry_price=round(float(pos.entry_price), 2),
            initial_qty=int(initial_qty),
            initial_stop=round(float(initial_stop), 2),
            initial_risk_per_share=round(float(initial_risk_per_share), 2),
            initial_risk_amount=round(float(initial_risk_amount), 2),
            entry_notional=entry_notional,
            exit_notional=exit_notional,
            benchmark_gross_pnl=benchmark_gross_pnl,
            slippage_cost=slippage_cost,
            slippage_adjustment=slippage_adjustment,
            scale_out_qty=int(so_qty),
            scale_out_price=round(float(so_price), 2),
            scale_out_pnl=round(float(so_pnl), 2),
            target2_qty=int(t2_qty),
            target2_price=round(float(t2_price), 2),
            target2_pnl=round(float(t2_pnl), 2),
            final_exit_qty=int(fn_qty),
            final_exit_price=round(float(fn_price), 2),
            final_exit_pnl=round(float(fn_pnl), 2),
            gross_pnl=float(gross_pnl),
            fees=float(commission),
            commission=float(commission),
            tax=float(tax),
            slippage=float(slippage_cost),
            net_pnl=float(net_pnl),
            trade_r=round(float(trade_r), 4),
            is_outlier=is_outlier,
            outlier_reason=outlier_reason
        )
        self.round_trip_trades.append(rt)
        return rt

    def get_r_distribution_stats(self) -> Dict[str, float]:
        """
        Section 13: Computes complete R distribution statistics:
        Mean R, Median R, P25, P75, P95, Max, Min.
        """
        r_vals = sorted([rt.trade_r for rt in self.round_trip_trades])
        if not r_vals:
            return {
                "mean_r": 0.0, "median_r": 0.0, "p25_r": 0.0,
                "p75_r": 0.0, "p95_r": 0.0, "max_r": 0.0, "min_r": 0.0
            }
        n = len(r_vals)
        mean_r = sum(r_vals) / n
        median_r = r_vals[n // 2]
        p25_r = r_vals[int(n * 0.25)]
        p75_r = r_vals[int(n * 0.75)]
        p95_r = r_vals[min(n - 1, int(n * 0.95))]
        max_r = max(r_vals)
        min_r = min(r_vals)
        return {
            "mean_r": round(mean_r, 4),
            "median_r": round(median_r, 4),
            "p25_r": round(p25_r, 4),
            "p75_r": round(p75_r, 4),
            "p95_r": round(p95_r, 4),
            "max_r": round(max_r, 4),
            "min_r": round(min_r, 4)
        }

    def serialize_state(self) -> Dict[str, Any]:
        """백테스터 포지션 트래커의 활성 포지션 상태 직렬화"""
        pos_data = {}
        for pid, p in self.positions.items():
            pos_data[pid] = {
                "position_id": p.position_id,
                "iem_cd": p.iem_cd,
                "name": p.name,
                "time_horizon": p.time_horizon.value if hasattr(p.time_horizon, "value") else str(p.time_horizon),
                "strategy_id": p.strategy_id,
                "qty": p.qty,
                "remaining_qty": p.qty,
                "entry_price": float(p.entry_price),
                "current_price": float(p.current_price),
                "stop_price": int(p.stop_price),
                "target_1r": int(p.target_1r),
                "target_2r": int(p.target_2r),
                "target_3r": int(getattr(p, "target_3r", 0)),
                "trailing_stop_price": int(p.trailing_stop_price),
                "highest_price": int(p.highest_price),
                "r_unit": float(p.r_unit),
                "initial_risk_amount": float(getattr(p, "initial_risk_amount", 0.0)),
                "realized_pnl": float(getattr(p, "realized_pnl", 0.0)),
                "target_1r_taken": bool(p.target_1r_taken),
                "target_2r_taken": bool(p.target_2r_taken),
                "target_3r_taken": bool(getattr(p, "target_3r_taken", False)),
                "is_closed": bool(p.is_closed),
                "status": getattr(p, "status", "ACTIVE"),
                "entry_time": p.entry_time.isoformat() if isinstance(p.entry_time, datetime) else str(p.entry_time)
            }
        return {
            "positions": pos_data,
            "closed_count": len(self.closed_trades),
            "trades_count": len(self.round_trip_trades)
        }

    def restore_state(self, state: Dict[str, Any]) -> int:
        """직렬화된 상태로부터 백테스터 활성 포지션 복원 (유령 포지션 원천 차단)"""
        restored_count = 0
        pos_data = state.get("positions", {})
        for pid, data in pos_data.items():
            if data.get("is_closed") or data.get("qty", 0) <= 0:
                continue

            th_val = data.get("time_horizon", "INTRADAY")
            th = TimeHorizon.SWING if "SWING" in str(th_val).upper() else TimeHorizon.INTRADAY

            entry_t = datetime.now()
            if data.get("entry_time"):
                try:
                    entry_t = datetime.fromisoformat(data["entry_time"])
                except Exception:
                    pass

            pos = Position(
                position_id=data["position_id"],
                time_horizon=th,
                strategy_id=data.get("strategy_id", "RECOVERED"),
                iem_cd=data["iem_cd"],
                name=data.get("name", data["iem_cd"]),
                qty=int(data["qty"]),
                entry_price=float(data["entry_price"]),
                current_price=float(data.get("current_price", data["entry_price"])),
                stop_price=int(data["stop_price"]),
                target_1r=int(data["target_1r"]),
                target_2r=int(data["target_2r"]),
                target_3r=int(data.get("target_3r", 0)),
                r_unit=float(data.get("r_unit", 1.0)),
                initial_risk_amount=float(data.get("initial_risk_amount", 0.0)),
                entry_time=entry_t,
                trailing_stop_price=int(data.get("trailing_stop_price", data["stop_price"])),
                highest_price=int(data.get("highest_price", data["entry_price"]))
            )
            pos.realized_pnl = float(data.get("realized_pnl", 0.0))
            pos.target_1r_taken = bool(data.get("target_1r_taken", False))
            pos.target_2r_taken = bool(data.get("target_2r_taken", False))
            pos.target_3r_taken = bool(data.get("target_3r_taken", False))
            pos.is_closed = False
            pos.status = data.get("status", "ACTIVE")

            self.positions[pid] = pos
            restored_count += 1

        return restored_count
