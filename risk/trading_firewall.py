"""v7.0 Ironclad Trading Firewall (risk/trading_firewall.py)
Hardwired Risk Governance & Veto Layer.

INVARIANT:
AI models and ML algorithms have NO AUTHORITY over risk parameters.
The Trading Firewall retains absolute veto power over all trade generation,
enforcing account survivability, circuit breakers, loss limits, and tick integrity.
"""

from typing import Dict, Any, Optional, Tuple
from dataclasses import dataclass
from datetime import datetime, timedelta
from core.models import OrderSide, TimeHorizon, MarketRegime, TradeSignal
from core.tick_normalizer import normalize_price, get_tick_size
from risk.circuit_breaker import CircuitBreaker
from risk.loss_limits import LossLimitManager


@dataclass
class FirewallVerdict:
    approved: bool
    veto_reason: Optional[str]
    shares: int
    normalized_price: float
    risk_status: str
    details: Dict[str, Any]


class TradingFirewall:
    """
    Independent Veto Layer executing pre-trade safety screening.
    Every signal from ML/Strategies must pass through this firewall before reaching OrderRouter.
    """

    def __init__(
        self,
        circuit_breaker: Optional[CircuitBreaker] = None,
        loss_limit_mgr: Optional[LossLimitManager] = None,
        max_intraday_positions: Any = float("inf"),
        max_swing_positions: Any = float("inf"),
        max_single_stock_pct: float = 0.15,
        max_sector_pct: float = 0.30
    ):
        self.circuit_breaker = circuit_breaker or CircuitBreaker()
        self.loss_limit_mgr = loss_limit_mgr or LossLimitManager()
        self.max_intraday_positions = max_intraday_positions
        self.max_swing_positions = max_swing_positions
        self.max_single_stock_pct = max_single_stock_pct
        self.max_sector_pct = max_sector_pct

    def check_firewall(
        self,
        signal: TradeSignal,
        shares: int,
        order_price: float,
        equity: float,
        cash: float,
        current_regime: MarketRegime,
        active_positions: Dict[str, Any],
        now: Optional[datetime] = None
    ) -> FirewallVerdict:
        """
        Executes non-negotiable safety checks.
        Returns FirewallVerdict with approval status or veto reason.
        """
        now = now or datetime.now()

        # 1. Circuit Breaker & Heartbeat Staleness Check
        if self.circuit_breaker.is_tripped:
            return FirewallVerdict(
                approved=False,
                veto_reason=f"CIRCUIT_BREAKER_TRIPPED: {self.circuit_breaker.trip_reason}",
                shares=0,
                normalized_price=order_price,
                risk_status="HALTED",
                details={"circuit_breaker": True}
            )

        if self.circuit_breaker.check_data_staleness(now):
            return FirewallVerdict(
                approved=False,
                veto_reason="DATA_HEARTBEAT_STALE: Market data feed delay exceeds 3.0s threshold",
                shares=0,
                normalized_price=order_price,
                risk_status="HALTED",
                details={"staleness": True}
            )

        # 2. Market Regime Panic / Crash Check
        if signal.side == OrderSide.BUY and current_regime in (MarketRegime.PANIC, MarketRegime.BEAR):
            # Strict veto: AI is not allowed to catch falling knives during market crash
            return FirewallVerdict(
                approved=False,
                veto_reason=f"MARKET_REGIME_VETO: Buying prohibited in {current_regime.name} regime",
                shares=0,
                normalized_price=order_price,
                risk_status="REGIME_VETO",
                details={"regime": current_regime.name}
            )

        # 3. Daily Loss Limit Check
        daily_loss_pct = self.loss_limit_mgr.daily_loss_pct if hasattr(self.loss_limit_mgr, "daily_loss_pct") else 0.0
        weekly_loss_pct = self.loss_limit_mgr.weekly_loss_pct if hasattr(self.loss_limit_mgr, "weekly_loss_pct") else 0.0
        loss_eval = self.loss_limit_mgr.evaluate_loss_limits(daily_loss_pct, weekly_loss_pct)

        if signal.time_horizon == TimeHorizon.INTRADAY and not loss_eval.get("can_trade_intraday", True):
            return FirewallVerdict(
                approved=False,
                veto_reason=f"DAILY_LOSS_LIMIT_EXCEEDED: Intraday trading suspended ({loss_eval.get('action')})",
                shares=0,
                normalized_price=order_price,
                risk_status="LOSS_LIMIT_HALT",
                details=loss_eval
            )
        if signal.time_horizon == TimeHorizon.SWING and not loss_eval.get("can_trade_swing", True):
            return FirewallVerdict(
                approved=False,
                veto_reason=f"LOSS_LIMIT_EXCEEDED: Swing trading suspended ({loss_eval.get('action')})",
                shares=0,
                normalized_price=order_price,
                risk_status="LOSS_LIMIT_HALT",
                details=loss_eval
            )

        # 4. Position Sizing & Price / Tick Normalization
        if shares <= 0:
            return FirewallVerdict(
                approved=False,
                veto_reason="ZERO_OR_NEGATIVE_SHARES",
                shares=0,
                normalized_price=order_price,
                risk_status="INVALID_SIZE",
                details={"shares": shares}
            )

        # Tick normalization
        normalized_p = normalize_price(order_price, signal.side.value if hasattr(signal.side, "value") else str(signal.side), "LIMIT")

        # Intraday Stop loss sanity check: stop loss cannot exceed 3% for intraday
        if signal.time_horizon == TimeHorizon.INTRADAY and signal.stop_price > 0:
            loss_dist_pct = (normalized_p - signal.stop_price) / normalized_p
            if loss_dist_pct > 0.0301:
                return FirewallVerdict(
                    approved=False,
                    veto_reason=f"INTRADAY_STOP_LOSS_TOO_WIDE: Stop loss {loss_dist_pct:.2%} exceeds 3.0% maximum allowed",
                    shares=0,
                    normalized_price=normalized_p,
                    risk_status="INVALID_STOP",
                    details={"stop_price": signal.stop_price, "loss_dist": loss_dist_pct}
                )

        # 5. Purchasing Power / Cash Sufficiency Check
        order_cost = normalized_p * shares
        if order_cost > cash:
            max_shares_affordable = int(cash // normalized_p)
            if max_shares_affordable <= 0:
                return FirewallVerdict(
                    approved=False,
                    veto_reason=f"INSUFFICIENT_CASH: Required {order_cost:,.0f} > Available {cash:,.0f}",
                    shares=0,
                    normalized_price=normalized_p,
                    risk_status="INSUFFICIENT_FUNDS",
                    details={"order_cost": order_cost, "cash": cash}
                )
            # Downsize shares to affordable amount
            shares = max_shares_affordable

        # 6. Portfolio Concentration Check
        order_value = normalized_p * shares
        if equity > 0 and (order_value / equity) > self.max_single_stock_pct:
            # Cap shares to maximum allowed single stock weight
            shares = int((equity * self.max_single_stock_pct) // normalized_p)
            if shares <= 0:
                return FirewallVerdict(
                    approved=False,
                    veto_reason=f"CONCENTRATION_LIMIT: Exceeds {self.max_single_stock_pct:.0%} single stock limit",
                    shares=0,
                    normalized_price=normalized_p,
                    risk_status="CONCENTRATION_VETO",
                    details={"max_single_stock_pct": self.max_single_stock_pct}
                )

        return FirewallVerdict(
            approved=True,
            veto_reason=None,
            shares=shares,
            normalized_price=normalized_p,
            risk_status="APPROVED",
            details={
                "approved_shares": shares,
                "normalized_price": normalized_p,
                "position_count_check": "BYPASSED / NOT_USED",
                "max_position_count": "UNLIMITED"
            }
        )
