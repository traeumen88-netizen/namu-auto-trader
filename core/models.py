"""통합 퀀트 시스템 핵심 데이터 모델 (Domain Models)
- Tick, Candle, OrderBook, Signal, Order, Position, RegimeState, TradeRecord
"""

from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import List, Tuple, Optional, Dict, Any


class TimeHorizon(str, Enum):
    INTRADAY = "INTRADAY"
    SWING = "SWING"


class OrderSide(str, Enum):
    BUY = "BUY"
    SELL = "SELL"


class OrderType(str, Enum):
    LIMIT = "01"     # 보통가 (지정가)
    MARKET = "05"    # 시장가
    IOC = "IOC"      # Immediate Or Cancel (모의/실전에 따라 보통가+즉시취소 에뮬레이션)
    AGGRESSIVE_LIMIT = "AGGRESSIVE_LIMIT" # 최우선 호가 +- N Tick 적용 체결 우선 지정가


class OrderStatus(str, Enum):
    # Standard State Machine Lifecycle Stages
    ORDER_CREATED = "ORDER_CREATED"
    ORDER_SENT = "ORDER_SENT"
    ORDER_ACK = "ORDER_ACK"
    PENDING = "PENDING"
    PARTIAL_FILL = "PARTIAL_FILL"
    FILLED = "FILLED"
    CANCELED = "CANCELED"
    CANCELLED = "CANCELLED"
    REJECTED = "REJECTED"
    EXPIRED = "EXPIRED"

    # Aliases for backwards compatibility
    CREATED = "ORDER_CREATED"
    SENT = "ORDER_SENT"
    ACK = "ORDER_ACK"
    PARTIAL = "PARTIAL_FILL"


class MarketRegime(str, Enum):
    STRONG_BULL = "STRONG_BULL"
    BULL = "BULL"
    NEUTRAL = "NEUTRAL"
    BEAR = "BEAR"
    PANIC = "PANIC"


class SymbolState(str, Enum):
    """전체 유니버스 종목 상태 머신 (Section 4)"""
    INACTIVE = "INACTIVE"       # 이상 이벤트 없음 (기본 저비용 감시)
    WATCH = "WATCH"             # 관심 이벤트 감지 (이벤트 점수 50~64)
    ACTIVE = "ACTIVE"           # 실시간 정밀 분석 승격 (이벤트 점수 65 이상)
    CANDIDATE = "CANDIDATE"     # 매매전략 적합성 분석 가치 있음
    SETUP = "SETUP"             # 구체적인 전략 핵심 구조 성립
    ENTRY_READY = "ENTRY_READY" # 현재 가격 기준 즉시 진입 가능 상태
    SIGNAL = "SIGNAL"           # 매매 조건 충족 후보 (하위 호환)
    POSITION = "POSITION"       # 실제 포지션 보유
    COOLDOWN = "COOLDOWN"       # 손절/실패 후 일시 거래 제한


class SetupType(str, Enum):
    """최소 지원 9대 셋업 유형 (Section 8)"""
    MOMENTUM = "MOMENTUM"
    BREAKOUT = "BREAKOUT"
    VWAP_PULLBACK = "VWAP_PULLBACK"
    EMA_PULLBACK = "EMA_PULLBACK"
    COMPRESSION_BREAKOUT = "COMPRESSION_BREAKOUT"
    ORB = "ORB"
    PDH_BREAKOUT = "PDH_BREAKOUT"
    HH_HL = "HH_HL"
    NEWS_MOMENTUM = "NEWS_MOMENTUM"


class CandidatePriority(str, Enum):
    PRIORITY_1 = "ACTIVE PRIORITY 1"  # 80점 이상
    PRIORITY_2 = "ACTIVE PRIORITY 2"  # 65~79점
    WATCH = "WATCH"                   # 50~64점
    INACTIVE = "INACTIVE"             # 49점 이하


class MarketEventType(str, Enum):
    """Section 5 장중 Dynamic Discovery 15대 이벤트 (Events A ~ O)"""
    EVENT_A = "VOL_SURGE_3X"          # 최근 1분 거래량 >= 평균 * 3
    EVENT_B = "RETURN_3M_2PCT"        # 최근 3분 수익률 >= +2.0%
    EVENT_C = "RETURN_5M_3PCT"        # 최근 5분 수익률 >= +3.0%
    EVENT_D = "TURNOVER_SURGE_3X"     # 최근 1분 거래대금 >= 20개 평균 * 3
    EVENT_E = "NEW_DAY_HIGH"          # 현재가 >= 당일 고가
    EVENT_F = "PDH_BREAKOUT"          # 현재가 > 전일 고가
    EVENT_G = "HIGH_20BAR_BREAKOUT"   # 현재가 > 최근 20개 1분봉 최고가
    EVENT_H = "VWAP_BREAKOUT"         # VWAP 상향 돌파
    EVENT_I = "EMA_GOLDEN_CROSS"      # EMA9 > EMA20 골든크로스
    EVENT_J = "EXECUTION_INTENSITY"   # 체결강도 >= 120
    EVENT_K = "ORDERBOOK_IMBALANCE"   # 호가 불균형 OBI >= +0.25
    EVENT_L = "NEWS_EVENT"            # 뉴스/공시 이벤트
    EVENT_M = "RANK_SURGE"            # 거래대금 순위 급상승
    EVENT_N = "ATR_EXPANSION"         # 5분 ATR 급증
    EVENT_O = "VOLATILITY_BURST"      # 가격 변동성 급증


@dataclass
class SymbolInfo:
    """전체 상장 유니버스 종목 마스터 정보 (Section 1)"""
    iem_cd: str
    name: str
    market: str = "KOSPI"  # "KOSPI" | "KOSDAQ"
    status: str = "NORMAL"  # "NORMAL", "MANAGED", "HALTED"
    is_tradable: bool = True
    price: int = 0
    prev_close: int = 0
    regular_close: Optional[float] = None
    prev_high: int = 0
    prev_low: int = 0
    open_price: int = 0
    high_price: int = 0
    low_price: int = 0
    acml_vol: int = 0
    acml_trde_amt: int = 0
    sector: str = "기타"
    theme: str = "기타"
    is_managed: bool = False
    is_halted: bool = False
    state: SymbolState = SymbolState.INACTIVE
    event_score: float = 0.0
    active_events: List[str] = field(default_factory=list)
    last_event_time: Optional[datetime] = None
    cooldown_until: Optional[datetime] = None
    loss_count_today: int = 0
    candidate_time: Optional[datetime] = None
    score_history: List[Tuple[datetime, float]] = field(default_factory=list)
    buy_block_reasons: List[str] = field(default_factory=list)
    momentum_stage: str = "NORMAL"  # "EARLY", "ACTIVE", "LATE", "NORMAL"
    rule_score: float = 0.0
    ml_probability: Optional[float] = None
    expected_net_r: Optional[float] = None
    last_evaluated_time: Optional[datetime] = None
    ask1_price: int = 0
    bid1_price: int = 0
    ask1_qty: int = 0
    bid1_qty: int = 0
    relative_strength: float = 0.0
    stage_timestamps: Dict[str, float] = field(default_factory=dict)


@dataclass
class MarketEvent:
    """감지된 실시간 시장 이벤트"""
    event_type: MarketEventType
    iem_cd: str
    name: str
    timestamp: datetime
    description: str
    score_delta: float
    metrics: Dict[str, Any] = field(default_factory=dict)


@dataclass
class Tick:

    timestamp: datetime
    iem_cd: str
    price: int
    volume: int
    ask_price: int = 0
    bid_price: int = 0
    ask_qty: int = 0
    bid_qty: int = 0


@dataclass
class Candle:
    timestamp: datetime
    timeframe: str  # "1m", "3m", "5m", "15m", "1d"
    open: int
    high: int
    low: int
    close: int
    volume: int
    turnover: int = 0
    vwap: float = 0.0
    is_closed: bool = False  # 봉 마감 여부 (Look-Ahead Bias 방지)


@dataclass
class OrderBook:
    timestamp: datetime
    iem_cd: str
    asks: List[Tuple[int, int]] = field(default_factory=list)  # [(price, qty), ...]
    bids: List[Tuple[int, int]] = field(default_factory=list)  # [(price, qty), ...]
    obi: float = 0.0  # Order Book Imbalance (-1.0 ~ +1.0)
    bid_ask_spread_ratio: float = 0.0


@dataclass
class TradeSignal:
    strategy_id: str
    time_horizon: TimeHorizon
    iem_cd: str
    name: str
    side: OrderSide
    strategy_price: int
    stop_price: int
    score: float
    reason: str
    timestamp: datetime
    target_1r: int = 0
    target_2r: int = 0
    target_3r: int = 0
    atr14: float = 0.0
    expected_rr: float = 0.0
    momentum_stage: str = "NORMAL"  # "EARLY", "ACTIVE", "LATE", "NORMAL"
    rule_score: float = 0.0
    ml_probability: Optional[float] = None
    expected_net_r: Optional[float] = None
    approved_status: str = "SIGNAL"  # "SIGNAL", "BUY_APPROVED", "REJECTED"
    rejection_reasons: List[str] = field(default_factory=list)
    order_type: Optional[OrderType] = None
    ask1_price: int = 0
    bid1_price: int = 0
    ask1_qty: int = 0
    bid1_qty: int = 0
    relative_strength: float = 0.0
    rvol: float = 1.0
    latency_ms: float = 0.0
    stage_timestamps: Dict[str, float] = field(default_factory=dict)
    entry_timing_valid: bool = True
    timing_reason: str = ""
    entry_price: float = 0.0
    return_pct: float = 0.0
    signal_session: str = "REGULAR"  # "REGULAR" | "AFTER_HOURS"
    entry_session: str = "REGULAR"   # "REGULAR" | "AFTER_HOURS"

    # [Section 1] Signal Timestamps & TTL
    signal_created_at: Optional[datetime] = None
    candidate_at: Optional[datetime] = None
    setup_at: Optional[datetime] = None
    buy_approved_at: Optional[datetime] = None
    latest_quote_at: Optional[datetime] = None
    order_created_at: Optional[datetime] = None
    order_sent_at: Optional[datetime] = None
    fill_at: Optional[datetime] = None
    signal_age_ms: float = 0.0

    # [Section 2] Price Drift Fields
    approved_price: Optional[float] = None
    latest_price: Optional[float] = None
    price_drift_pct: float = 0.0
    approved_quote_timestamp: Optional[datetime] = None
    latest_quote_timestamp: Optional[datetime] = None

    # [Section 4] Idempotency & Intent
    order_intent_id: Optional[str] = None
    idempotency_key: Optional[str] = None

    def __post_init__(self):
        if self.signal_created_at is None:
            self.signal_created_at = self.timestamp
        if self.candidate_at is None:
            self.candidate_at = self.timestamp


@dataclass
class PipelineTelemetry:
    """실시간 전체 시장 발굴 -> 실제 체결 파이프라인 관제 텔레메트리 (v9.3)"""
    universe_total: int = 0
    universe_count: int = 3136
    event_detected: int = 0
    candidates: int = 0
    setup_matches: int = 0
    hard_gate_passed: int = 0
    score_passed: int = 0
    edge_passed: int = 0
    risk_passed: int = 0
    fresh_quote_passed: int = 0
    data_stale_rejections: int = 0
    data_stale_distribution: Dict[str, int] = field(default_factory=dict)
    execution_passed: int = 0
    buy_candidates: int = 0  # alias for setup_matches
    buy_approved: int = 0
    orders_created: int = 0
    orders_sent: int = 0
    partial_fills: int = 0
    filled: int = 0
    rejected: int = 0
    zombie_orders_detected: int = 0
    reconciled_orders: int = 0
    top_rejection_reasons: Dict[str, int] = field(default_factory=dict)
    stage_latencies: Dict[str, Dict[str, float]] = field(default_factory=dict)
    entry_ready: int = 0
    setup_bottleneck_active: bool = False
    bottleneck_message: str = ""


@dataclass
class SetupInspectionItem:
    """Candidate 및 Setup 실시간 인스펙터 항목 (Section 20 & 61)"""
    iem_cd: str
    name: str
    candidate_type: str
    strategy: str
    core_pass: bool
    core_details: str
    soft_pass: bool
    soft_details: str
    score: float
    is_valid_setup: bool
    is_entry_ready: bool
    is_entry_allowed: bool
    buy_approved: bool
    block_reason: str
    timestamp: datetime = field(default_factory=datetime.now)

    @property
    def strategy_id(self) -> str:
        return self.strategy

    @property
    def is_valid(self) -> bool:
        return self.is_valid_setup

    @property
    def setup_type(self) -> str:
        return self.strategy




@dataclass
class Order:
    client_order_id: str
    iem_cd: str
    side: OrderSide
    order_type: OrderType
    qty: int
    price: int
    strategy_id: str
    time_horizon: TimeHorizon
    status: OrderStatus = OrderStatus.PENDING
    broker_order_no: Optional[str] = None
    filled_qty: int = 0
    filled_avg_price: float = 0.0
    created_at: datetime = field(default_factory=datetime.now)
    sent_at: Optional[datetime] = None
    ack_at: Optional[datetime] = None
    filled_at: Optional[datetime] = None
    slippage: float = 0.0
    fee: float = 0.0
    tax: float = 0.0
    is_zombie_suspected: bool = False
    reconciled: bool = False
    signal_session: str = "REGULAR"
    entry_session: str = "REGULAR"

    # [Section 4 & 5] Partial Fill & Quantity Reconciliation
    requested_qty: int = 0
    remaining_qty: int = 0
    protected_qty: int = 0
    exit_pending_qty: int = 0
    broker_psbl_qty: int = 0
    internal_position_qty: int = 0
    order_intent_id: Optional[str] = None
    idempotency_key: Optional[str] = None
    account_no: Optional[str] = None

    def __post_init__(self):
        if self.requested_qty == 0 and self.qty > 0:
            self.requested_qty = self.qty
        if self.remaining_qty == 0 and self.qty > 0:
            self.remaining_qty = max(0, self.qty - self.filled_qty)



@dataclass
class Position:
    position_id: str  # Intraday / Swing 완전 분리 (예: INT_005930_20260904_1, SWG_005930_20260904_1)
    time_horizon: TimeHorizon
    strategy_id: str
    iem_cd: str
    name: str
    qty: int
    entry_price: float
    current_price: float
    stop_price: int
    target_1r: int
    target_2r: int
    target_3r: int
    r_unit: float  # (Entry - Stop)
    initial_risk_amount: float
    entry_time: datetime
    trailing_stop_price: int
    highest_price: int
    is_closed: bool = False
    exit_price: float = 0.0
    exit_time: Optional[datetime] = None
    exit_reason: str = ""
    target_1r_taken: bool = False
    target_2r_taken: bool = False
    target_3r_taken: bool = False
    active_exit_order_id: Optional[str] = None
    exit_order_sent_at: Optional[datetime] = None
    status: str = "ACTIVE"  # "ACTIVE", "STALE_POSITION", "TIME_STOP_TRIGGERED", "EXIT_PENDING", "EXIT_ORDER_SENT", "FILLED", "POSITION_CLOSED"
    stale_detected_at: Optional[datetime] = None
    time_stop_triggered_at: Optional[datetime] = None
    signal_session: str = "REGULAR"
    entry_session: str = "REGULAR"
    watchdog_registered: bool = True
    watchdog_last_check_at: Optional[datetime] = None

    # [Section 5 & 7] Quantity Reconciliation & Trade Classification
    filled_qty: int = 0
    remaining_qty: int = 0
    broker_psbl_qty: Optional[int] = None
    pending_exit_qty: int = 0  # 미체결 매도 대기 수량
    trade_classification: str = "NEW_STRATEGY_BUY"  # "NEW_STRATEGY_BUY", "RESTART_RECOVERY", "RESTART_RECOVERY_SCALE_OUT"

    # [Section 7 & 12] Round-trip Linkage & Entry Telemetry
    entry_reason: str = ""
    entry_rvol: float = 0.0
    entry_momentum_3m: float = 0.0
    entry_vwap_gap: float = 0.0
    entry_rebound_strength: float = 0.0
    expected_move_pct: float = 0.0
    expected_net_r: float = 0.0
    lowest_price: int = 0
    mfe_pct: float = 0.0
    mae_pct: float = 0.0

    @property
    def available_qty(self) -> int:
        """실제 매도 주문 가능한 가용 수량 (실보유 - 미체결 매도대기)"""
        return max(0, self.qty - getattr(self, "pending_exit_qty", 0))

    def __post_init__(self):
        if self.filled_qty == 0 and self.qty > 0:
            self.filled_qty = self.qty
        if self.broker_psbl_qty is None and self.qty > 0:
            self.broker_psbl_qty = self.qty
        if self.remaining_qty == 0 and self.qty > 0:
            self.remaining_qty = self.qty
        if self.lowest_price == 0 and self.entry_price > 0:
            self.lowest_price = int(self.entry_price)


@dataclass
class CostBreakdown:
    broker_fee: float
    securities_tax: float
    agricultural_tax: float
    slippage_cost: float
    spread_cost: float
    total_cost: float
    scenario: str  # "NORMAL", "STRESS", "WORST"
