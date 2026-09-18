"""[FINAL MASTER v16.0] 포지션 관리자 및 독립 스톱로스 워치독 (execution/position_manager.py)
Section 38 ~ 42: Stop Loss Watchdog & Position Lifecycle Engine

- 당일 종목별 단타(Intraday) 및 스윙(Swing) 포지션 ID 완전 분리 관리
- 독립 Stop Loss Watchdog: 실시간 틱 수신 즉시 (current_price <= stop_price) 브로커 시장가 매도 주문 자동 발주
- 3단계 분할 익절 (+1R, +2R, +3R) 시 실제 브로커 매도 주문 발주
- Trailing Stop: Highest - 1.5 * ATR 또는 EMA9 하향이탈 시 자동 전량 청산 주문
- 장마감 단타 청산: 15:10 손실정리, 15:20 강제 전량 시장가 매도 발주
- 프로그램 재시작 시 브로커 실제 잔고 기반 자동 포지션 복구 (Restart Recovery)
"""

import math
import logging
from datetime import datetime, time
from typing import List, Dict, Optional, Tuple, Any
from core.models import Position, TimeHorizon, OrderSide, OrderType, TradeSignal, OrderStatus
from core.tick_normalizer import normalize_price
from config.settings import TIME_INTRADAY_UNWIND_START, TIME_INTRADAY_FORCE_CLOSE
import config.settings as settings
from execution.exit_order_policy import ExitOrderPolicyEngine, ExitReason, ExitOrderPlan

logger = logging.getLogger("PositionManager")


class PositionManager:
    @property
    def router(self):
        return getattr(self, "_router", None)

    @router.setter
    def router(self, value):
        self._router = value
        if value and hasattr(value, "register_fill_listener"):
            value.register_fill_listener(self.on_order_fill)
        if value and hasattr(value, "register_cancel_listener"):
            value.register_cancel_listener(self.on_order_cancel_or_reject)

    def __init__(self, order_router=None, persistence_manager=None, trade_db=None, **kwargs):
        self.router = order_router
        self.persistence_manager = persistence_manager
        self.trading_mode = kwargs.get("trading_mode", "LIVE")
        if trade_db is not None:
            self.trade_db = trade_db
        else:
            try:
                from ml.trade_db import TradeDatabase
                from namu_client import NamuClient
                client = getattr(self.router, "client", None) if hasattr(self, "router") else None
                if client is None or getattr(client, "dry_run", False) or not isinstance(client, NamuClient):
                    self.trade_db = TradeDatabase(":memory:")
                else:
                    self.trade_db = TradeDatabase()
            except Exception:
                self.trade_db = None
        # 활성 포지션 목록: {position_id: Position}
        self.positions: Dict[str, Position] = {}
        # 마감된 포지션 이력
        self.closed_positions: List[Position] = []
        # 최근 청산 내역 저장소 (심볼별 최종 청산 결과: 손실/수익, 손절 여부 등 추적)
        self.last_closed_trades: Dict[str, Dict[str, Any]] = {}
        # [Section 3 & 4] 전략별/종목별 청산 내역 및 재진입 Telemetry 저장소
        self.strategy_closed_trades: Dict[Tuple[str, str], Dict[str, Any]] = {}
        self.trade_history: List[Dict[str, Any]] = []
        self.daily_entry_counts_by_symbol: Dict[str, int] = {}
        self.daily_entry_counts_by_strategy: Dict[Tuple[str, str], int] = {}
        self.daily_stop_counts_by_symbol: Dict[str, int] = {}
        self.daily_stop_counts_by_strategy: Dict[Tuple[str, str], int] = {}
        # 스톱로스 감시 통계
        self.stop_watchdog_stats = {
            "stops_triggered": 0,
            "stops_executed": 0,
            "stops_failed": 0,
            "partial_exits": 0,
            "trailing_stops": 0,
            "market_close_exits": 0,
            "timeouts_handled": 0,
            "time_stops_triggered": 0
        }
        # OrderRouter 체결 이벤트 리스너 등록
        if getattr(self, "router", None) and hasattr(self.router, "register_fill_listener"):
            self.router.register_fill_listener(self.on_order_fill)

    def on_order_fill(self, order, fill_qty: int, fill_price: float, is_full_fill: bool):
        """실제 체결 확인 시점에만 호출되어 내부 수량 차감 및 영구 기록 처리 (Item 8)"""
        if fill_qty <= 0:
            return
        order_side = getattr(order, "side", None)
        side_val = getattr(order_side, "value", str(order_side)).upper()
        if side_val == "SELL":
            matching_positions = [p for p in self.positions.values() if p.iem_cd == order.iem_cd and not p.is_closed]
            if not matching_positions:
                matching_positions = [p for p in self.positions.values() if p.iem_cd == order.iem_cd]

            if matching_positions:
                pos = matching_positions[0]
                # 1. 실제 보유수량 차감
                pos.qty = max(0, pos.qty - fill_qty)
                # 2. 미체결 매도 대기수량 차감
                pos.pending_exit_qty = max(0, getattr(pos, "pending_exit_qty", 0) - fill_qty)

                # 3. 실체결 영구 기록
                if self.persistence_manager:
                    try:
                        mode, act_no = self.get_account_context()
                        use_mode = getattr(pos, 'trading_mode', mode)
                        use_act = getattr(pos, 'account_no', act_no)
                        self.persistence_manager.record_fill(
                            client_order_id=order.client_order_id,
                            iem_cd=pos.iem_cd,
                            side="SELL",
                            qty=fill_qty,
                            price=float(fill_price),
                            trading_mode=use_mode,
                            account_no=use_act
                        )
                    except Exception as e:
                        logger.error(f"실체결 영구 기록 실패: {e}")

                # 4. 전량 체결 시 상태 전이
                if is_full_fill or pos.qty <= 0:
                    pos.active_exit_order_id = None
                    if pos.qty <= 0:
                        pos.is_closed = True
                        pos.status = "POSITION_CLOSED"
                        pos.exit_time = datetime.now()
                        pos.exit_price = float(fill_price)
                        self.last_closed_trades[pos.iem_cd] = {
                            "pnl": (float(fill_price) - pos.entry_price) * fill_qty,
                            "pnl_pct": ((float(fill_price) - pos.entry_price) / pos.entry_price * 100.0) if pos.entry_price > 0 else 0.0,
                            "reason": getattr(pos, "exit_reason", ""),
                            "exit_time": pos.exit_time,
                            "is_stop_loss": float(fill_price) < pos.entry_price
                        }
                        if pos.position_id in self.positions:
                            del self.positions[pos.position_id]
                        if pos not in self.closed_positions:
                            self.closed_positions.append(pos)
                        logger.info(f"[포지션 전량 청산 완료] {pos.name}({pos.iem_cd}) {fill_qty}주 체결 완료 -> 포지션 종료")
                else:
                    pos.status = "PARTIAL_EXITED"
                    logger.info(f"[포지션 부분 체결] {pos.name}({pos.iem_cd}) {fill_qty}주 체결, 잔여 {pos.qty}주")

    def on_order_cancel_or_reject(self, order, reason: str = ""):
        """주문 취소/거부/만료 시 매도 대기수량(pending_exit_qty) 및 active_exit_order_id 해제"""
        if not order:
            return
        order_side = getattr(order, "side", None)
        side_val = getattr(order_side, "value", str(order_side)).upper()
        if side_val == "SELL":
            matching_positions = [p for p in self.positions.values() if p.iem_cd == order.iem_cd and not p.is_closed]
            if not matching_positions:
                matching_positions = [p for p in self.positions.values() if p.iem_cd == order.iem_cd]
            for pos in matching_positions:
                if pos.active_exit_order_id == order.client_order_id:
                    rel_qty = getattr(order, "remaining_qty", order.qty) or order.qty
                    pos.pending_exit_qty = max(0, getattr(pos, "pending_exit_qty", 0) - rel_qty)
                    pos.active_exit_order_id = None
                    if pos.status in ("EXIT_ORDER_SENT", "EXIT_PENDING"):
                        pos.status = "OPEN"
                    logger.info(f"[Exit 주문 취소/거부/만료 해제] {pos.name}({pos.iem_cd}) 대기수량 {rel_qty}주 복구 (사유: {reason})")

    def get_account_context(self) -> Tuple[str, str]:
        """현재 포지션 매니저에 연동된 클라이언트의 계좌 모드 및 번호 반환"""
        if hasattr(self, "trading_mode") and self.trading_mode:
            import config
            act = config.ACCOUNT_LIVE if str(self.trading_mode).lower() == "live" else config.ACCOUNT_MOCK
            return str(self.trading_mode).upper(), str(act)
        client = getattr(self.router, "client", None) if hasattr(self, "router") else None
        if client is None and hasattr(self, "broker"):
            client = self.broker

        from namu_client import NamuClient
        import config
        if client is not None and isinstance(client, NamuClient) and not getattr(client, "dry_run", False):
            mode = getattr(client, "mode", "mock") or "mock"
            act_no = getattr(client, "act_no", "") or ""
            if mode == "live" and act_no != config.ACCOUNT_LIVE:
                mode = "mock"
        else:
            mode = getattr(client, "mode", "mock") if client else "mock"
            act_no = getattr(client, "act_no", "") if client else ""
            if not act_no:
                act_no = config.ACCOUNT_LIVE if str(mode).lower() == "live" else config.ACCOUNT_MOCK
        return str(mode).upper(), str(act_no)

    def open_position(
        self,
        time_horizon: TimeHorizon = TimeHorizon.INTRADAY,
        strategy_id: str = "AI_MOMENTUM",
        iem_cd: str = "",
        name: str = "",
        qty: int = 0,
        entry_price: float = 0.0,
        stop_price: int = 0,
        target_1r: int = 0,
        target_2r: int = 0,
        target_3r: int = 0,
        initial_risk: float = 0.0,
        signal_session: str = "REGULAR",
        entry_session: str = "REGULAR",
        entry_reason: str = "",
        entry_rvol: float = 0.0,
        entry_momentum_3m: float = 0.0,
        entry_vwap_gap: float = 0.0,
        entry_rebound_strength: float = 0.0,
        expected_move_pct: float = 0.0,
        expected_net_r: float = 0.0,
        **kwargs
    ) -> Position:
        """신규 포지션 등록 (단타 / 스윙 ID 분리 및 계좌 식별자 부여, Entry Quality Telemetry 보존)"""
        if qty == 0 and "quantity" in kwargs:
            qty = int(kwargs["quantity"])
        if initial_risk == 0.0:
            initial_risk = abs(entry_price - stop_price) * qty
        entry_t = kwargs.get("entry_time") or datetime.now()

        prefix = "INT" if time_horizon == TimeHorizon.INTRADAY else "SWG"
        timestamp_str = entry_t.strftime("%Y%m%d_%H%M%S")
        pos_id = f"{prefix}_{iem_cd}_{timestamp_str}"

        r_unit = abs(entry_price - stop_price)
        mode, act_no = self.get_account_context()

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
            entry_time=entry_t,
            trailing_stop_price=stop_price,
            highest_price=int(entry_price),
            lowest_price=int(entry_price),
            signal_session=signal_session,
            entry_session=entry_session,
            entry_reason=entry_reason,
            entry_rvol=entry_rvol,
            entry_momentum_3m=entry_momentum_3m,
            entry_vwap_gap=entry_vwap_gap,
            entry_rebound_strength=entry_rebound_strength,
            expected_move_pct=expected_move_pct,
            expected_net_r=expected_net_r
        )
        pos.trading_mode = mode
        pos.account_no = act_no

        self.positions[pos_id] = pos

        # 당일 진입 횟수 누적 (종목별 및 전략별 분리)
        # Section 3 & 4: RESTART_RECOVERY 및 SCALE_OUT은 당일 진입 횟수 누적에서 배제
        target_cd = str(iem_cd).strip()
        is_recovery_or_scale = (
            strategy_id.startswith("RESTART_RECOVERY") or
            strategy_id.startswith("SCALE_OUT") or
            kwargs.get("trade_classification") == "RESTART_RECOVERY" or
            pos_id.startswith("RECOVERED_")
        )
        if not is_recovery_or_scale:
            self.daily_entry_counts_by_symbol[target_cd] = self.daily_entry_counts_by_symbol.get(target_cd, 0) + 1
            self.daily_entry_counts_by_strategy[(target_cd, strategy_id)] = (
                self.daily_entry_counts_by_strategy.get((target_cd, strategy_id), 0) + 1
            )

        print(f"[포지션 등록] [{mode}:{act_no}] {pos.name}({pos.iem_cd}) [{pos.time_horizon.value}] {qty}주 @ {entry_price:,}원 (ID: {pos_id}, 세션: {signal_session}/{entry_session})")
        return pos

    def get_held_codes(self) -> List[str]:
        """현재 활성 보유 종목 코드(iem_cd) 목록 반환 (position_id 및 Position 객체에서 6자리 코드 정밀 추출)"""
        import re
        codes = set()
        for pos_id, p in self.positions.items():
            if getattr(p, "is_closed", False):
                continue
            # 1. Position 객체 내부의 iem_cd 속성 확인
            cd = str(getattr(p, "iem_cd", "")).strip()
            # 2. position_id(예: INT_005930_20260908_101816, RECOVERED_SWG_342870)에서 6자리 숫자 코드 추출
            if not cd or len(cd) != 6 or not cd.isdigit():
                m = re.search(r"(\d{6})", pos_id)
                if m:
                    cd = m.group(1)
            # 3. 6자리 종목코드 규격 확인 후 등록
            if cd and len(cd) == 6 and cd.isdigit():
                codes.add(cd)
        return list(codes)

    def has_position(self, iem_cd: str) -> bool:
        """해당 종목 코드의 활성 포지션 보유 여부 확인 (iem_cd 및 position_id 양방향 검증)"""
        import re
        target_cd = str(iem_cd).strip()
        for pos_id, p in self.positions.items():
            if getattr(p, "is_closed", False):
                continue
            if getattr(p, "iem_cd", "") == target_cd:
                return True
            m = re.search(r"(\d{6})", pos_id)
            if m and m.group(1) == target_cd:
                return True
        return False

    def get_position(self, iem_cd: str) -> Optional[Position]:
        """해당 종목 코드의 활성 포지션 객체 반환 (미보유 시 None)"""
        import re
        target_cd = str(iem_cd).strip()
        for pos_id, p in self.positions.items():
            if getattr(p, "is_closed", False):
                continue
            if getattr(p, "iem_cd", "") == target_cd:
                return p
            m = re.search(r"(\d{6})", pos_id)
            if m and m.group(1) == target_cd:
                return p
        return None

    def sync_from_broker(self, holdings: List[Dict[str, Any]]):
        """
        Section 48: 프로그램 재시작 시 브로커 보유 종목 기반 포지션 자동 복원
        """
        cli = getattr(self.router, "client", None) if hasattr(self, "router") else None
        if cli is None and hasattr(self, "broker"):
            cli = self.broker

        for h in holdings:
            iem_cd = h.get("iem_cd", "")
            qty = int(h.get("qty", 0))
            buy_price = float(h.get("buy_price") or h.get("avg_price") or 0.0)
            now_price = int(h.get("now_price") or h.get("eval_price") or buy_price)
            if buy_price <= 0 and now_price > 0:
                buy_price = float(now_price)
            name = h.get("iem_nm") or h.get("name") or iem_cd
            if not iem_cd or qty <= 0:
                continue
            # 사용자 수동 보유 종목(375820 웰푸드팜 등)은 자동매매/스톱로스 감시 대상에서 안전하게 제외
            if iem_cd == "375820" or "웰푸드팜" in str(name):
                continue

            # 브로커 실시간 매도가능수량 및 미체결 매도수량 정밀 조회
            unfilled_sell_qty = 0
            psbl_qty = qty
            if cli and hasattr(cli, "get_sellable_quantity") and not getattr(cli, "dry_run", False):
                try:
                    s_info = cli.get_sellable_quantity(iem_cd)
                    if s_info:
                        qty = int(s_info.get("holding_qty") or s_info.get("bnc_qty") or qty)
                        unfilled_sell_qty = int(s_info.get("unfilled_sell_qty") or s_info.get("tdt_sll_ny_cns_qty") or 0)
                        psbl_qty = int(s_info.get("sellable_qty") if "sellable_qty" in s_info else s_info.get("sll_pbl_qty", qty - unfilled_sell_qty))
                except Exception as e:
                    logger.warning(f"get_sellable_quantity({iem_cd}) 조회 실패: {e}")

            # 이미 등록되어 있는지 확인
            existing = [p for p in self.positions.values() if p.iem_cd == iem_cd and not p.is_closed]
            if not existing:
                # 주문 기록이 있다면 타임호라이즌(SWING vs INTRADAY) 추론
                time_horizon = TimeHorizon.INTRADAY
                if self.persistence_manager:
                    try:
                        orders = self.persistence_manager.get_orders(limit=100)
                        for o in orders:
                            if o.get("iem_cd") == iem_cd and o.get("side") == "BUY":
                                cid = o.get("client_order_id", "")
                                if cid.startswith("SWG_"):
                                    time_horizon = TimeHorizon.SWING
                                    break
                    except Exception:
                        pass

                # 손절가: 매입가 대비 -2.5% 기준 산정
                stop_price = normalize_price(int(buy_price * 0.975), OrderSide.BUY)
                risk_per_share = buy_price - stop_price
                t1 = normalize_price(int(buy_price + 1.0 * risk_per_share), OrderSide.BUY)
                t2 = normalize_price(int(buy_price + 2.0 * risk_per_share), OrderSide.BUY)
                t3 = normalize_price(int(buy_price + 3.0 * risk_per_share), OrderSide.BUY)

                prefix = "SWG" if time_horizon == TimeHorizon.SWING else "INT"
                pos = Position(
                    position_id=f"RECOVERED_{prefix}_{iem_cd}",
                    time_horizon=time_horizon,
                    strategy_id="RESTART_RECOVERY",
                    iem_cd=iem_cd,
                    name=name,
                    qty=qty,
                    entry_price=buy_price,
                    current_price=now_price,
                    stop_price=stop_price,
                    target_1r=t1,
                    target_2r=t2,
                    target_3r=t3,
                    r_unit=risk_per_share,
                    initial_risk_amount=risk_per_share * qty,
                    entry_time=datetime.now(),
                    trailing_stop_price=stop_price,
                    highest_price=max(now_price, int(buy_price)),
                    trade_classification="RESTART_RECOVERY",
                    broker_psbl_qty=psbl_qty,
                    pending_exit_qty=unfilled_sell_qty,
                    remaining_qty=qty,
                    filled_qty=qty
                )
                if unfilled_sell_qty > 0 and self.router and hasattr(self.router, "pending_orders"):
                    for p_order in self.router.pending_orders.values():
                        if p_order.iem_cd == iem_cd and p_order.side == OrderSide.SELL:
                            pos.active_exit_order_id = p_order.client_order_id
                            break
                self.positions[pos.position_id] = pos
                print(f"[포지션 복구] 브로커 보유 {name}({iem_cd}) [{time_horizon.value}] {qty}주 (가용: {psbl_qty}주, 대기: {unfilled_sell_qty}주) @ {buy_price:,}원 자동 복원 완료 (손절: {stop_price:,}원)")
            else:
                # 기보유 포지션 존재 시 브로커 실보유 수량 및 미체결 매도 수량과 즉시 동기화 (Error 16157 수량 괴리 원천 차단)
                for p in existing:
                    p.broker_psbl_qty = psbl_qty
                    p.pending_exit_qty = unfilled_sell_qty
                    if unfilled_sell_qty > 0 and not p.active_exit_order_id and self.router and hasattr(self.router, "pending_orders"):
                        for p_order in self.router.pending_orders.values():
                            if p_order.iem_cd == iem_cd and p_order.side == OrderSide.SELL:
                                p.active_exit_order_id = p_order.client_order_id
                                break
                tot_pos_qty = sum(p.qty for p in existing)
                if tot_pos_qty != qty:
                    logger.warning(f"[잔고 수량 불일치 동기화] {name}({iem_cd}) 내부 {tot_pos_qty}주 -> 브로커 실보유 {qty}주로 보정")
                    if len(existing) == 1:
                        existing[0].qty = qty
                        if qty <= 0:
                            existing[0].is_closed = True
                            existing[0].status = "POSITION_CLOSED"
                    else:
                        diff = qty - tot_pos_qty
                        existing[-1].qty = max(0, existing[-1].qty + diff)

    sync_broker_positions = sync_from_broker

    def check_stops(
        self,
        price_map: Dict[str, int],
        current_time: Optional[datetime] = None,
        bid_map: Optional[Dict[str, float]] = None,
        ask_map: Optional[Dict[str, float]] = None
    ) -> List[Position]:
        """
        종목별 현재가 딕셔너리를 받아 스톱로스 및 청산 검사 수행 후 청산된 포지션 목록 반환
        """
        now = current_time or datetime.now()
        bid_map = bid_map or {}
        ask_map = ask_map or {}
        closed_before = len(self.closed_positions)
        for code, price in price_map.items():
            self.update_price_and_manage(
                iem_cd=code,
                current_price=int(price),
                current_time=now,
                bid=bid_map.get(code),
                ask=ask_map.get(code)
            )
        return self.closed_positions[closed_before:]

    def is_sell_order_execution_allowed(self, current_time: datetime) -> Tuple[bool, str]:
        """정규장/허용된 거래시간 여부 확인 (장외시간 실제 SELL 주문 전송 차단)
        - 장외시간: 가격 모니터링/Exit 판단/Trigger 기록 허용, 실제 브로커 SELL 발주 금지
        - 정규장(09:00~15:30): 기존 정상 SELL 발주 허용 (15:10, 15:20 EOD 청산 포함)
        """
        # 단위 테스트 및 Dry-run / Offline 모드에서는 시간 제한 면제
        client = getattr(self.router, "client", None) if hasattr(self, "router") else None
        if client is None or getattr(client, "dry_run", False):
            return True, "OK"

        try:
            from core.after_hours_manager import MarketSessionManager, MarketSession
            session = MarketSessionManager.get_market_session(current_time)
            if session != MarketSession.REGULAR:
                return False, f"OUTSIDE_TRADING_HOURS (현재 세션: {session.value})"
            return True, "OK"
        except Exception:
            if current_time.weekday() >= 5 or not (time(9, 0) <= current_time.time() < time(15, 30)):
                return False, "OUTSIDE_TRADING_HOURS"
            return True, "OK"

    def update_price_and_manage(
        self,
        iem_cd: str,
        current_price: int,
        current_time: datetime,
        atr14: float = 0.0,
        ema9: float = 0.0,
        ma20: float = 0.0,
        vwap: float = 0.0,
        momentum_3m: float = 0.0,
        bid: Optional[float] = None,
        ask: Optional[float] = None
    ):
        """실시간 가격 갱신 및 독립 스톱로스/익절/시간청산 판별 및 주문 발주"""
        now_time = current_time.time()
        time_1510 = time(15, 10)
        time_1520 = time(15, 20)
        is_sell_allowed, sell_sess_reason = self.is_sell_order_execution_allowed(current_time)

        target_cd = str(iem_cd).strip()
        matching_positions = [
            p for p in list(self.positions.values())
            if not getattr(p, "is_closed", False) and (
                str(getattr(p, "iem_cd", "")).strip() == target_cd or target_cd in getattr(p, "position_id", "")
            )
        ]

        for pos in matching_positions:
            pos.current_price = current_price
            pos.highest_price = max(pos.highest_price, current_price)
            if getattr(pos, "lowest_price", 0) <= 0:
                pos.lowest_price = min(int(pos.entry_price), current_price)
            else:
                pos.lowest_price = min(pos.lowest_price, current_price)
            if pos.entry_price > 0:
                pos.mfe_pct = (pos.highest_price - pos.entry_price) / pos.entry_price * 100.0
                pos.mae_pct = (pos.lowest_price - pos.entry_price) / pos.entry_price * 100.0
            pos.watchdog_registered = True
            pos.watchdog_last_check_at = current_time

            is_swing = (pos.time_horizon == TimeHorizon.SWING or str(getattr(pos, 'time_horizon', '')).upper() == "SWING")
            is_intraday = not is_swing

            # Telemetry logging as requested by user
            hard_stop_cond = (current_price <= pos.stop_price) if getattr(pos, 'stop_price', 0) > 0 else False
            logger.info(
                f"[STOP_CHECK] stock_code={pos.iem_cd} | position_id={pos.position_id} | "
                f"quantity={pos.qty} | entry_price={pos.entry_price:,.1f} | current_price={current_price:,} | "
                f"stop_price={pos.stop_price:,} | trailing_stop_price={pos.trailing_stop_price:,} | "
                f"position_state={'CLOSED' if getattr(pos, 'is_closed', False) else 'OPEN'} | "
                f"watchdog_registered=True | watchdog_last_check_at={current_time} | "
                f"HARD_STOP_COND={hard_stop_cond}"
            )

            # =================================================================
            # A. 단타 포지션 (INTRADAY) 관리
            # =================================================================
            if is_intraday:
                # 1. 강제 청산 시간: 15:20 ~ 15:30 (정규장 마감 동시호가 전 시장가 전량 매도)
                if time_1520 <= now_time <= time(15, 30):
                    if not is_sell_allowed:
                        logger.info(f"[SESSION_BLOCK] side=SELL reason=OUTSIDE_TRADING_HOURS symbol={pos.iem_cd} condition=MARKET_CLOSE_1520")
                        continue
                    self.stop_watchdog_stats["market_close_exits"] += 1
                    self._close_position(pos, current_price, current_time, "장마감 단타 강제청산 (15:20)", bid=bid, ask=ask)
                    continue

                # 2. 정리 시작 시간: 15:10 ~ 15:20 (손실 중인 종목 사전 정리)
                if time_1510 <= now_time < time_1520 and current_price < pos.entry_price:
                    if not is_sell_allowed:
                        logger.info(f"[SESSION_BLOCK] side=SELL reason=OUTSIDE_TRADING_HOURS symbol={pos.iem_cd} condition=MARKET_CLOSE_1510")
                        continue
                    self.stop_watchdog_stats["market_close_exits"] += 1
                    self._close_position(pos, current_price, current_time, "장마감 단타 손실정리 (15:10)", bid=bid, ask=ask)
                    continue

                # 3. 절대 손절 (Stop-Loss Watchdog - Section 38: 틱 기준 즉시 집행)
                if current_price <= pos.stop_price:
                    self.stop_watchdog_stats["stops_triggered"] += 1
                    if self.persistence_manager and hasattr(self.persistence_manager, "record_stop_trigger"):
                        mode, act_no = self.get_account_context()
                        use_mode = getattr(pos, 'trading_mode', mode)
                        use_act = getattr(pos, 'account_no', act_no)
                        self.persistence_manager.record_stop_trigger(
                            pos.position_id, pos.iem_cd, float(pos.stop_price), float(current_price),
                            trading_mode=use_mode, account_no=use_act
                        )
                    if not is_sell_allowed:
                        logger.info(f"[SESSION_BLOCK] side=SELL reason=OUTSIDE_TRADING_HOURS symbol={pos.iem_cd} condition=HARD_STOP price={current_price:,} stop={pos.stop_price:,}")
                        continue
                    print(f"[스톱로스 발동] {pos.name}({pos.iem_cd}) 현재가 {current_price:,}원 <= 손절가 {pos.stop_price:,}원 (EXIT_TRIGGERED -> SELL 발주)")
                    self._close_position(pos, current_price, current_time, f"스톱로스 도달 ({pos.stop_price:,}원)", bid=bid, ask=ask)
                    continue

                # 4. 1차 익절 (+1R): 30% 매도
                if not pos.target_1r_taken and current_price >= pos.target_1r:
                    if not is_sell_allowed:
                        logger.info(f"[SESSION_BLOCK] side=SELL reason=OUTSIDE_TRADING_HOURS symbol={pos.iem_cd} condition=TARGET_1R price={current_price:,} target={pos.target_1r:,}")
                    else:
                        sell_qty = max(1, int(pos.qty * 0.30))
                        self._partial_exit(pos, sell_qty, current_price, "+1R 도달 (30% 익절)", bid=bid, ask=ask, now=current_time)
                        pos.target_1r_taken = True
                        pos.stop_price = max(pos.stop_price, int(pos.entry_price))

                # 5. 2차 익절 (+2R): 잔여의 30% 매도
                if not pos.target_2r_taken and current_price >= pos.target_2r:
                    if not is_sell_allowed:
                        logger.info(f"[SESSION_BLOCK] side=SELL reason=OUTSIDE_TRADING_HOURS symbol={pos.iem_cd} condition=TARGET_2R price={current_price:,} target={pos.target_2r:,}")
                    else:
                        sell_qty = max(1, int(pos.qty * 0.30))
                        self._partial_exit(pos, sell_qty, current_price, "+2R 도달 (30% 익절)", bid=bid, ask=ask, now=current_time)
                        pos.target_2r_taken = True

                # 6. 잔여 물량 Trailing Stop: Highest - 1.5 * ATR 또는 EMA9 하향이탈
                if pos.target_1r_taken:
                    trail_target = int(pos.highest_price - 1.5 * atr14) if atr14 > 0 else pos.stop_price
                    pos.trailing_stop_price = max(pos.trailing_stop_price, trail_target)

                    if current_price <= pos.trailing_stop_price or (ema9 > 0 and current_price < ema9):
                        if not is_sell_allowed:
                            logger.info(f"[SESSION_BLOCK] side=SELL reason=OUTSIDE_TRADING_HOURS symbol={pos.iem_cd} condition=TRAILING_STOP price={current_price:,} trail={pos.trailing_stop_price:,}")
                            continue
                        self.stop_watchdog_stats["trailing_stops"] += 1
                        self._close_position(pos, current_price, current_time, "Trailing Stop / EMA9 이탈 청산", bid=bid, ask=ask)
                        continue

                # 7. 복합 정체 포지션 감시 및 타임스톱 (Section 5: TIME_STOP)
                if not is_sell_allowed:
                    pass
                elif self._check_time_stop(pos, current_price, current_time, atr14=atr14, vwap=vwap, momentum_3m=momentum_3m, bid=bid, ask=ask):
                    continue

            # =================================================================
            # B. 스윙 포지션 (SWING) 관리
            # =================================================================
            elif is_swing:
                # 1. 절대 손절 (Stop-Loss)
                if current_price <= pos.stop_price:
                    self.stop_watchdog_stats["stops_triggered"] += 1
                    if self.persistence_manager and hasattr(self.persistence_manager, "record_stop_trigger"):
                        mode, act_no = self.get_account_context()
                        use_mode = getattr(pos, 'trading_mode', mode)
                        use_act = getattr(pos, 'account_no', act_no)
                        self.persistence_manager.record_stop_trigger(
                            pos.position_id, pos.iem_cd, float(pos.stop_price), float(current_price),
                            trading_mode=use_mode, account_no=use_act
                        )
                    if not is_sell_allowed:
                        logger.info(f"[SESSION_BLOCK] side=SELL reason=OUTSIDE_TRADING_HOURS symbol={pos.iem_cd} condition=SWING_STOP price={current_price:,} stop={pos.stop_price:,}")
                        continue
                    print(f"[스윙 스톱로스 발동] {pos.name}({pos.iem_cd}) 현재가 {current_price:,}원 <= 손절가 {pos.stop_price:,}원 (EXIT_TRIGGERED -> SELL 발주)")
                    self._close_position(pos, current_price, current_time, f"스윙 손절 도달 ({pos.stop_price:,}원)", bid=bid, ask=ask)
                    continue

                # 2. 1차 익절 (+1R: 20%)
                if not pos.target_1r_taken and current_price >= pos.target_1r:
                    if not is_sell_allowed:
                        logger.info(f"[SESSION_BLOCK] side=SELL reason=OUTSIDE_TRADING_HOURS symbol={pos.iem_cd} condition=SWING_TARGET_1R price={current_price:,} target={pos.target_1r:,}")
                    else:
                        sell_qty = max(1, int(pos.qty * 0.20))
                        self._partial_exit(pos, sell_qty, current_price, "스윙 +1R 도달 (20% 익절)", bid=bid, ask=ask, now=current_time)
                        pos.target_1r_taken = True
                        pos.stop_price = max(pos.stop_price, int(pos.entry_price))

                # 3. 2차 익절 (+2R: 20%)
                if not pos.target_2r_taken and current_price >= pos.target_2r:
                    if not is_sell_allowed:
                        logger.info(f"[SESSION_BLOCK] side=SELL reason=OUTSIDE_TRADING_HOURS symbol={pos.iem_cd} condition=SWING_TARGET_2R price={current_price:,} target={pos.target_2r:,}")
                    else:
                        sell_qty = max(1, int(pos.qty * 0.20))
                        self._partial_exit(pos, sell_qty, current_price, "스윙 +2R 도달 (20% 익절)", bid=bid, ask=ask, now=current_time)
                        pos.target_2r_taken = True

                # 4. 3차 익절 (+3R: 20%)
                if not pos.target_3r_taken and current_price >= pos.target_3r:
                    if not is_sell_allowed:
                        logger.info(f"[SESSION_BLOCK] side=SELL reason=OUTSIDE_TRADING_HOURS symbol={pos.iem_cd} condition=SWING_TARGET_3R price={current_price:,} target={pos.target_3r:,}")
                    else:
                        sell_qty = max(1, int(pos.qty * 0.20))
                        self._partial_exit(pos, sell_qty, current_price, "스윙 +3R 도달 (20% 익절)", bid=bid, ask=ask, now=current_time)
                        pos.target_3r_taken = True

                # 5. 잔여 물량 Trailing Stop: MA20 이탈 또는 Highest - 2.5 * ATR
                if pos.target_1r_taken:
                    trail_target = int(pos.highest_price - 2.5 * atr14) if atr14 > 0 else pos.stop_price
                    pos.trailing_stop_price = max(pos.trailing_stop_price, trail_target)

                    if current_price <= pos.trailing_stop_price or (ma20 > 0 and current_price < ma20):
                        if not is_sell_allowed:
                            logger.info(f"[SESSION_BLOCK] side=SELL reason=OUTSIDE_TRADING_HOURS symbol={pos.iem_cd} condition=SWING_TRAILING_STOP price={current_price:,} trail={pos.trailing_stop_price:,}")
                            continue
                        self.stop_watchdog_stats["trailing_stops"] += 1
                        self._close_position(pos, current_price, current_time, "스윙 Trailing Stop / MA20 이탈 청산", bid=bid, ask=ask)
                        continue

                # 6. 복합 정체 포지션 감시 및 타임스톱 (Section 5: TIME_STOP)
                if not is_sell_allowed:
                    pass
                elif self._check_time_stop(pos, current_price, current_time, atr14=atr14, vwap=vwap, momentum_3m=momentum_3m, bid=bid, ask=ask):
                    continue

    def _check_time_stop(
        self,
        pos: Position,
        current_price: int,
        current_time: datetime,
        atr14: float = 0.0,
        vwap: float = 0.0,
        momentum_3m: float = 0.0,
        bid: Optional[float] = None,
        ask: Optional[float] = None
    ) -> bool:
        """
        복합 조건 기반 정체 포지션 타임스톱 (TIME_STOP) 평가 (Section 5 & Section 9)
        단순 고정시간 청산이 아닌:
        1. 우상향 추세 유지 포지션은 60분 이상 보유 허용 (실증 백테스트 +1.14억원 기여)
        2. 진척도 부진 및 거래량/모멘텀 정체/하락 시에만 엄격하게 타임스톱 집행
        상태 전이: ACTIVE -> STALE_POSITION -> TIME_STOP_TRIGGERED -> EXIT_PENDING -> EXIT_ORDER_SENT -> FILLED -> POSITION_CLOSED
        """
        # 동일 포지션 미체결 Exit 주문이 대기 중이면 중복 발주 차단
        if pos.active_exit_order_id and self.router and pos.active_exit_order_id in self.router.pending_orders:
            return False

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
        if pos.time_horizon == TimeHorizon.INTRADAY:
            # 1단계: 20분 경과 시 STALE_POSITION 상태 전이 평가 (진척 부진 및 수익 미발생 시에만)
            if elapsed_min >= 20.0 and pos.status == "ACTIVE":
                if pct_change <= 0.0030 and progress < 0.20 and current_price <= pos.entry_price:
                    pos.status = "STALE_POSITION"
                    pos.stale_detected_at = current_time
                    logger.info(f"[정체 포지션 감지] {pos.name}({pos.iem_cd}) 20분 경과 가격변동 {pct_change*100:.2f}%, 진척도 {progress*100:.1f}% -> STALE_POSITION 전이")

            # 2단계: 30분 경과 시 복합 타임스톱 실행 평가
            # [Section 9 개선] 추세 유지 중인 건전한 포지션은 60분 이상이라도 타임스톱 면제 (실증 +1.14억원 기여)
            # 타임스톱 면제 조건 (추세가 살아있는 포지션):
            is_trending = (
                (current_price > pos.entry_price and current_price >= pos.highest_price * 0.985) or
                (vwap > 0 and current_price >= vwap and momentum_3m >= 0.0) or
                (progress >= 0.25)
            )
            if is_trending:
                return False

            if elapsed_min >= 30.0:
                # 정체 및 약화 조건 충족 시에만 타임스톱:
                # 진척도 부진 (< 20%) AND (가격변화 극미 0.25% 이하 OR 음수 모멘텀 OR VWAP 하회 OR STALE 상태 OR 손실 상태)
                is_stagnant = (
                    progress < 0.20 and (
                        pct_change <= 0.0025
                        or momentum_3m < 0.0
                        or (vwap > 0 and current_price < vwap * 0.995)
                        or pos.status == "STALE_POSITION"
                        or current_price < pos.entry_price
                        or atr_move < 0.30
                    )
                )
                if is_stagnant:
                    pos.status = "TIME_STOP_TRIGGERED"
                    pos.time_stop_triggered_at = current_time
                    self.stop_watchdog_stats["time_stops_triggered"] = self.stop_watchdog_stats.get("time_stops_triggered", 0) + 1
                    reason = f"단타 정체 타임스톱 (보유 {int(elapsed_min)}분, 변동 {pct_change*100:.2f}%, 진척도 {progress*100:.1f}%, 모멘텀={momentum_3m:+.2f}%)"
                    print(f"[TIME_STOP 발동] {pos.name}({pos.iem_cd}) {reason}")
                    self._close_position(pos, current_price, current_time, reason, bid=bid, ask=ask)
                    return True

        # [스윙 포지션 (SWING)]
        elif pos.time_horizon == TimeHorizon.SWING:
            # 일봉 기준 정체 포지션 평가: 보유 3영업일(약 72시간) 경과 후 변동폭 극미
            elapsed_days = elapsed_sec / 86400.0
            if elapsed_days >= 3.0:
                # 3일간 누적 가격 변화율 0.50% 미만 및 ATR 대비 0.5 미만
                if pct_change <= 0.0050 and atr_move < 0.50 and progress < 0.20:
                    pos.status = "TIME_STOP_TRIGGERED"
                    pos.time_stop_triggered_at = current_time
                    self.stop_watchdog_stats["time_stops_triggered"] = self.stop_watchdog_stats.get("time_stops_triggered", 0) + 1
                    reason = f"스윙 타임스톱 (보유 {elapsed_days:.1f}일, 정체 변동 {pct_change*100:.2f}%)"
                    print(f"[스윙 TIME_STOP 발동] {pos.name}({pos.iem_cd}) {reason}")
                    self._close_position(pos, current_price, current_time, reason, bid=bid, ask=ask)
                    return True

        return False

    def _partial_exit(
        self,
        pos: Position,
        sell_qty: int,
        price: int,
        reason: str,
        bid: Optional[float] = None,
        ask: Optional[float] = None,
        now: Optional[datetime] = None
    ):
        """실제 브로커 매도 주문 발주를 동반한 부분 매도 실행 (Section 1, 5, 6, 8)"""
        now_dt = now or datetime.now()
        # Section 6: 동일 포지션 중복 Exit 주문 방지 (active_exit_order_id)
        if pos.active_exit_order_id and self.router and pos.active_exit_order_id in self.router.pending_orders:
            logger.warning(f"[중복 Exit 방지] {pos.name}({pos.iem_cd}) 이미 미체결 Exit 주문({pos.active_exit_order_id}) 대기 중")
            return

        avail_qty = getattr(pos, "available_qty", max(0, pos.qty - getattr(pos, "pending_exit_qty", 0)))
        actual_sell = min(sell_qty, avail_qty)
        if actual_sell <= 0:
            logger.warning(f"[가용수량 부족] {pos.name}({pos.iem_cd}) 보유 {pos.qty}주 중 {getattr(pos, 'pending_exit_qty', 0)}주 매도대기 중 (가용 {avail_qty}주) -> 매도 스킵")
            return

        # 정규장/허용 거래시간 외 매도 주문 발주 차단 (Section 1: 장외시간 SELL 방지)
        allowed, sess_reason = self.is_sell_order_execution_allowed(now_dt)
        if not allowed:
            logger.info(f"[SESSION_BLOCK] side=SELL reason=OUTSIDE_TRADING_HOURS symbol={pos.iem_cd} desc=PARTIAL_EXIT_BLOCKED ({sess_reason})")
            return

        # Section 1 & 5: Exit Reason별 주문 유형 및 최적가 산출
        exit_plan = ExitOrderPolicyEngine.determine_exit_plan(
            exit_reason=reason,
            qty=actual_sell,
            current_price=float(price),
            bid=bid,
            ask=ask,
            now=now_dt
        )

        # Section 8: 주문 방식 결정 감사 로그 영구 기록
        if self.persistence_manager:
            try:
                self.persistence_manager.record_exit_execution(exit_plan, pos.iem_cd, pos.position_id)
            except Exception as e:
                logger.error(f"Exit 실행 감사 로그 기록 실패: {e}")

        order_price = int(exit_plan.limit_price) if exit_plan.limit_price > 0 else int(price)
        ret_pct = ((float(order_price) - pos.entry_price) / pos.entry_price * 100.0) if pos.entry_price > 0 else 0.0

        exit_sig = TradeSignal(
            strategy_id=f"{pos.strategy_id}_EXIT",
            time_horizon=pos.time_horizon,
            iem_cd=pos.iem_cd,
            name=pos.name,
            side=OrderSide.SELL,
            strategy_price=price,
            stop_price=0,
            score=100.0,
            reason=reason,
            timestamp=now_dt,
            order_type=exit_plan.selected_order_type,
            entry_price=float(pos.entry_price),
            return_pct=round(float(ret_pct), 2)
        )

        # 브로커 실제 매도 주문 발주
        res_order = None
        if self.router:
            res_order = self.router.submit_order(
                signal=exit_sig,
                shares=actual_sell,
                order_type=exit_plan.selected_order_type,
                order_price=order_price,
                now=now_dt
            )

        if res_order:
            pos.active_exit_order_id = res_order.client_order_id
            pos.exit_order_sent_at = now_dt
            if self.persistence_manager:
                try:
                    mode, act_no = self.get_account_context()
                    use_mode = getattr(pos, 'trading_mode', mode)
                    use_act = getattr(pos, 'account_no', act_no)
                    self.persistence_manager.record_order(res_order, trading_mode=use_mode, account_no=use_act)
                except Exception as e:
                    logger.error(f"분할 매도 주문 영구 기록 실패: {e}")

            if res_order.status == OrderStatus.FILLED:
                self.stop_watchdog_stats["partial_exits"] += 1
                self.on_order_fill(res_order, actual_sell, float(order_price), is_full_fill=False)
            elif res_order.status in (OrderStatus.REJECTED, OrderStatus.CANCELLED, OrderStatus.EXPIRED):
                pos.active_exit_order_id = None
                return
            else:
                # ORDER_ACK 또는 PENDING 상태: 미체결 매도 대기수량만 증가, 실제 보유수량(pos.qty)은 체결 전까지 유지!
                pos.pending_exit_qty = getattr(pos, "pending_exit_qty", 0) + actual_sell
                pos.status = "EXIT_PENDING"
                logger.info(f"[분할매도 접수] {pos.name}({pos.iem_cd}) {actual_sell}주 주문 접수 (주문번호: {res_order.broker_order_no}, 체결 대기, pending_exit_qty={pos.pending_exit_qty})")
                return

            # v7.0/v8.0 TradeDatabase 실시간 실현손익 및 분할 매도 체결 영구 기록
            if getattr(self, "trade_db", None):
                try:
                    import json
                    from ml.trade_db import TradeRecord
                    sell_p = float(order_price)
                    trade_pnl = (sell_p - pos.entry_price) * actual_sell
                    ret_pct = ((sell_p - pos.entry_price) / pos.entry_price * 100) if pos.entry_price > 0 else 0.0
                    r_mult = (trade_pnl / (pos.initial_risk * actual_sell)) if getattr(pos, 'initial_risk', 0) > 0 else (1.0 if trade_pnl > 0 else -1.0)
                    category = "PROFIT_TARGET" if trade_pnl > 0 else "NORMAL_STOP"
                    
                    entry_t_str = pos.entry_time.strftime("%Y-%m-%d %H:%M:%S") if hasattr(pos, 'entry_time') and isinstance(pos.entry_time, datetime) else (str(pos.entry_time) if getattr(pos, 'entry_time', None) else datetime.now().strftime("%Y-%m-%d %H:%M:%S"))
                    holding_sec = (datetime.now() - pos.entry_time).total_seconds() if hasattr(pos, 'entry_time') and isinstance(pos.entry_time, datetime) else 0.0

                    client = getattr(self.router, "client", None) if hasattr(self, "router") else None
                    if client is None and hasattr(self, "broker"):
                        client = self.broker

                    from namu_client import NamuClient
                    import config
                    if client is not None and isinstance(client, NamuClient) and not getattr(client, "dry_run", False):
                        mode = getattr(client, "mode", "mock") or "mock"
                        act_no = getattr(client, "act_no", "") or ""
                    else:
                        mode = "mock"
                        act_no = getattr(config, "ACCOUNT_MOCK", "50001003032")

                    t_rec = TradeRecord(
                        trade_id=f"T_{pos.iem_cd}_{int(datetime.now().timestamp())}_{pos.iem_cd}_SCALE_OUT",
                        symbol=pos.iem_cd,
                        symbol_name=pos.name,
                        entry_time=entry_t_str,
                        exit_time=datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                        entry_price=float(pos.entry_price),
                        exit_price=sell_p,
                        shares=actual_sell,
                        pnl=float(trade_pnl),
                        return_pct=float(ret_pct),
                        r_multiple=float(r_mult),
                        exit_reason=reason,
                        bad_trade_category=category,
                        holding_time_seconds=float(holding_sec),
                        setup_name=getattr(pos, "strategy_id", "UNKNOWN"),
                        time_horizon=getattr(pos.time_horizon, "value", str(pos.time_horizon)),
                        market_regime="NORMAL",
                        model_version="v16.0",
                        trading_mode=mode,
                        account_no=act_no
                    )
                    self.trade_db.save_trade(t_rec)
                except Exception as t_err:
                    logger.error(f"분할 매도 실현손익 기록 중 오류: {t_err}")
        else:
            logger.error(f"[분할 매도 발주 실패] {pos.name}({pos.iem_cd}) 주문 거절 -> 내부 수량 차감 취소 및 브로커 잔고 동기화")
            if self.router and hasattr(self.router, "client") and not getattr(self.router.client, "dry_run", False):
                try:
                    b_bal = self.router.client.balance_service.get_balance(
                        self.router.client.act_no, self.router.client.trade_base_url, force_refresh=True
                    ) if hasattr(self.router.client, "balance_service") else self.router.client.get_balance()
                    holdings = {h["iem_cd"]: h for h in b_bal.get("holdings", [])}
                    if pos.iem_cd in holdings:
                        pos.qty = int(holdings[pos.iem_cd].get("qty", 0))
                    else:
                        pos.qty = 0
                        pos.is_closed = True
                        pos.status = "POSITION_CLOSED"
                except Exception as ex:
                    logger.error(f"분할 매도 실패 후 잔고 동기화 오류: {ex}")
            return

    def _close_position(
        self,
        pos: Position,
        price: int,
        exit_time: datetime,
        reason: str,
        bid: Optional[float] = None,
        ask: Optional[float] = None
    ):
        """실제 브로커 매도 주문 발주를 동반한 전량 포지션 청산 (Section 2, 5, 6, 8)"""
        # Section 6: 동일 포지션 중복 Exit 주문 방지 (active_exit_order_id 및 pending_orders 전수 점검)
        if pos.active_exit_order_id and self.router and pos.active_exit_order_id in self.router.pending_orders:
            e_reason = ExitOrderPolicyEngine.parse_exit_reason(reason)
            # Section 4 & 7: 위험 회피 및 장마감 청산은 기존 미체결 주문을 즉각 취소하고 최우선 청산 집행
            if e_reason in (ExitReason.HARD_STOP, ExitReason.EMERGENCY, ExitReason.CIRCUIT_BREAKER, ExitReason.END_OF_DAY, ExitReason.TRAILING_STOP, ExitReason.TREND_BREAK):
                logger.warning(f"[위험회피/장마감 우선] 기존 미체결 Exit({pos.active_exit_order_id}) 즉시 취소 후 {e_reason.value} 집행")
                cancelled_order = self.router.pending_orders.get(pos.active_exit_order_id)
                self.router.cancel_order(pos.active_exit_order_id, reason=f"OVERRIDDEN_BY_{e_reason.value}")
                if cancelled_order:
                    rel_qty = getattr(cancelled_order, "remaining_qty", cancelled_order.qty) or cancelled_order.qty
                    pos.pending_exit_qty = max(0, getattr(pos, "pending_exit_qty", 0) - rel_qty)
                pos.active_exit_order_id = None
            else:
                logger.warning(f"[중복 Exit 방지] {pos.name}({pos.iem_cd}) 이미 미체결 Exit 주문({pos.active_exit_order_id}) 대기 중")
                return

        # 동일 종목에 대해 이미 진행 중인 매도 주문이 있는지 재확인
        if self.router and self.router.pending_orders:
            for po in self.router.pending_orders.values():
                if po.iem_cd == pos.iem_cd and po.side == OrderSide.SELL:
                    logger.warning(f"[중복 Exit 방지] {pos.name}({pos.iem_cd}) 동일 종목 미체결 매도 주문({po.client_order_id}) 진행 중")
                    return

        # 연속 3회 이상 실패한 포지션은 즉각 격리하여 580회 무한 루프 원천 차단
        if getattr(pos, "exit_failure_count", 0) >= 3:
            logger.error(f"[청산 연속 실패 격리] {pos.name}({pos.iem_cd}) 3회 이상 발주 실패 -> 무한 재주문 차단을 위해 포지션 격리 종료")
            pos.is_closed = True
            pos.status = "EXIT_QUARANTINED"
            pos.exit_reason = f"QUARANTINED_AFTER_3_FAILS: {reason}"
            if pos.position_id in self.positions:
                del self.positions[pos.position_id]
            self.closed_positions.append(pos)
            return

        # [Section 5 & 6] 브로커 매도가능수량 및 가용수량(available_qty) 정합성 검증
        broker_psbl = getattr(pos, "broker_psbl_qty", None)
        avail_qty = getattr(pos, "available_qty", max(0, pos.qty - getattr(pos, "pending_exit_qty", 0)))
        if broker_psbl is not None and broker_psbl < avail_qty:
            logger.warning(f"[수량 불일치 클램핑] {pos.name}({pos.iem_cd}) 가용 수량({avail_qty}주) > 브로커 매도가능수량({broker_psbl}주) -> {broker_psbl}주로 클램핑")
            avail_qty = broker_psbl

        if avail_qty <= 0:
            if pos.qty <= 0:
                pos.is_closed = True
                pos.status = "POSITION_CLOSED"
                if pos.position_id in self.positions:
                    del self.positions[pos.position_id]
                self.closed_positions.append(pos)
            else:
                logger.info(f"[청산 대기] {pos.name}({pos.iem_cd}) 가용수량 0 (보유 {pos.qty}주, 대기 {getattr(pos, 'pending_exit_qty', 0)}주) -> 신규 매도 발주 생략")
            return

        exit_qty = avail_qty

        # 정규장/허용 거래시간 외 매도 주문 발주 차단 (Section 1: 장외시간 SELL 방지)
        allowed, sess_reason = self.is_sell_order_execution_allowed(exit_time)
        if not allowed:
            logger.info(f"[SESSION_BLOCK] side=SELL reason=OUTSIDE_TRADING_HOURS symbol={pos.iem_cd} desc=CLOSE_POSITION_BLOCKED ({sess_reason})")
            return

        # Section 2 & 5: Exit Reason별 주문 유형 및 최적가 산출
        exit_plan = ExitOrderPolicyEngine.determine_exit_plan(
            exit_reason=reason,
            qty=exit_qty,
            current_price=float(price),
            bid=bid,
            ask=ask,
            now=exit_time
        )

        # Section 8: 주문 방식 결정 감사 로그 영구 기록
        if self.persistence_manager:
            try:
                self.persistence_manager.record_exit_execution(exit_plan, pos.iem_cd, pos.position_id)
            except Exception as e:
                logger.error(f"Exit 실행 감사 로그 기록 실패: {e}")

        order_price = int(exit_plan.limit_price) if exit_plan.limit_price > 0 else int(price)
        ret_pct = ((float(order_price) - pos.entry_price) / pos.entry_price * 100.0) if pos.entry_price > 0 else 0.0

        exit_sig = TradeSignal(
            strategy_id=f"{pos.strategy_id}_CLOSE",
            time_horizon=pos.time_horizon,
            iem_cd=pos.iem_cd,
            name=pos.name,
            side=OrderSide.SELL,
            strategy_price=price,
            stop_price=0,
            score=100.0,
            reason=reason,
            timestamp=exit_time,
            order_type=exit_plan.selected_order_type,
            entry_price=float(pos.entry_price),
            return_pct=round(float(ret_pct), 2)
        )

        # 브로커 실제 전량 매도 주문 발주
        order_success = False
        res_order = None
        if self.router:
            res_order = self.router.submit_order(
                signal=exit_sig,
                shares=exit_qty,
                order_type=exit_plan.selected_order_type,
                order_price=order_price,
                now=exit_time
            )
            order_success = (res_order is not None)

        if res_order:
            pos.active_exit_order_id = res_order.client_order_id
            pos.exit_order_sent_at = exit_time
            if self.persistence_manager:
                try:
                    mode, act_no = self.get_account_context()
                    use_mode = getattr(pos, 'trading_mode', mode)
                    use_act = getattr(pos, 'account_no', act_no)
                    self.persistence_manager.record_order(res_order, trading_mode=use_mode, account_no=use_act)
                except Exception as e:
                    logger.error(f"청산 매도 주문 영구 기록 실패: {e}")

            if res_order.status == OrderStatus.FILLED:
                self.stop_watchdog_stats["stops_executed"] += 1
            elif res_order.status in (OrderStatus.REJECTED, OrderStatus.CANCELLED, OrderStatus.EXPIRED):
                self.stop_watchdog_stats["stops_failed"] += 1
                pos.active_exit_order_id = None
                return
            else:
                # ORDER_ACK 또는 PENDING 상태: 미체결 매도 대기수량만 증가, 실제 체결 전까지 포지션 유지!
                pos.pending_exit_qty = getattr(pos, "pending_exit_qty", 0) + exit_qty
                pos.status = "EXIT_ORDER_SENT"
                pos.exit_reason = reason
                logger.info(f"[전량청산 접수] {pos.name}({pos.iem_cd}) {exit_qty}주 청산 주문 접수 (주문번호: {res_order.broker_order_no}, 체결 대기, status=EXIT_ORDER_SENT, pending_exit_qty={pos.pending_exit_qty})")
                return
        else:
            self.stop_watchdog_stats["stops_failed"] += 1
            pos.status = "EXIT_FAILED"
            pos.exit_reason = f"ORDER_FAILED: {reason}"
            pos.exit_failure_count = getattr(pos, "exit_failure_count", 0) + 1
            logger.error(f"[청산 주문 실패] {pos.name}({pos.iem_cd}) 청산 발주 실패 (실패 {pos.exit_failure_count}회) -> 브로커 실잔고 즉각 강제 동기화")
            
            # 주문가능수량 부족(16157) 등 브로커-내부 수량 괴리 자동 동기화 (force_refresh=True로 캐시 우회)
            if self.router and hasattr(self.router, "client") and not getattr(self.router.client, "dry_run", False):
                try:
                    b_bal = self.router.client.balance_service.get_balance(
                        self.router.client.act_no, self.router.client.trade_base_url, force_refresh=True
                    ) if hasattr(self.router.client, "balance_service") else self.router.client.get_balance()
                    holdings = {h["iem_cd"]: h for h in b_bal.get("holdings", [])}
                    if pos.iem_cd not in holdings:
                        logger.warning(f"[청산 오류 자동복구] {pos.name}({pos.iem_cd}) 브로커 잔고 미존재 확인 -> 내부 포지션 즉시 종료 처리")
                        pos.is_closed = True
                        pos.status = "POSITION_CLOSED"
                        pos.qty = 0
                        pos.exit_reason = f"BROKER_NOT_FOUND_AFTER_FAIL: {reason}"
                        if pos.position_id in self.positions:
                            del self.positions[pos.position_id]
                        if pos not in self.closed_positions:
                            self.closed_positions.append(pos)
                        return
                    else:
                        real_qty = int(holdings[pos.iem_cd].get("qty", 0))
                        if real_qty <= 0:
                            pos.is_closed = True
                            pos.status = "POSITION_CLOSED"
                            pos.qty = 0
                            pos.exit_reason = f"BROKER_ZERO_QTY_AFTER_FAIL: {reason}"
                            if pos.position_id in self.positions:
                                del self.positions[pos.position_id]
                            if pos not in self.closed_positions:
                                self.closed_positions.append(pos)
                            return
                        elif real_qty != pos.qty:
                            logger.warning(f"[청산 수량 보정] {pos.name}({pos.iem_cd}) 내부 {pos.qty}주 -> 브로커 실보유 {real_qty}주로 즉각 동기화")
                            pos.qty = real_qty
                except Exception as sync_err:
                    logger.error(f"청산 실패 후 브로커 잔고 동기화 중 오류: {sync_err}")

            if pos.exit_failure_count >= 3:
                logger.error(f"[청산 연속 실패 격리] {pos.name}({pos.iem_cd}) 3회 발주 실패 -> 포지션 격리 종료")
                pos.is_closed = True
                pos.status = "EXIT_QUARANTINED"
                if pos.position_id in self.positions:
                    del self.positions[pos.position_id]
                if pos not in self.closed_positions:
                    self.closed_positions.append(pos)
            return

        pos.is_closed = True
        pos.status = "POSITION_CLOSED"
        pos.qty = 0
        pos.exit_price = float(price)
        pos.exit_time = exit_time
        pos.exit_reason = reason
        pos.realized_pnl = (pos.exit_price - pos.entry_price) * exit_qty
        holding_sec = (exit_time - pos.entry_time).total_seconds() if hasattr(pos, 'entry_time') and isinstance(pos.entry_time, datetime) else 0.0

        if pos.position_id in self.positions:
            del self.positions[pos.position_id]
        if pos not in self.closed_positions:
            self.closed_positions.append(pos)

        # v7.0/v8.0 TradeDatabase 실시간 실현손익 및 매도 체결 영구 기록
        if getattr(self, "trade_db", None):
            try:
                import json
                from ml.trade_db import TradeRecord
                trade_pnl = (pos.exit_price - pos.entry_price) * exit_qty
                ret_pct = ((pos.exit_price - pos.entry_price) / pos.entry_price * 100) if pos.entry_price > 0 else 0.0
                r_mult = (trade_pnl / (pos.initial_risk * exit_qty)) if getattr(pos, 'initial_risk', 0) > 0 else (1.0 if trade_pnl > 0 else -1.0)
                category = "PROFIT_TARGET" if trade_pnl > 0 else "NORMAL_STOP"
                
                entry_t_str = pos.entry_time.strftime("%Y-%m-%d %H:%M:%S") if hasattr(pos, 'entry_time') and isinstance(pos.entry_time, datetime) else (str(pos.entry_time) if getattr(pos, 'entry_time', None) else exit_time.strftime("%Y-%m-%d %H:%M:%S"))
                holding_sec = (exit_time - pos.entry_time).total_seconds() if hasattr(pos, 'entry_time') and isinstance(pos.entry_time, datetime) else 0.0

                client = getattr(self.router, "client", None) if hasattr(self, "router") else None
                if client is None and hasattr(self, "broker"):
                    client = self.broker

                from namu_client import NamuClient
                import config
                if client is not None and isinstance(client, NamuClient) and not getattr(client, "dry_run", False):
                    mode = getattr(client, "mode", "mock") or "mock"
                    act_no = getattr(client, "act_no", "") or ""
                    if mode == "live" and act_no != config.ACCOUNT_LIVE:
                        mode = "mock"
                else:
                    mode = "mock"
                    act_no = getattr(client, "act_no", "") if client else ""

                trade_rec = TradeRecord(
                    trade_id=f"T_{pos.iem_cd}_{int(datetime.now().timestamp())}_{pos.position_id[-6:] if pos.position_id else '000000'}",
                    symbol=pos.iem_cd,
                    symbol_name=pos.name or pos.iem_cd,
                    setup_name=pos.strategy_id or "AI_MOMENTUM",
                    time_horizon=pos.time_horizon.value if hasattr(pos.time_horizon, 'value') else str(pos.time_horizon),
                    side="BUY",
                    entry_time=entry_t_str,
                    exit_time=exit_time.strftime("%Y-%m-%d %H:%M:%S"),
                    entry_price=float(pos.entry_price),
                    exit_price=float(pos.exit_price),
                    shares=int(exit_qty),
                    stop_price=float(pos.stop_price or 0),
                    target_price=float(pos.target_1r or 0),
                    pnl=float(trade_pnl),
                    return_pct=round(float(ret_pct), 2),
                    r_multiple=round(float(r_mult), 2),
                    mae_pct=round(float(((pos.lowest_price - pos.entry_price) / pos.entry_price * 100.0) if pos.entry_price > 0 else 0.0), 2),
                    mfe_pct=round(float(((pos.highest_price - pos.entry_price) / pos.entry_price * 100.0) if pos.entry_price > 0 else 0.0), 2),
                    mae_r=0.0,
                    mfe_r=0.0,
                    holding_seconds=holding_sec,
                    model_version="v7.0_champion",
                    p_target_pred=0.70,
                    p_stop_pred=0.20,
                    expected_net_r_pred=0.40,
                    bad_trade_category=category,
                    raw_features=json.dumps({
                        "exit_reason": reason,
                        "entry_reason": getattr(pos, "entry_reason", ""),
                        "entry_rvol": getattr(pos, "entry_rvol", 0.0),
                        "entry_momentum_3m": getattr(pos, "entry_momentum_3m", 0.0),
                        "entry_vwap_gap": getattr(pos, "entry_vwap_gap", 0.0),
                        "entry_rebound_strength": getattr(pos, "entry_rebound_strength", 0.0),
                        "expected_move_pct": getattr(pos, "expected_move_pct", 0.0),
                        "expected_net_r": getattr(pos, "expected_net_r", 0.0),
                    }),
                    exit_reason=reason,
                    trading_mode=str(mode).lower(),
                    account_no=str(act_no),
                    signal_session=getattr(pos, "signal_session", "REGULAR") or "REGULAR",
                    entry_session=getattr(pos, "entry_session", "REGULAR") or "REGULAR",
                    entry_rvol=getattr(pos, "entry_rvol", None),
                    entry_momentum_3m=getattr(pos, "entry_momentum_3m", None),
                    entry_vwap_gap=getattr(pos, "entry_vwap_gap", None),
                    entry_rebound_strength=getattr(pos, "entry_rebound_strength", None),
                    expected_move_pct=getattr(pos, "expected_move_pct", None),
                    expected_net_r=getattr(pos, "expected_net_r", None)
                )
                self.trade_db.record_trade(trade_rec)
            except Exception as e:
                logger.error(f"TradeDatabase 영구 기록 실패: {e}")

        pnl = (pos.exit_price - pos.entry_price) * exit_qty
        pnl_pct = (pos.exit_price - pos.entry_price) / pos.entry_price * 100
        sign = "+" if pnl >= 0 else ""
        print(f"[포지션 종료 및 매도 발주] {pos.name}({pos.iem_cd}) [{pos.time_horizon.value}] {exit_plan.selected_order_type.value} 청산 @ {price:,}원 ({reason}) -> 실현손익: {sign}{pnl:,.0f}원 ({sign}{pnl_pct:.2f}%)")

        # [Section 6, 7, 8 & Section 3, 4, 7] 최근 청산 이력 및 손절 여부 기록 (쿨다운, Churn 및 재진입 Telemetry 추적용)
        is_loss = (pnl < 0) or ("손절" in reason or "스톱로스" in reason or "STOP" in reason.upper())
        mfe_val = ((pos.highest_price - pos.entry_price) / pos.entry_price) if pos.entry_price > 0 else 0.0
        mae_val = ((pos.lowest_price - pos.entry_price) / pos.entry_price) if pos.entry_price > 0 else 0.0
        
        target_cd = str(pos.iem_cd).strip()
        strat_id = str(pos.strategy_id).strip()

        trade_record_dict = {
            "symbol": target_cd,
            "strategy": strat_id,
            "entry_time": pos.entry_time,
            "exit_time": exit_time,
            "reason": reason,
            "exit_reason": reason,
            "entry_price": float(pos.entry_price),
            "exit_price": float(price),
            "qty": exit_qty,
            "pnl": float(pnl),
            "pnl_pct": float(pnl_pct),
            "holding_time": holding_sec,
            "mfe": float(mfe_val),
            "mae": float(mae_val),
            "mfe_pct": float(mfe_val),
            "mae_pct": float(mae_val),
            "is_stop_loss": is_loss,
            # Round-trip entry telemetry
            "entry_reason": getattr(pos, "entry_reason", ""),
            "entry_rvol": getattr(pos, "entry_rvol", 0.0),
            "entry_momentum_3m": getattr(pos, "entry_momentum_3m", 0.0),
            "entry_vwap_gap": getattr(pos, "entry_vwap_gap", 0.0),
            "entry_rebound_strength": getattr(pos, "entry_rebound_strength", 0.0),
            "expected_move": getattr(pos, "expected_move_pct", 0.0),
            "expected_move_pct": getattr(pos, "expected_move_pct", 0.0),
            "expected_net_r": getattr(pos, "expected_net_r", 0.0),
            "holding_seconds": holding_sec,
        }
        is_recovery_or_scale = (
            strat_id.startswith("RESTART_RECOVERY") or
            strat_id.startswith("SCALE_OUT") or
            getattr(pos, "trade_classification", "") == "RESTART_RECOVERY" or
            getattr(pos, "position_id", "").startswith("RECOVERED_")
        )

        if not is_recovery_or_scale:
            self.last_closed_trades[target_cd] = trade_record_dict
            self.strategy_closed_trades[(target_cd, strat_id)] = trade_record_dict
            self.trade_history.append(trade_record_dict)

            if is_loss:
                self.daily_stop_counts_by_symbol[target_cd] = self.daily_stop_counts_by_symbol.get(target_cd, 0) + 1
                self.daily_stop_counts_by_strategy[(target_cd, strat_id)] = (
                    self.daily_stop_counts_by_strategy.get((target_cd, strat_id), 0) + 1
                )

            try:
                from strategies.profit_opportunity_gate import ProfitOpportunityTelemetry
                gross_m = abs(price - pos.entry_price) / pos.entry_price if pos.entry_price > 0 else 0.0
                cost_m = 0.0023
                net_m = (price - pos.entry_price) / pos.entry_price - cost_m if pos.entry_price > 0 else 0.0
                ProfitOpportunityTelemetry.get_instance().record_trade_completion(
                    gross_move=gross_m,
                    net_move=net_m,
                    cost=cost_m,
                    holding_time_sec=holding_sec,
                    actual_mfe=mfe_val,
                    actual_mae=mae_val,
                    pnl=pnl,
                    strategy_id=strat_id
                )
            except Exception:
                pass

    def get_last_closed_trade(self, iem_cd: str) -> Optional[Dict[str, Any]]:
        """해당 종목의 최근 청산 결과 반환"""
        return self.last_closed_trades.get(str(iem_cd).strip())

    def get_strategy_last_closed_trade(self, iem_cd: str, strategy_id: str) -> Optional[Dict[str, Any]]:
        """[Section 4] 동일 종목의 특정 전략별 최근 청산 결과 반환"""
        return self.strategy_closed_trades.get((str(iem_cd).strip(), str(strategy_id).strip()))

    def is_reentry_allowed(
        self,
        symbol: str,
        strategy_id: Optional[str] = None,
        current_time: Optional[datetime] = None
    ) -> Tuple[bool, str]:
        """
        [Section 3 & 4] 재진입 허용 여부 판별:
        1. 당일 손절 횟수 3회 도달 시 영구 차단 (REENTRY_DAILY_LIMIT_EXCEEDED)
        2. 직전 거래 손절 후 15분(900초) 쿨다운 미경과 시 차단 (REENTRY_COOLDOWN)
        3. 전략별/종목별 분리 추적 지원
        """
        now = current_time or datetime.now()
        target_cd = str(symbol).strip()
        strat_id = str(strategy_id).strip() if strategy_id else None

        # 1. 당일 손절 횟수 제한 (3회 이상 손절 시 당일 차단)
        stop_count = (
            self.daily_stop_counts_by_strategy.get((target_cd, strat_id), 0)
            if strat_id else self.daily_stop_counts_by_symbol.get(target_cd, 0)
        )
        if stop_count >= 3:
            return False, f"당일 손절 제한 초과 ({stop_count}회 >= 3회, REENTRY_DAILY_LIMIT_EXCEEDED)"

        # 2. 최근 청산 이력 확인
        last_trade = (
            self.strategy_closed_trades.get((target_cd, strat_id))
            if strat_id else self.last_closed_trades.get(target_cd)
        )
        if not last_trade:
            return True, "OK"

        is_stop = last_trade.get("is_stop_loss", False) or ("손절" in last_trade.get("reason", "") or "스톱" in last_trade.get("reason", ""))
        exit_time = last_trade.get("exit_time")
        if is_stop and isinstance(exit_time, datetime):
            elapsed = (now - exit_time).total_seconds()
            if elapsed < 900.0:
                remaining = int(900.0 - elapsed)
                return False, f"손절 후 쿨다운 진행 중 (잔여: {remaining}초 < 900초, REENTRY_COOLDOWN_ACTIVE)"

        return True, "OK"

    def get_reentry_telemetry(
        self,
        symbol: str,
        strategy_id: Optional[str] = None,
        now: Optional[datetime] = None,
        **kwargs
    ) -> Dict[str, Any]:
        """
        [Section 3 & 4] 재진입 품질 telemetry 산출
        - 종목별 & 전략별 분리 집계
        - last_exit_reason, last_exit_time, time_since_last_exit
        - same_symbol_entry_count_today, same_symbol_stop_count_today
        - previous_trade_pnl, previous_trade_mfe, previous_trade_mae
        - last_3_trade_pnl, last_3_trade_avg_mfe
        """
        if "strategy" in kwargs and not strategy_id:
            strategy_id = kwargs["strategy"]
        now = now or datetime.now()
        target_cd = str(symbol).strip()

        if strategy_id:
            strat_id = str(strategy_id).strip()
            matching = [
                t for t in self.trade_history
                if t["symbol"] == target_cd and t["strategy"] == strat_id
                and not t.get("strategy", "").startswith("RESTART_RECOVERY")
                and not t.get("strategy", "").startswith("SCALE_OUT")
            ]
            last_trade = self.strategy_closed_trades.get((target_cd, strat_id))
            entry_count = self.daily_entry_counts_by_strategy.get((target_cd, strat_id), 0)
            stop_count = self.daily_stop_counts_by_strategy.get((target_cd, strat_id), 0)
        else:
            matching = [
                t for t in self.trade_history
                if t["symbol"] == target_cd
                and not t.get("strategy", "").startswith("RESTART_RECOVERY")
                and not t.get("strategy", "").startswith("SCALE_OUT")
            ]
            last_trade = self.last_closed_trades.get(target_cd)
            entry_count = self.daily_entry_counts_by_symbol.get(target_cd, 0)
            stop_count = self.daily_stop_counts_by_symbol.get(target_cd, 0)

        # RESTART_RECOVERY 및 SCALE_OUT 청산 결과 원천 배제
        if last_trade and (
            str(last_trade.get("strategy", "")).startswith("RESTART_RECOVERY") or
            str(last_trade.get("strategy", "")).startswith("SCALE_OUT")
        ):
            last_trade = None

        last_exit_reason = last_trade["exit_reason"] if last_trade else "NONE"
        last_exit_time = last_trade["exit_time"] if last_trade else None
        if last_exit_time and isinstance(last_exit_time, datetime):
            time_since_last_exit = max(0.0, (now - last_exit_time).total_seconds())
        else:
            time_since_last_exit = 999999.0

        prev_pnl = float(last_trade["pnl"]) if last_trade else 0.0
        prev_mfe = float(last_trade["mfe"]) if last_trade else 0.0
        prev_mae = float(last_trade["mae"]) if last_trade else 0.0

        last_3 = matching[-3:] if matching else []
        last_3_pnl = sum(t["pnl"] for t in last_3) if last_3 else 0.0
        last_3_avg_mfe = (sum(t["mfe"] for t in last_3) / len(last_3)) if last_3 else 0.0

        return {
            "symbol": target_cd,
            "strategy": strategy_id,
            "last_exit_reason": last_exit_reason,
            "last_exit_time": last_exit_time,
            "time_since_last_exit": time_since_last_exit,
            "same_symbol_entry_count_today": entry_count,
            "same_symbol_stop_count_today": stop_count,
            "previous_trade_pnl": prev_pnl,
            "previous_trade_mfe": prev_mfe,
            "previous_trade_mae": prev_mae,
            "last_3_trade_pnl": last_3_pnl,
            "last_3_trade_avg_mfe": last_3_avg_mfe
        }

    def get_reentry_stats_summary(self) -> Dict[str, int]:
        """[Section 11] 리포트용 전체 종목 재진입 통계 집계 (상호 배타적 Disjoint Buckets)"""
        same_symbol_reentries = sum(1 for cnt in self.daily_entry_counts_by_symbol.values() if cnt > 1)
        same_3plus = sum(1 for cnt in self.daily_entry_counts_by_symbol.values() if cnt >= 3)

        # 종목별 거래 이력 분류 (RESTART_RECOVERY 및 SCALE_OUT 제외)
        trades_by_sym: Dict[str, List[Dict[str, Any]]] = {}
        for t in self.trade_history:
            strat = t.get("strategy", "")
            if strat.startswith("RESTART_RECOVERY") or strat.startswith("SCALE_OUT"):
                continue
            sym = t.get("symbol", "")
            if sym:
                trades_by_sym.setdefault(sym, []).append(t)

        stop_lt_15m = 0
        stop_15_30m = 0
        stop_gte_30m = 0

        for sym, trades in trades_by_sym.items():
            sorted_trades = sorted(
                trades,
                key=lambda x: x.get("entry_time") if isinstance(x.get("entry_time"), datetime) else datetime.min
            )
            for i in range(1, len(sorted_trades)):
                curr = sorted_trades[i]
                prev = sorted_trades[i - 1]
                if prev.get("is_stop_loss", False):
                    c_entry = curr.get("entry_time")
                    p_exit = prev.get("exit_time")
                    if isinstance(c_entry, datetime) and isinstance(p_exit, datetime):
                        gap_sec = (c_entry - p_exit).total_seconds()
                        if gap_sec >= 0:
                            if gap_sec < 900.0:
                                stop_lt_15m += 1
                            elif gap_sec < 1800.0:
                                stop_15_30m += 1
                            else:
                                stop_gte_30m += 1

        total_stop_reentries = stop_lt_15m + stop_15_30m + stop_gte_30m
        return {
            "same_symbol_reentries": same_symbol_reentries,
            "stop_reentry_lt_15m": stop_lt_15m,
            "stop_reentry_15_30m": stop_15_30m,
            "stop_reentry_gte_30m": stop_gte_30m,
            "stop_reentry_lt_30m": stop_lt_15m + stop_15_30m,
            "total_stop_reentries": total_stop_reentries,
            "same_symbol_3plus_entries": same_3plus
        }

    def check_exit_order_timeouts(
        self,
        now: Optional[datetime] = None,
        timeout_seconds: Optional[float] = None,
        price_map: Optional[Dict[str, float]] = None,
        bid_map: Optional[Dict[str, float]] = None
    ) -> List[Dict[str, Any]]:
        """
        Section 1, 3, 6: 미체결 지정가 Exit 주문 타임아웃 감지 및 Aggressive Limit 전환 재주문
        - LIMIT 주문이 timeout_seconds (기본 30초) 동안 미체결 시:
          1. 미체결 주문 취소
          2. 현재 호가/가격 재평가
          3. Fallback(AGGRESSIVE_LIMIT)으로 전환 및 재주문 발주
          4. 결정 감사 로그 기록
        """
        now = now or datetime.now()
        timeout_sec = timeout_seconds if timeout_seconds is not None else getattr(settings, "EXIT_ORDER_TIMEOUT_SECONDS", 30.0)
        price_map = price_map or {}
        bid_map = bid_map or {}

        if not self.router:
            return []

        handled_timeouts = []
        target_positions = list(self.positions.values()) + [p for p in self.closed_positions if p.active_exit_order_id]

        for pos in target_positions:
            if not pos.active_exit_order_id:
                continue

            order_id = pos.active_exit_order_id
            pending_order = self.router.pending_orders.get(order_id)
            if not pending_order:
                pos.active_exit_order_id = None
                continue

            sent_at = pos.exit_order_sent_at or pending_order.sent_at or now
            elapsed = (now - sent_at).total_seconds()
            if elapsed < timeout_sec:
                continue

            # 타임아웃 도달
            logger.warning(
                f"[Exit 주문 타임아웃 감지] {pos.name}({pos.iem_cd}) 주문 {order_id} "
                f"미체결 경과 {elapsed:.1f}초 >= {timeout_sec:.1f}초 -> AGGRESSIVE_LIMIT 전환 시작"
            )

            # 1. 기존 미체결 주문 취소
            self.router.cancel_order(order_id, reason="ORDER_TIMEOUT")

            # 2. 현재 가격 및 Bid 재평가
            cur_price = price_map.get(pos.iem_cd, pos.current_price or pos.entry_price)
            cur_bid = bid_map.get(pos.iem_cd, cur_price)
            remaining_qty = pending_order.qty

            # 3. Fallback Plan 산출 (AGGRESSIVE_LIMIT)
            fallback_plan = ExitOrderPolicyEngine.determine_exit_plan(
                exit_reason=pos.exit_reason or ExitReason.TARGET_EXIT,
                qty=remaining_qty,
                current_price=float(cur_price),
                bid=float(cur_bid),
                is_fallback=True,
                now=now
            )

            # 4. 결정 감사 로그 영구 기록
            if self.persistence_manager:
                try:
                    self.persistence_manager.record_exit_execution(fallback_plan, pos.iem_cd, pos.position_id)
                except Exception as e:
                    logger.error(f"Fallback Exit 감사 로그 기록 실패: {e}")

            # 5. Fallback 재주문 발주
            fb_order_price = int(fallback_plan.limit_price) if fallback_plan.limit_price > 0 else int(cur_price)
            ret_pct = ((float(fb_order_price) - pos.entry_price) / pos.entry_price * 100.0) if pos.entry_price > 0 else 0.0
            exit_sig = TradeSignal(
                strategy_id=f"{pos.strategy_id}_TIMEOUT_FALLBACK",
                time_horizon=pos.time_horizon,
                iem_cd=pos.iem_cd,
                name=pos.name,
                side=OrderSide.SELL,
                strategy_price=int(cur_price),
                stop_price=0,
                score=100.0,
                reason=f"ORDER_TIMEOUT -> {fallback_plan.description}",
                timestamp=now,
                order_type=fallback_plan.selected_order_type,
                entry_price=float(pos.entry_price),
                return_pct=round(float(ret_pct), 2)
            )

            new_order = self.router.submit_order(
                signal=exit_sig,
                shares=remaining_qty,
                order_type=fallback_plan.selected_order_type,
                order_price=fb_order_price,
                now=now
            )

            if new_order:
                pos.active_exit_order_id = new_order.client_order_id
                pos.exit_order_sent_at = now
                if new_order.client_order_id not in self.router.pending_orders:
                    pos.active_exit_order_id = None

            self.stop_watchdog_stats["timeouts_handled"] += 1
            handled_timeouts.append({
                "iem_cd": pos.iem_cd,
                "position_id": pos.position_id,
                "cancelled_order_id": order_id,
                "new_order_id": new_order.client_order_id if new_order else None,
                "fallback_plan": fallback_plan,
                "elapsed_seconds": elapsed
            })

        return handled_timeouts

    def serialize_state(self) -> Dict[str, Any]:
        """포지션 매니저의 활성 및 종료 포지션 상태를 직렬화"""
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
                "entry_time": p.entry_time.isoformat() if isinstance(p.entry_time, datetime) else str(p.entry_time),
                "trading_mode": getattr(p, "trading_mode", "MOCK"),
                "account_no": getattr(p, "account_no", "")
            }
        return {
            "positions": pos_data,
            "closed_count": len(self.closed_positions),
            "stats": dict(self.stop_watchdog_stats)
        }

    def restore_state(self, state: Dict[str, Any]) -> int:
        """직렬화된 상태로부터 포지션 복원 (유령 포지션/유령 주문 원천 차단)"""
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
            pos.trading_mode = data.get("trading_mode", "MOCK")
            pos.account_no = data.get("account_no", "")

            self.positions[pid] = pos
            restored_count += 1

        if "stats" in state:
            self.stop_watchdog_stats.update(state["stats"])

        logger.info(f"[포지션 복원 완료] 총 {restored_count}건 활성 포지션 복원 완료 (유령 포지션 0건)")
        return restored_count

