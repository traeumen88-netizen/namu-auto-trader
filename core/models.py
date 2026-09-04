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
