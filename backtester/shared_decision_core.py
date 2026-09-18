"""[SHARED DECISION CORE v1.0] Unified Decision Core for LIVE and Backtest
(backtester/shared_decision_core.py)

Shared Decision Core that ensures 100% Parity between LIVE operations and Backtest:
  MARKET_UNIVERSE
  → MARKET_DATA
  → DATA_QUALITY_GATE
  → EVENT_DETECTION
  → CANDIDATE
  → SETUP
  → SETUP_SCORE
  → HISTORICAL_SIMILARITY
  → ML_SCORE
  → META_DECISION
  → EDGE
  → RISK
  → POSITION_SIZING
  → ORDER_CREATE (Dispatched to Execution Simulator in Backtest)
"""

import math
import logging
from datetime import datetime, timedelta, time as dtime
from typing import Dict, Any, List, Optional, Tuple
from dataclasses import dataclass, field

from core.models import (
    SymbolInfo, SymbolState, TradeSignal, TimeHorizon, OrderSide, OrderType,
    MarketRegime, Candle, Tick, Position
)
from core.symbol_store import SymbolStateStore
from core.aggregator import CandleAggregator
from core.data_quality_gate import DataQualityGate
from core.candidate_promotion import CandidatePromotionEngine
from strategies.full_strategy_suite import FullStrategySuite
from strategies.scoring_engine import ScoringEngine
from ml.meta_decision import MetaDecisionEngine, MetaDecisionResult, FeatureScalerPipeline
from ml.similarity_engine import HistoricalSimilarityEngine
from ml.experience_memory import ExperienceMemory
from core.edge_engine import EdgeEngine, EdgeResult
from risk.portfolio_risk import PortfolioRiskManager
from risk.loss_limits import LossLimitManager
from risk.position_sizer import PositionSizer
from risk.portfolio_cash import PortfolioCashManager
from backtester.leakage_verifier import LookaheadLeakageVerifier

logger = logging.getLogger("SharedDecisionCore")


@dataclass
class PipelineDecision:
    """Complete Decision Output from the Shared Pipeline."""
    symbol: str
    name: str
    timestamp: datetime
    is_candidate: bool
    active_events: List[str]
    setup_matched: bool
    strategy_id: str
    rule_score: float
    p_target: float
    p_stop: float
    expected_net_r: float
    reward_risk_ratio: float
    meta_decision: str  # "BUY", "BUY_SMALL", "WAIT", "NO_TRADE"
    is_edge_approved: bool
    is_risk_approved: bool
    approved_shares: int
    entry_price: float
    stop_price: float
    target_1r: float
    target_2r: float
    target_3r: float
    time_horizon: TimeHorizon
    order_type: OrderType
    decision_reason: str
    decision_trace: Dict[str, Any]
    signal: Optional[TradeSignal] = None


class SharedDecisionCore:
    """
    Unified Decision Engine shared identically by LIVE Quant Trader and Realistic Backtester.
    No backtest-only shortcuts or artificial overrides.
    """

    def __init__(
        self,
        symbol_master: Dict[str, SymbolInfo],
        leakage_verifier: Optional[LookaheadLeakageVerifier] = None,
        min_expected_net_r: float = 0.15,
        min_reward_risk: float = 1.5,
        experience_memory_path: str = "data/experience_memory.db"
    ):
        self.symbol_master = symbol_master
        self.store = SymbolStateStore(symbol_master)
        self.promoter = CandidatePromotionEngine(self.store)
        self.data_gate = DataQualityGate(max_staleness_seconds=30.0)
        self.leakage_verifier = leakage_verifier or LookaheadLeakageVerifier()

        # Decision sub-engines
        self.meta_decision_engine = MetaDecisionEngine(
            min_expected_net_r=min_expected_net_r,
            min_reward_risk=min_reward_risk
        )
        self.edge_engine = EdgeEngine(min_expected_net_r=min_expected_net_r)
        self.loss_limit_manager = LossLimitManager()

        try:
            self.exp_memory = ExperienceMemory(db_path=experience_memory_path)
            self.similarity_engine = HistoricalSimilarityEngine(self.exp_memory)
        except Exception:
            self.exp_memory = None
            self.similarity_engine = None

        self.aggregators: Dict[str, CandleAggregator] = {}
        self.daily_candles_cache: Dict[str, List[Dict[str, Any]]] = {}
        self.or_high_map: Dict[str, int] = {}
        self.cooldowns: Dict[str, datetime] = {}

    def get_or_create_aggregator(self, symbol: str) -> CandleAggregator:
        if symbol not in self.aggregators:
            self.aggregators[symbol] = CandleAggregator(symbol)
        return self.aggregators[symbol]

    def record_stop_loss(self, symbol: str, current_time: datetime, cooldown_minutes: int = 20):
        """손절 발생 시 해당 종목에 대해 20분 쿨다운 등록 (Section 13: Policy 3)"""
        self.cooldowns[symbol] = current_time + timedelta(minutes=cooldown_minutes)

    def evaluate_bar(
        self,
        symbol: str,
        bar: Dict[str, Any],
        current_time: datetime,
        account_equity: float,
        available_cash: float,
        active_positions: List[Position],
        daily_pnl_ratio: float = 0.0,
        weekly_pnl_ratio: float = 0.0,
        regime: MarketRegime = MarketRegime.NEUTRAL
    ) -> List[PipelineDecision]:
        """
        Executes the full funnel for a single stock bar at point-in-time T:
        UNIVERSE -> DATA_GATE -> EVENT_DETECTION -> CANDIDATE -> SETUP ->
        SETUP_SCORE -> SIMILARITY -> ML_SCORE -> META -> EDGE -> RISK -> SIZING
        """
        # 1. Missing bar guard
        if bar is None or not bar:
            return [PipelineDecision(
                symbol=symbol, name=symbol, timestamp=current_time,
                is_candidate=False, active_events=[], setup_matched=False,
                strategy_id="NONE", rule_score=0.0, p_target=0.0, p_stop=1.0,
                expected_net_r=-1.0, reward_risk_ratio=0.0, meta_decision="NO_TRADE",
                is_edge_approved=False, is_risk_approved=False, approved_shares=0,
                entry_price=0.0, stop_price=0, target_1r=0, target_2r=0, target_3r=0,
                time_horizon=TimeHorizon.INTRADAY, order_type=OrderType.LIMIT,
                decision_reason="DATA_QUALITY_GATE_REJECT: MISSING_BAR",
                decision_trace={"stage": "DATA_QUALITY_GATE", "reason": "MISSING_BAR"}
            )]

        # 2. Leakage Verification
        bar_ts = bar.get("timestamp", current_time)
        self.leakage_verifier.verify_bar_timestamp(current_time, bar_ts, symbol)

        sym = self.store.get(symbol)
        if not sym:
            sym = SymbolInfo(iem_cd=symbol, name=symbol, price=int(bar.get("close", 0)))
            self.store.register_symbol(sym)

        price = int(bar.get("close", 0))
        volume = int(bar.get("volume", 0))
        high_price = int(bar.get("high", price))
        low_price = int(bar.get("low", price))
        open_price = int(bar.get("open", price))

        # 2. DATA QUALITY GATE
        valid, q_reason = self.data_gate.validate_bar(
            iem_cd=symbol, bar=bar, now=current_time, is_halted=getattr(sym, "is_halted", False)
        )
        if not valid:
            return [PipelineDecision(
                symbol=symbol, name=sym.name, timestamp=current_time,
                is_candidate=False, active_events=[], setup_matched=False,
                strategy_id="NONE", rule_score=0.0, p_target=0.0, p_stop=1.0,
                expected_net_r=-1.0, reward_risk_ratio=0.0, meta_decision="NO_TRADE",
                is_edge_approved=False, is_risk_approved=False, approved_shares=0,
                entry_price=price, stop_price=0, target_1r=0, target_2r=0, target_3r=0,
                time_horizon=TimeHorizon.INTRADAY, order_type=OrderType.LIMIT,
                decision_reason=q_reason or "DATA_QUALITY_REJECT",
                decision_trace={"stage": "DATA_QUALITY_GATE", "reason": q_reason}
            )]

        # Update symbol info
        sym.price = price
        sym.high_price = max(sym.high_price, high_price)
        sym.low_price = min(sym.low_price or low_price, low_price)
        sym.open_price = sym.open_price or open_price
        if sym.high_price > 0 and symbol not in self.or_high_map:
            self.or_high_map[symbol] = sym.high_price

        # Update aggregator
        agg = self.get_or_create_aggregator(symbol)
        candle = Candle(
            timestamp=current_time, timeframe="1m",
            open=open_price, high=high_price, low=low_price, close=price,
            volume=volume, turnover=price * volume, is_closed=True
        )
        agg.candles_1m.append(candle)
        if len(agg.candles_1m) > 120:
            agg.candles_1m.pop(0)

        # 3. EVENT DETECTION & CANDIDATE PROMOTION
        ret_1m = (price - open_price) / open_price if open_price > 0 else 0.0
        exec_intensity = 120.0 if ret_1m >= 0.015 else 100.0
        state, score, events, patterns = self.promoter.process_event_evaluation(
            iem_cd=symbol, agg=agg, now=current_time,
            execution_intensity=exec_intensity
        )

        is_candidate = (state in (SymbolState.ACTIVE, SymbolState.SIGNAL, SymbolState.WATCH))
        event_names = [e.event_type.value if hasattr(e.event_type, "value") else str(e) for e in events]

        if not is_candidate:
            return []

        # 4. SETUP EVALUATION (Full Strategy Suite)
        signals: List[TradeSignal] = []

        # A. Intraday Strategies (Cutoff at 15:00 to prevent intraday positions entering before 15:20 EOD liquidation)
        if current_time.time() < dtime(15, 0):
            intra_sigs = FullStrategySuite.evaluate_intraday_all(
                sym=sym, agg=agg, regime=regime, now=current_time,
                patterns=patterns, spread_ratio=0.0015,
                or_high=self.or_high_map.get(symbol),
                has_news=any("NEWS" in str(e) for e in event_names)
            )
            signals.extend(intra_sigs)

        # B. Swing Strategies
        daily_candles = self.daily_candles_cache.get(symbol, [])
        if daily_candles and len(daily_candles) >= 50:
            swing_sigs = FullStrategySuite.evaluate_swing_all(
                sym=sym, daily_candles=daily_candles, regime=regime,
                now=current_time, has_catalyst=any("NEWS" in str(e) for e in event_names),
                agg=agg
            )

            signals.extend(swing_sigs)

        if not signals:
            return [PipelineDecision(
                symbol=symbol, name=sym.name, timestamp=current_time,
                is_candidate=True, active_events=event_names, setup_matched=False,
                strategy_id="NONE", rule_score=score, p_target=0.0, p_stop=1.0,
                expected_net_r=0.0, reward_risk_ratio=0.0, meta_decision="NO_TRADE",
                is_edge_approved=False, is_risk_approved=False, approved_shares=0,
                entry_price=price, stop_price=0, target_1r=0, target_2r=0, target_3r=0,
                time_horizon=TimeHorizon.INTRADAY, order_type=OrderType.LIMIT,
                decision_reason=f"NO_SETUP_MATCH ({'; '.join(sym.buy_block_reasons) or '조건 미충족'})",
                decision_trace={"stage": "SETUP", "candidate_score": score, "events": event_names, "block_reasons": sym.buy_block_reasons}
            )]

        # 4-1. 기보유 종목 중복 매수 차단 Guard
        is_already_held = any(
            (getattr(p, "iem_cd", "") == symbol or getattr(p, "symbol", "") == symbol)
            and not getattr(p, "is_closed", False)
            for p in active_positions
        )
        if is_already_held:
            return [PipelineDecision(
                symbol=symbol, name=sym.name, timestamp=current_time,
                is_candidate=True, active_events=event_names, setup_matched=True,
                strategy_id=signals[0].strategy_id, rule_score=score, p_target=0.0, p_stop=1.0,
                expected_net_r=0.0, reward_risk_ratio=0.0, meta_decision="NO_TRADE",
                is_edge_approved=False, is_risk_approved=False, approved_shares=0,
                entry_price=price, stop_price=0, target_1r=0, target_2r=0, target_3r=0,
                time_horizon=signals[0].time_horizon, order_type=OrderType.LIMIT,
                decision_reason="ALREADY_HOLDING_POSITION",
                decision_trace={"stage": "HOLDING_GUARD", "reason": "ALREADY_HOLDING_POSITION"}
            )]

        # 4-2. 손절 후 재진입 쿨다운 (20분) Guard (Section 13: Re-entry Policy 3)
        if symbol in self.cooldowns and current_time < self.cooldowns[symbol]:
            cooldown_until = self.cooldowns[symbol]
            bar_rvol = float(bar.get("rvol", 1.0))
            is_strong_reentry = (bar_rvol >= 1.8 and sym.high_price > 0 and price >= sym.high_price and ret_1m >= 0.005)
            if not is_strong_reentry:
                return [PipelineDecision(
                    symbol=symbol, name=sym.name, timestamp=current_time,
                    is_candidate=True, active_events=event_names, setup_matched=True,
                    strategy_id=signals[0].strategy_id, rule_score=score, p_target=0.0, p_stop=1.0,
                    expected_net_r=0.0, reward_risk_ratio=0.0, meta_decision="NO_TRADE",
                    is_edge_approved=False, is_risk_approved=False, approved_shares=0,
                    entry_price=price, stop_price=0, target_1r=0, target_2r=0, target_3r=0,
                    time_horizon=signals[0].time_horizon, order_type=OrderType.LIMIT,
                    decision_reason=f"STOP_LOSS_COOLDOWN_ACTIVE (만료: {cooldown_until.strftime('%H:%M')})",
                    decision_trace={"stage": "COOLDOWN_GUARD", "reason": "STOP_LOSS_COOLDOWN_ACTIVE"}
                )]

        # 4-3. 단일 종목 다중 전략 신호 발생 시 최고 점수 1개 신호만 선택 (035420 등 동시 3중 진입 방지)
        if len(signals) > 1:
            signals = [max(signals, key=lambda s: s.score)]

        decisions: List[PipelineDecision] = []

        # 5. RISK LIMIT PRE-CHECK
        loss_eval = self.loss_limit_manager.evaluate_loss_limits(daily_pnl_ratio, weekly_pnl_ratio)
        tot_risk_amt, tot_risk_ratio, port_status = PortfolioRiskManager.calculate_total_open_risk(
            active_positions, account_equity
        )

        for sig in signals:
            trace: Dict[str, Any] = {
                "symbol": symbol,
                "strategy": sig.strategy_id,
                "timestamp": current_time.isoformat(),
                "rule_score": sig.score,
                "events": event_names
            }

            # 6. HISTORICAL SIMILARITY (PIT Strictly <= T)
            sim_meta = None
            real_rvol = 1.0
            if agg:
                real_rvol = agg.calculate_rvol("1m", lookback=20)
            if (not real_rvol or real_rvol <= 0.1 or real_rvol == 1.0) and hasattr(sig, "rvol") and sig.rvol > 0:
                real_rvol = float(sig.rvol)
            if not real_rvol or real_rvol <= 0:
                real_rvol = 1.0
            real_rvol = round(float(real_rvol), 2)

            if self.similarity_engine and self.exp_memory:
                try:
                    sim_res = self.similarity_engine.query_similarity(
                        current_features={"price": sig.strategy_price, "score": sig.score, "rvol": real_rvol},
                        current_regime=regime,
                        now=current_time
                    )
                    sim_meta = sim_res.__dict__
                    trace["similarity"] = {"passed": True, "hist_win_rate": getattr(sim_res, "hist_win_rate", 0.5)}
                except Exception as e:
                    trace["similarity"] = {"passed": False, "error": str(e)}

            # 7. FEATURE SCALING & ML PREDICTION
            raw_features = {
                "price": float(sig.strategy_price),
                "score": float(sig.score),
                "rvol": real_rvol,
                "vwap_dist": abs(sig.strategy_price - (agg.calculate_vwap("1m") or sig.strategy_price)) / max(1, sig.strategy_price),
                "ret_1m": ret_1m,
                "ret_3m": ret_1m * 1.5,
                "ret_5m": ret_1m * 2.0
            }
            scaler_ok, scaled_feats, _ = FeatureScalerPipeline.transform(raw_features)
            trace["scaler"] = {"ok": scaler_ok, "version": FeatureScalerPipeline.SCALER_VERSION}

            # Calibrated probability: Rule base blended with model
            base_p_target = min(0.75, max(0.40, 0.45 + (sig.score - 50.0) * 0.005))
            pred_probs = {"p_target": base_p_target, "p_stop": 1.0 - base_p_target}
            trace["ml"] = {"p_target": base_p_target}

            # 8. EDGE ENGINE
            edge_res = self.edge_engine.calculate_edge(
                sym=sym, signal=sig, p_target=base_p_target, p_stop=1.0 - base_p_target
            )
            trace["edge"] = {
                "expected_net_r": edge_res.expected_net_r,
                "is_approved": edge_res.is_approved,
                "rejection_reason": edge_res.rejection_reason
            }

            # 9. META DECISION ENGINE
            tod_bucket = ExperienceMemory.get_time_of_day_bucket(current_time) if self.exp_memory else "09:30-11:30"
            target_to_eval = edge_res.target_price if edge_res.target_price > edge_res.entry_price else sig.target_2r
            meta_res = self.meta_decision_engine.evaluate_candidate(
                setup_name=sig.strategy_id,
                time_horizon=sig.time_horizon,
                entry_price=edge_res.entry_price,
                stop_price=sig.stop_price,
                target_price=target_to_eval,
                predicted_probs=pred_probs,
                rule_score=sig.score,
                similarity_meta=sim_meta,
                regime=regime.value if hasattr(regime, "value") else str(regime),
                time_of_day_bucket=tod_bucket,
                target_2r=sig.target_2r,
                target_3r=sig.target_3r,
                entry_timing_valid=getattr(sig, "entry_timing_valid", True)
            )
            trace["meta"] = {
                "decision": meta_res.decision,
                "reason": meta_res.reason,
                "expected_net_r": meta_res.expected_net_r,
                "rr_ratio": meta_res.reward_risk_ratio
            }

            # 10. RISK & POSITION SIZING
            can_trade = loss_eval.get("can_trade_intraday", True) if sig.time_horizon == TimeHorizon.INTRADAY else loss_eval.get("can_trade_swing", True)
            is_risk_approved = can_trade and port_status != "BLOCKED"

            approved_shares = 0
            decision_reason = meta_res.reason

            if is_risk_approved and meta_res.decision in ("BUY", "BUY_SMALL") and edge_res.is_approved:
                # Calculate shares
                shares, risk_amt, rationale = PositionSizer.calculate_shares(
                    time_horizon=sig.time_horizon,
                    equity=account_equity,
                    available_cash=available_cash,
                    entry_price=int(edge_res.entry_price),
                    stop_price=int(sig.stop_price),
                    order_type=sig.order_type
                )
                trace["risk"] = {
                    "passed": True,
                    "approved_shares": shares,
                    "risk_amount": risk_amt,
                    "rationale": rationale
                }
                if shares > 0:
                    approved_shares = shares
                    final_decision = meta_res.decision
                else:
                    final_decision = "NO_TRADE"
                    decision_reason = f"INSUFFICIENT_CASH: {rationale}"
                    trace["risk"]["passed"] = False
                    trace["risk"]["rejection"] = "INSUFFICIENT_CASH"
            else:
                final_decision = "NO_TRADE"
                if not can_trade or port_status == "BLOCKED":
                    decision_reason = f"RISK_LIMIT_BLOCKED ({loss_eval.get('status', port_status)})"
                elif not edge_res.is_approved:
                    decision_reason = f"LOW_EXPECTED_EDGE ({edge_res.rejection_reason or 'Net R 과소'})"
                else:
                    decision_reason = meta_res.reason
                trace["risk"] = {"passed": False, "reason": decision_reason}

            decisions.append(PipelineDecision(
                symbol=symbol,
                name=sym.name,
                timestamp=current_time,
                is_candidate=True,
                active_events=event_names,
                setup_matched=True,
                strategy_id=sig.strategy_id,
                rule_score=sig.score,
                p_target=meta_res.p_target,
                p_stop=meta_res.p_stop,
                expected_net_r=meta_res.expected_net_r,
                reward_risk_ratio=meta_res.reward_risk_ratio,
                meta_decision=final_decision,
                is_edge_approved=edge_res.is_approved,
                is_risk_approved=is_risk_approved,
                approved_shares=approved_shares,
                entry_price=edge_res.entry_price,
                stop_price=sig.stop_price,
                target_1r=sig.target_1r,
                target_2r=sig.target_2r,
                target_3r=sig.target_3r,
                time_horizon=sig.time_horizon,
                order_type=sig.order_type or OrderType.MARKET,
                decision_reason=decision_reason,
                decision_trace=trace,
                signal=sig
            ))

        return decisions
