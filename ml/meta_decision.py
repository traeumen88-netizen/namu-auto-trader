"""v7.0 Meta Decision Engine (ml/meta_decision.py)
Multi-setup Comparative Decision Engine & Gatekeeper.

- Evaluates candidate setups (Intraday & Swing).
- Computes Net Expected R incorporating roundtrip transaction costs & slippage.
- Enforces Gatekeeper / No-Trade filters:
    P(Target) >= 0.65
    Edge >= 0.25
    E[Net R] >= +0.20R
    R:R >= 1.5
- Applies Fractional Kelly sizing bounded by strict portfolio risk constraints.
"""

from typing import Dict, Any, List, Optional
from dataclasses import dataclass
from core.cost_model import CostModel
from core.models import OrderSide, TimeHorizon


@dataclass
class MetaDecisionResult:
    approved: bool
    setup_name: str
    p_target: float
    p_stop: float
    edge: float
    reward_risk_ratio: float
    cost_r: float
    expected_net_r: float
    kelly_fraction: float
    recommended_risk_pct: float
    reason: str
    details: Dict[str, Any]


class MetaDecisionEngine:
    """
    Multi-setup Comparative Decision Engine & Gatekeeper.
    Ranks candidate setups, deducts execution frictions, and applies Fractional Kelly sizing.
    """

    def __init__(
        self,
        min_p_target: float = 0.65,
        min_edge: float = 0.25,
        min_expected_net_r: float = 0.20,
        min_reward_risk: float = 1.5,
        kelly_scale: float = 0.25,       # Quarter-Kelly for conservative capital preservation
        max_intraday_risk_pct: float = 0.005, # Max 0.5% equity risk per intraday trade
        max_swing_risk_pct: float = 0.010,    # Max 1.0% equity risk per swing trade
        market: str = "KOSPI"
    ):
        self.min_p_target = min_p_target
        self.min_edge = min_edge
        self.min_expected_net_r = min_expected_net_r
        self.min_reward_risk = min_reward_risk
        self.kelly_scale = kelly_scale
        self.max_intraday_risk_pct = max_intraday_risk_pct
        self.max_swing_risk_pct = max_swing_risk_pct
        self.cost_model = CostModel(market=market)

    def calculate_cost_in_r(
        self,
        entry_price: float,
        stop_price: float,
        shares: int = 100,
        scenario: str = "NORMAL"
    ) -> float:
        """
        Calculates roundtrip execution cost (slippage, commission, tax) in units of 1R.
        1R = abs(entry_price - stop_price) * shares.
        """
        if entry_price <= 0 or stop_price <= 0:
            return 0.15 # Default conservative penalty

        r_value_per_share = abs(entry_price - stop_price)
        if r_value_per_share == 0:
            return 1.0

        # Buy cost
        buy_cost = self.cost_model.calculate_cost(OrderSide.BUY, entry_price, shares, scenario)
        # Estimated sell cost near entry
        sell_cost = self.cost_model.calculate_cost(OrderSide.SELL, entry_price, shares, scenario)
        total_roundtrip_cost = buy_cost.total_cost + sell_cost.total_cost

        r_total_cash = r_value_per_share * shares
        cost_in_r = total_roundtrip_cost / r_total_cash
        return round(cost_in_r, 4)

    def calculate_fractional_kelly(self, p_target: float, b_ratio: float) -> float:
        """
        Standard Kelly formula: f* = (p*(b + 1) - 1) / b
        Where:
            p = win rate P(Target)
            b = win/loss payout ratio (Reward-to-Risk ratio)
        Returns:
            fractional_kelly = f* * kelly_scale (clipped to >= 0)
        """
        if b_ratio <= 0:
            return 0.0
        q = 1.0 - p_target
        f_star = (p_target * b_ratio - q) / b_ratio
        if f_star <= 0:
            return 0.0
        return max(0.0, f_star * self.kelly_scale)

    def evaluate_candidate(
        self,
        setup_name: str,
        time_horizon: TimeHorizon,
        entry_price: float,
        stop_price: float,
        target_price: float,
        predicted_probs: Dict[str, float],
        shares: int = 100,
        cost_scenario: str = "NORMAL",
        chase_ratio: float = 0.0
    ) -> MetaDecisionResult:
        """
        Evaluates an individual trade candidate against the Gatekeeper filter.
        """
        p_target = predicted_probs.get("p_target", 0.0)
        p_stop = predicted_probs.get("p_stop", 0.0)

        # Chase penalty adjustment
        if chase_ratio > 0.015:
            # Chasing high entry degrades probability
            penalty = min(0.20, chase_ratio * 5.0)
            p_target = max(0.0, p_target - penalty)
            p_stop = min(1.0, p_stop + penalty)

        edge = p_target - p_stop

        # Calculate R:R
        risk = abs(entry_price - stop_price)
        reward = abs(target_price - entry_price)
        rr_ratio = reward / risk if risk > 0 else 0.0

        # Execution cost in R
        cost_r = self.calculate_cost_in_r(entry_price, stop_price, shares, cost_scenario)

        # Expected Net R
        # E[Net R] = P(Target) * rr_ratio - P(Stop) * 1.0 - Cost(R)
        expected_net_r = (p_target * rr_ratio) - (p_stop * 1.0) - cost_r

        # Fractional Kelly
        f_kelly = self.calculate_fractional_kelly(p_target, rr_ratio)

        # Cap by maximum portfolio risk per trade
        max_risk = self.max_intraday_risk_pct if time_horizon == TimeHorizon.INTRADAY else self.max_swing_risk_pct
        recommended_risk = min(f_kelly, max_risk)

        # Gatekeeper Rule Check
        reasons = []
        if p_target < self.min_p_target:
            reasons.append(f"P(Target) {p_target:.3f} < {self.min_p_target:.2f}")
        if edge < self.min_edge:
            reasons.append(f"Edge {edge:.3f} < {self.min_edge:.2f}")
        if expected_net_r < self.min_expected_net_r:
            reasons.append(f"E[Net R] {expected_net_r:+.3f}R < {self.min_expected_net_r:.2f}R")
        if rr_ratio < self.min_reward_risk:
            reasons.append(f"R:R {rr_ratio:.2f} < {self.min_reward_risk:.2f}")
        if recommended_risk <= 0:
            reasons.append("Kelly sizing non-positive")

        approved = len(reasons) == 0

        reason_str = "APPROVED" if approved else f"GATEKEEPER_REJECT: {'; '.join(reasons)}"

        return MetaDecisionResult(
            approved=approved,
            setup_name=setup_name,
            p_target=round(p_target, 4),
            p_stop=round(p_stop, 4),
            edge=round(edge, 4),
            reward_risk_ratio=round(rr_ratio, 2),
            cost_r=round(cost_r, 4),
            expected_net_r=round(expected_net_r, 4),
            kelly_fraction=round(f_kelly, 4),
            recommended_risk_pct=round(recommended_risk, 4),
            reason=reason_str,
            details={
                "entry_price": entry_price,
                "stop_price": stop_price,
                "target_price": target_price,
                "time_horizon": time_horizon.value if hasattr(time_horizon, "value") else str(time_horizon),
                "cost_scenario": cost_scenario,
                "chase_ratio": chase_ratio
            }
        )

    def select_best_setup(self, candidate_results: List[MetaDecisionResult]) -> Optional[MetaDecisionResult]:
        """
        Ranks multiple approved setups by Expected Net R and picks the top setup.
        Returns None if no candidate passes gatekeeper.
        """
        approved = [r for r in candidate_results if r.approved]
        if not approved:
            return None
        # Sort by expected_net_r descending
        approved.sort(key=lambda x: x.expected_net_r, reverse=True)
        return approved[0]
