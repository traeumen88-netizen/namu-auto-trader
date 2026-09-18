"""v7.0 Self-Improving AI Quant Trading Engine (execution/ai_quant_trader.py)
Coordinates Full Universe Event Detection, 52-Feature Quantitative Engine,
Calibrated Probability ML Inference, Meta-Decision Gatekeeper,
Ironclad Risk Firewall, and Self-Improving Learning Lifecycle.
"""

import os
import sys
import logging
from datetime import datetime
from typing import Dict, Any, List, Optional

from core.models import (
    Tick, Candle, OrderSide, OrderType, OrderStatus, TimeHorizon, MarketRegime, TradeSignal
)
from core.tick_normalizer import normalize_price
from core.aggregator import CandleAggregator
from universe.full_universe_master import FullUniverseMaster
from core.symbol_store import SymbolStateStore
from core.low_cost_scanner import LowCostMarketScanner
from ml.features import QuantitativeFeatureEngine
from ml.opportunity_model import OpportunityPredictor
from ml.meta_decision import MetaDecisionEngine, MetaDecisionResult
from ml.trade_db import TradeDatabase, TradeRecord
from ml.drift_detector import DriftDetector
from ml.learning_pipeline import ChampionChallengerManager, LearningPipeline
from risk.circuit_breaker import CircuitBreaker
from risk.loss_limits import LossLimitManager
from risk.trading_firewall import TradingFirewall
from execution.order_router import OrderRouter
from execution.position_manager import PositionManager

logger = logging.getLogger("AIQuantTrader")


class AIQuantTrader:
    """
    v7.0 Main Production Execution Engine.
    Combines Full KRX Universe Event-Driven Architecture with Self-Improving Machine Learning.
    """

    def __init__(
        self,
        namu_client=None,
        paper_trading: bool = True,
        initial_equity: float = 100_000_000.0,
        model_dir: str = "models/opportunity",
        db_path: str = "data/trade_history_v7.db"
    ):
        self.client = namu_client
        self.paper_trading = paper_trading
        self.equity = initial_equity
        self.cash = initial_equity

        # Full Universe (all 3,136 KRX symbols, zero hardcoding)
        self.master_symbols = FullUniverseMaster.load_full_universe()
        self.store = SymbolStateStore(self.master_symbols)
        self.scanner = LowCostMarketScanner(self.store)

        # Quantitative Engines
        self.feature_engine = QuantitativeFeatureEngine()
        self.champion_model = OpportunityPredictor(version="v7.0_champion", model_dir=model_dir)
        self.meta_decision = MetaDecisionEngine()
        self.trade_db = TradeDatabase(db_path=db_path)
        self.drift_detector = DriftDetector()
        self.cc_manager = ChampionChallengerManager()
        self.learning_pipeline = LearningPipeline(models_dir=model_dir)

        # Risk & Execution Architecture
        self.circuit_breaker = CircuitBreaker()
        self.loss_limit_mgr = LossLimitManager()
        self.firewall = TradingFirewall(self.circuit_breaker, self.loss_limit_mgr)
        self.order_router = OrderRouter(namu_client, self.circuit_breaker)
        self.position_mgr = PositionManager(self.order_router)

        # Aggregators per symbol
        self.aggregators: Dict[str, CandleAggregator] = {}
        self.current_regime = MarketRegime.STRONG_BULL

        # Portfolio Risk Manager Unlimited Holdings Policy
        from risk.portfolio_risk import PortfolioRiskManager
        PortfolioRiskManager.UNLIMITED_MODE = True
        print("==================================================")
        print("[포트폴리오 정책] 보유 종목 수 무제한 정책 적용 (UNLIMITED_HOLDINGS)")
        print("  MAX_POSITION_COUNT     = UNLIMITED")
        print("  POSITION_COUNT_BLOCK   = FALSE")
        print("  POSITION_COUNT_CHECK   = BYPASSED / NOT_USED")
        print("  자금/위험 기준 통제    = 현금 부족(INSUFFICIENT_CASH), 리스크 초과(PORTFOLIO_RISK_LIMIT)")
        print("==================================================")

    def get_or_create_aggregator(self, symbol: str) -> CandleAggregator:
        if symbol not in self.aggregators:
            self.aggregators[symbol] = CandleAggregator(symbol)
        return self.aggregators[symbol]

    def on_tick(self, tick: Tick) -> Optional[TradeSignal]:
        """
        Processes an incoming real-time tick across any stock in KRX.
        """
        # 1. Update circuit breaker heartbeat
        self.circuit_breaker.update_data_heartbeat(tick.timestamp)

        # 2. Update aggregator
        agg = self.get_or_create_aggregator(tick.iem_cd)
        agg.on_tick(tick)

        # 3. Check active position management
        self.position_mgr.update_price_and_manage(tick.iem_cd, tick.price, tick.timestamp)

        # 4. If enough candles have formed, extract features and evaluate ML opportunity
        if len(agg.candles_1m) >= 15:
            return self.evaluate_opportunity(tick.iem_cd, tick.price, agg, tick.timestamp)

        return None

    def evaluate_opportunity(
        self,
        symbol: str,
        current_price: float,
        aggregator: CandleAggregator,
        timestamp: datetime
    ) -> Optional[TradeSignal]:
        """
        Calculates 52 features, predicts with Champion (and Challenger),
        and runs Meta-Decision Gatekeeper and Trading Firewall.
        """
        # Extract features
        features = self.feature_engine.calculate_features(
            symbol=symbol,
            aggregator=aggregator,
            current_price=current_price,
            market_regime=self.current_regime,
            timestamp=timestamp
        )

        # ML Model Inference
        pred_probs = self.champion_model.predict_opportunity(features)

        # Dynamic Stop & Target based on ATR & Tick Size
        atr = features.get("atr_1m_ratio", 0.01) * current_price
        stop_price = normalize_price(current_price - max(atr, current_price * 0.01), "SELL", "LIMIT")
        target_price = normalize_price(current_price + max(atr * 2.0, current_price * 0.02), "SELL", "LIMIT")

        # Meta-Decision Gatekeeper
        decision: MetaDecisionResult = self.meta_decision.evaluate_candidate(
            setup_name="AI_MOMENTUM_BREAKOUT",
            time_horizon=TimeHorizon.INTRADAY,
            entry_price=current_price,
            stop_price=stop_price,
            target_price=target_price,
            predicted_probs=pred_probs,
            chase_ratio=features.get("dev_vwap", 0.0)
        )

        if not decision.approved:
            return None

        # Build Candidate Signal
        sym_obj = self.store.get_symbol(symbol)
        stock_name = sym_obj.name if sym_obj else symbol
        signal = TradeSignal(
            strategy_id="AI_QUANT_V7",
            time_horizon=TimeHorizon.INTRADAY,
            iem_cd=symbol,
            name=stock_name,
            side=OrderSide.BUY,
            strategy_price=current_price,
            stop_price=stop_price,
            score=decision.p_target * 100.0,
            reason=f"P(Target)={decision.p_target:.2f}, E[Net R]={decision.expected_net_r:+.2f}R",
            timestamp=timestamp,
            target_1r=normalize_price(current_price + abs(current_price - stop_price), "SELL", "LIMIT"),
            target_2r=target_price
        )

        # Calculate position shares via Fractional Kelly recommended risk
        risk_amount = self.equity * decision.recommended_risk_pct
        risk_per_share = abs(current_price - stop_price)
        shares = int(risk_amount // risk_per_share) if risk_per_share > 0 else 0

        # Run Trading Firewall (Absolute Veto Layer)
        verdict = self.firewall.check_firewall(
            signal=signal,
            shares=shares,
            order_price=current_price,
            equity=self.equity,
            cash=self.cash,
            current_regime=self.current_regime,
            active_positions=self.position_mgr.positions,
            now=timestamp
        )

        if not verdict.approved:
            logger.info(f"Firewall Veto for {symbol}: {verdict.veto_reason}")
            return None

        # Execute Order
        if verdict.shares > 0:
            order = self.order_router.submit_order(
                signal=signal,
                shares=verdict.shares,
                order_type=OrderType.LIMIT,
                order_price=verdict.normalized_price,
                balance={"cash": self.cash},
                portfolio_risk_status="NORMAL"
            )

            if order and order.status == OrderStatus.FILLED:
                self.position_mgr.open_position(
                    time_horizon=signal.time_horizon,
                    strategy_id=signal.strategy_id,
                    iem_cd=signal.iem_cd,
                    name=signal.name,
                    qty=verdict.shares,
                    entry_price=verdict.normalized_price,
                    stop_price=signal.stop_price,
                    target_1r=signal.target_1r,
                    target_2r=signal.target_2r,
                    target_3r=normalize_price(verdict.normalized_price + 3 * risk_per_share, "SELL", "LIMIT"),
                    initial_risk=verdict.shares * risk_per_share
                )
                self.cash -= verdict.normalized_price * verdict.shares
                return signal

        return None

    def run_cycle(self):
        """실시간 모니터링 1회 순환 사이클 (v7.0 Self-Improving AI Quant Engine)"""
        now = datetime.now()
        self.circuit_breaker.update_data_heartbeat(now)

        # 1. 쿨다운 만료 종목 자동 복귀
        self.store.check_cooldown_expiry(now)

        # 2. 계좌 현황 및 잔고 조회
        if self.client:
            try:
                balance = self.client.get_balance()
                self.equity = float(balance.get("total_asset", self.equity))
                self.cash = float(balance.get("cash", self.cash))
                pnl = float(balance.get("total_profit", 0.0))
                pnl_rate = float(balance.get("total_profit_rate", 0.0))
                print(f"\n[{now.strftime('%Y-%m-%d %H:%M:%S')}] ─── [v7.0 SELF-IMPROVING AI QUANT 모니터링 주기] ───")
                print(f"[계좌 현황] 총자산: {int(self.equity):,}원 | 예수금: {int(self.cash):,}원 | 평가손익: {int(pnl):,}원 ({pnl_rate:+.2f}%)")
            except Exception as e:
                print(f"[계좌 오류] 잔고 조회 실패: {e}")
        else:
            print(f"\n[{now.strftime('%Y-%m-%d %H:%M:%S')}] ─── [v7.0 SELF-IMPROVING AI QUANT 모니터링 (모의/검증 모드)] ───")
            print(f"[자산 현황] 총자산: {int(self.equity):,}원 | 예수금: {int(self.cash):,}원 | 활성 포지션: {len(self.position_mgr.positions)}개")

        # 3. 모델 레지스트리 상태 출력
        challenger_str = f"{self.cc_manager.challenger_version} (State: {self.cc_manager.challenger_state}, Alloc: {self.cc_manager.challenger_allocation*100:.0f}%)" if self.cc_manager.challenger_version else "None"
        print(f"[AI MODEL] Active Champion: {self.champion_model.model_id} | Challenger: {challenger_str}")

        # 4. 전체 시장 순환 탐색 및 이벤트/피처 분석
        active_symbols = (
            self.store.get_by_state(SymbolState.POSITION)
            + self.store.get_by_state(SymbolState.SIGNAL)
            + self.store.get_by_state(SymbolState.ACTIVE)
            + self.store.get_by_state(SymbolState.WATCH)
        )
        print(f"[MARKET UNIVERSE] 총 {self.store.total_count():,}개 전 종목 감시 활성 (감시/신호/보유 후보: {len(active_symbols)}개)")

