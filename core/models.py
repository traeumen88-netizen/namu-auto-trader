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


class OrderStatus(str, Enum):
    PENDING = "PENDING"
    FILLED = "FILLED"
    PARTIAL = "PARTIAL"
    CANCELLED = "CANCELLED"
    REJECTED = "REJECTED"


class MarketRegime(str, Enum):
    STRONG_BULL = "STRONG_BULL"
    BULL = "BULL"
    NEUTRAL = "NEUTRAL"
    BEAR = "BEAR"
    PANIC = "PANIC"


class SymbolState(str, Enum):
    """전체 유니버스 종목 상태 머신 (Section 4)"""
    INACTIVE = "INACTIVE"   # 이상 이벤트 없음 (기본 저비용 감시)
    WATCH = "WATCH"         # 관심 이벤트 감지 (이벤트 점수 50~64)
    ACTIVE = "ACTIVE"       # 실시간 정밀 분석 승격 (이벤트 점수 65 이상)
    SIGNAL = "SIGNAL"       # 매매 조건 충족 후보
    POSITION = "POSITION"   # 실제 포지션 보유
    COOLDOWN = "COOLDOWN"   # 손절/실패 후 일시 거래 제한


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
    market: str  # "KOSPI" | "KOSDAQ"
    status: str = "NORMAL"  # "NORMAL", "MANAGED", "HALTED"
    is_tradable: bool = True
    price: int = 0
    prev_close: int = 0
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
    filled_at: Optional[datetime] = None
    slippage: float = 0.0
    fee: float = 0.0
    tax: float = 0.0


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


@dataclass
class CostBreakdown:
    broker_fee: float
    securities_tax: float
    agricultural_tax: float
    slippage_cost: float
    spread_cost: float
    total_cost: float
    scenario: str  # "NORMAL", "STRESS", "WORST"
