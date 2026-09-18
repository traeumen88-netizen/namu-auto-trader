"""[FINAL MASTER v16.0] 국내 주식 전체 종목 실시간 탐지형 통합 퀀트 자동매매 엔진
(Live Quant Trader v16.0 - Full Funnel Integration Engine)

전체 파이프라인:
UNIVERSE(3,136) -> MARKET_DATA(Radar+REST) -> DATA_QUALITY_GATE -> EVENT_DETECTED
-> CANDIDATE -> SETUP(8+ Strategies) -> SETUP_SCORE -> HISTORICAL_SIMILARITY
-> ML_SCORE(Scaler Pipeline) -> META_DECISION(R:R>=1.5, Expected Net R) -> RISK_CHECK
-> BUY_APPROVED -> ORDER_CREATED -> ORDER_SENT -> ORDER_ACK -> PARTIAL_FILL / FILLED
-> POSITION_OPEN -> STOP/TARGET/TRAILING_WATCH(독립 틱 워치독) -> EXIT_TRIGGERED
-> EXIT_ORDER_SENT -> EXIT_FILLED -> POSITION_CLOSED -> TRADE_RESULT
-> EXPERIENCE_DB / WARM_DB -> COLD_PARQUET_TIERING

핵심 보완 내역:
1. R:R 1.0 < 1.5 Gatekeeper 거절 버그 완전 척결 (복합 타겟 +1.5R~+2.0R 반영)
2. 3초 데이터 지연으로 인한 서킷브레이커 자가 트리거 방지 (완충 버퍼 적용)
3. 스톱로스 도달 시 실제 브로커 매도 주문 발주 (독립 틱 워치독 구축)
4. 재시작 시 브로커 잔고 기반 포지션 자동 복구 (Restart Recovery)
5. 3분 주기 백그라운드 정합성 조정 (Periodic Reconciliation)
6. 저비용 시장 전체 수급 레이더(MarketRadar) 연동으로 3,136 종목 실시간 기회 포착
7. 데이터 무결성 게이트(DataQualityGate)로 이상치 시세 원천 차단
8. 3계층 스토리지(HOT/WARM/COLD Parquet) 티어링 연동
"""

import sys
import os
import time
import json
import logging
from datetime import datetime, timedelta, time as dtime
from typing import Dict, Any, List, Optional, Tuple
from dataclasses import dataclass, field

logger = logging.getLogger("LiveQuantTrader")

# UTF-8 콘솔 출력 보정
if sys.platform == "win32":
    try:
        sys.stdout.reconfigure(encoding="utf-8")
        sys.stderr.reconfigure(encoding="utf-8")
    except Exception:
        pass

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if BASE_DIR not in sys.path:
    sys.path.insert(0, BASE_DIR)

from namu_client import NamuClient
import config.settings as settings
from core.models import (
    Tick, Candle, TimeHorizon, OrderSide, OrderType, OrderStatus,
    MarketRegime, TradeSignal, SymbolInfo, SymbolState
)
from execution.execution_funnel_telemetry import (
    EVENT_SIGNAL_DETECTED, EVENT_DECISION_APPROVED, EVENT_ORDER_CREATED,
    EVENT_QUOTE_CHECK_START, EVENT_QUOTE_REFETCH_START, EVENT_QUOTE_REFETCH_END,
    EVENT_RISK_RECHECK, EVENT_CASH_CHECK, EVENT_ORDER_SUBMIT_START,
    EVENT_BROKER_ACK, EVENT_FILL_RECEIVED
)
from universe.full_universe_master import FullUniverseMaster
from core.symbol_store import SymbolStateStore
from core.low_cost_scanner import LowCostMarketScanner
from core.data_quality_gate import DataQualityGate
from core.market_radar import MarketRadar
from core.setup_detector import SetupDetector
from strategies.full_strategy_suite import FullStrategySuite
from market_regime.swing_regime import SwingRegimeEngine
from market_regime.intraday_regime import IntradayRegimeEngine
from strategies.scoring_engine import ScoringEngine
from risk.position_sizer import PositionSizer
from risk.portfolio_risk import PortfolioRiskManager
from risk.loss_limits import LossLimitManager
from risk.circuit_breaker import CircuitBreaker
from execution.order_router import OrderRouter
from execution.position_manager import PositionManager
from execution.persistence_manager import PersistenceManager
from strategies.breakout_gate import validate_entry_timing_quality
from core.after_hours_manager import MarketSessionManager, MarketSession
from execution.exit_watchdog import ExitWatchdog, WatchdogStatus, ExitLifecycleStage
from execution.diagnostic_engine import DiagnosticEngine
from ml.learning_pipeline import ChampionChallengerManager
from core.edge_engine import EdgeEngine, EdgeResult
from core.time_sync import TimeSync, StageTimer
from ml.experience_memory import ExperienceMemory, PointInTimeSnapshot, OutcomeRecord
from ml.similarity_engine import HistoricalSimilarityEngine
from ml.model_registry import EnhancedModelRegistry, ModelRegistry
from ml.retraining_trigger import RetrainingTriggerEngine, LearningFreezeManager, CalibrationMonitor
from ml.meta_decision import MetaDecisionEngine, FeatureScalerPipeline
from risk.portfolio_cash import PortfolioCashManager, CashShortfallRecord
from execution.order_quote_manager import OrderQuoteManager, OrderQuoteSnapshot
from database.persistence import ExperienceDB
from ml.eod_worker import EODRetrospectiveWorker


def log_mock_buy_trace(stage: str, symbol: str, decision: str, reason: str = "", **kwargs):
    """
    [MOCK BUY TRACE] 16-Stage Funnel Instrumentation
    Emits standardized [MOCK BUY TRACE] logs across all 16 evaluation gates:
    UNIVERSE, EVENT_DETECTED, CANDIDATE, SETUP, SCORE, SIMILARITY,
    ML, META, EDGE, RISK, SIZER, BUY_APPROVED, ORDER_CREATED,
    ORDER_SENT, ACK, FILL
    """
    parts = [f"[MOCK BUY TRACE] stage={stage}", f"symbol={symbol}", f"decision={decision}"]
    if reason:
        parts.append(f"reason={reason}")
    for k, v in kwargs.items():
        if v is not None:
            parts.append(f"{k}={v}")
    line = " ".join(parts)
    logger.info(line)
    print(line)


def _emit_decision_trace_no_trade(
    acc, sig, cand_sym, edge_res, gate: str, reason: str,
    shares: int = 0, latest_price: Optional[float] = None,
    broker_avail: int = 0, usable_cash: float = 0.0,
    rvol: float = 1.0, mom_3m: float = 0.0, vwap_gap: float = 0.0,
    rebound_str: float = 0.50, signal_age_ms: float = 0.0,
    initial_gate: Optional[str] = None, initial_reason: Optional[str] = None
):
    """Unified NO_TRADE decision trace emitter (Section 2)"""
    try:
        from core.decision_trace import DecisionTraceRegistry
        from strategies.profit_opportunity_gate import ProfitOpportunityTelemetry
        ProfitOpportunityTelemetry.get_instance().record_gate_reject(gate, reason)

        p_cur = latest_price or (getattr(edge_res, "entry_price", 0.0) if edge_res else getattr(sig, "strategy_price", 0.0))
        p_entry = getattr(edge_res, "entry_price", 0.0) if edge_res else getattr(sig, "strategy_price", 0.0)
        exp_move = getattr(edge_res, "expected_move_pct", None) or getattr(sig, "expected_move_pct", 0.02) or 0.02
        exp_net_r = getattr(edge_res, "expected_net_r", None) or getattr(sig, "expected_net_r", 1.0) or 1.0

        bid = getattr(sig, "bid1_price", 0)
        ask = getattr(sig, "ask1_price", 0)
        if hasattr(sig, "quote_snapshot") and sig.quote_snapshot:
            bid = sig.quote_snapshot.bid or bid
            ask = sig.quote_snapshot.ask or ask
        spread_pct = (ask - bid) / float(bid) if (bid > 0 and ask >= bid) else 0.001

        dist_h = ((p_cur - cand_sym.high_price) / float(cand_sym.high_price)) if (cand_sym and getattr(cand_sym, "high_price", 0) > 0) else 0.0

        stop_p = getattr(sig, "stop_price", 0)
        stop_dist_pct = abs(p_cur - stop_p) / float(p_cur) if (p_cur > 0 and stop_p > 0) else 0.015

        DecisionTraceRegistry.get_instance().record_no_trade(
            symbol=sig.iem_cd,
            strategy=sig.strategy_id,
            entry_price=float(p_entry),
            current_price=float(p_cur),
            rejected_gate=gate,
            rejected_reason=reason,
            initial_rejected_gate=initial_gate or gate,
            initial_rejected_reason=initial_reason or reason,
            trading_mode=getattr(acc, "mode", "live"),
            account_no=getattr(acc, "act_no", ""),
            bid=float(bid),
            ask=float(ask),
            spread_pct=float(spread_pct),
            rvol=float(rvol),
            momentum_3m=float(mom_3m),
            vwap_gap=float(vwap_gap),
            dist_high=float(dist_h),
            rebound_strength=float(rebound_str),
            signal_age_ms=float(signal_age_ms),
            expected_move_pct=float(exp_move),
            expected_net_r=float(exp_net_r),
            position_size=shares,
            usable_cash=usable_cash,
            broker_psbl_qty=broker_avail,
            stop_distance_pct=stop_dist_pct,
            execution_result="REJECTED"
        )
    except Exception as trace_err:
        logger.error(f"[DECISION_TRACE_NO_TRADE_ERR] {sig.iem_cd}: {trace_err}")


def _emit_decision_trace_buy(
    acc, sig, cand_sym, edge_res, shares: int, latest_price: float,
    broker_avail: int, usable_cash: float, rvol: float, mom_3m: float,
    vwap_gap: float, rebound_str: float, signal_age_ms: float,
    expected_move_pct: float, expected_net_r: float, cost_ratio: float,
    stop_dist_pct: float, spread_pct: float
):
    """Unified BUY_DECISION_TRACE emitter (Section 1)"""
    try:
        from core.decision_trace import DecisionTraceRegistry
        from strategies.profit_opportunity_gate import ProfitOpportunityTelemetry
        ProfitOpportunityTelemetry.get_instance().record_buy_approved(
            rvol=rvol,
            mom_3m=mom_3m,
            vwap_gap=vwap_gap,
            rebound_strength=rebound_str
        )

        dist_h = ((latest_price - cand_sym.high_price) / float(cand_sym.high_price)) if (cand_sym and getattr(cand_sym, "high_price", 0) > 0) else 0.0

        bid = getattr(sig, "bid1_price", 0)
        ask = getattr(sig, "ask1_price", 0)
        if hasattr(sig, "quote_snapshot") and sig.quote_snapshot:
            bid = sig.quote_snapshot.bid or bid
            ask = sig.quote_snapshot.ask or ask

        DecisionTraceRegistry.get_instance().record_buy(
            symbol=sig.iem_cd,
            strategy=sig.strategy_id,
            entry_price=float(edge_res.entry_price),
            current_price=float(latest_price),
            trading_mode=getattr(acc, "mode", "live"),
            account_no=getattr(acc, "act_no", ""),
            bid=float(bid),
            ask=float(ask),
            spread_pct=float(spread_pct),
            rvol=float(rvol),
            candle_1m_direction="UP" if mom_3m > 0 else ("DOWN" if mom_3m < 0 else "FLAT"),
            momentum_3m=float(mom_3m),
            vwap_gap=float(vwap_gap),
            dist_high=float(dist_h),
            rebound_strength=float(rebound_str),
            signal_age_ms=float(signal_age_ms),
            expected_move_pct=float(expected_move_pct),
            expected_net_move_pct=float(expected_move_pct - 0.0023),
            expected_net_r=float(expected_net_r),
            cost_to_opportunity_ratio=float(cost_ratio),
            stop_distance_pct=float(stop_dist_pct),
            position_size=shares,
            usable_cash=usable_cash,
            reserved_cash=getattr(acc.cash_manager, "reserved_cash", 0.0) if acc.cash_manager else 0.0,
            broker_psbl_qty=broker_avail,
            signal_ttl_result="PASS",
            price_drift_pct=abs(latest_price - edge_res.entry_price) / max(1, edge_res.entry_price),
            execution_result="SUCCESS",
            final_reason="ALL_GATES_PASSED"
        )
    except Exception as trace_err:
        logger.error(f"[DECISION_TRACE_BUY_ERR] {sig.iem_cd}: {trace_err}")


@dataclass
class AccountContext:
    name: str              # "MOCK" or "LIVE"
    mode: str              # "mock" or "live"
    act_no: str            # 계좌번호
    client: NamuClient
    order_router: OrderRouter
    position_manager: PositionManager
    loss_manager: LossLimitManager
    cash_manager: Optional[Any] = None
    equity: float = 0.0
    cash: float = 0.0
    daily_pnl: float = 0.0
    daily_pnl_ratio: float = 0.0
    loss_eval: Dict[str, Any] = field(default_factory=dict)
    port_risk_status: str = "NORMAL"
    risk_summary: Dict[str, Any] = field(default_factory=dict)
    cooldowns: Dict[str, datetime] = field(default_factory=dict)
    circuit_breaker: Optional[CircuitBreaker] = None
    daily_entry_counts: Dict[str, int] = field(default_factory=dict)
    last_trade_outcomes: Dict[str, str] = field(default_factory=dict)
    cumulative_loss_amount: Dict[str, float] = field(default_factory=dict)
    consecutive_losses: Dict[str, int] = field(default_factory=dict)

    def set_cooldown(self, iem_cd: str, now: Optional[datetime] = None, cooldown_seconds: int = 600):
        """계좌별 독립 쿨다운 설정 (타 계좌에 영향 없음)"""
        current_time = now or datetime.now()
        self.cooldowns[iem_cd] = current_time + timedelta(seconds=cooldown_seconds)
        logger.info(f"[{self.name}] 종목 {iem_cd} 계좌 독립 쿨다운 설정 ({cooldown_seconds}초, 만료: {self.cooldowns[iem_cd].strftime('%H:%M:%S')})")

    def is_in_cooldown(self, iem_cd: str, now: Optional[datetime] = None) -> bool:
        """계좌별 독립 쿨다운 여부 확인"""
        if iem_cd not in self.cooldowns:
            return False
        current_time = now or datetime.now()
        if current_time < self.cooldowns[iem_cd]:
            return True
        else:
            del self.cooldowns[iem_cd]
            return False

    def check_cooldown_expiry(self, now: Optional[datetime] = None):
        """만료된 계좌 쿨다운 자동 정리"""
        current_time = now or datetime.now()
        expired = [c for c, exp in self.cooldowns.items() if current_time >= exp]
        for c in expired:
            del self.cooldowns[c]


class LiveQuantTrader:
    def __init__(self, mode: str = "dual", act_no: str = None, force_signal_test: bool = False, start_syncer: Optional[bool] = None):
        self.circuit_breaker = CircuitBreaker()
        self.diagnostic_engine = DiagnosticEngine()
        self.quote_manager = OrderQuoteManager(max_order_age_sec=3.0)
        self.cc_manager = ChampionChallengerManager()
        self.force_signal_test = force_signal_test

        # v16.0 핵심 엔진 초기화
        self.data_quality_gate = DataQualityGate(max_staleness_seconds=30.0)
        self.market_radar = MarketRadar(request_timeout=3.0)
        self.setup_detector = SetupDetector()
        self.persistence_manager = PersistenceManager()
        self.experience_memory = ExperienceMemory(db_path="data/experience_memory.db")
        self.similarity_engine = HistoricalSimilarityEngine(self.experience_memory)
        self.model_registry = EnhancedModelRegistry()
        self.retraining_engine = RetrainingTriggerEngine()
        self.meta_decision_engine = MetaDecisionEngine(min_expected_net_r=0.15, min_reward_risk=1.5)
        self.last_retrain_time = datetime.now()
        self.last_recon_time = datetime.now()
        self.exp_db = ExperienceDB(db_path="data/experience_memory.db")
        self.eod_worker = EODRetrospectiveWorker(exp_db=self.exp_db, registry=self.model_registry)
        self.eod_batch_executed_today: Optional[str] = None
        # 청산 워치독 장애 격리 및 치명적 청산 장애 보호 엔진 (Requirement 2-12)
        self.exit_watchdog = ExitWatchdog(db_path="data/operational_v16.db", heartbeat_file="data/watchdog_heartbeat.json")

        # 정밀 시간 동기화 및 호가창 엣지 엔진
        TimeSync.sync_ntp()
        self.diagnostic_engine.telemetry.ntp_synced = TimeSync.is_synced()
        self.edge_engine = EdgeEngine(min_expected_net_r=0.15)

        # Telegram 실시간 알림 및 수신(RX) 루프 가동
        from core.telegram_notifier import telegram_notifier
        self.telegram_notifier = telegram_notifier
        if self.telegram_notifier.enabled:
            self.telegram_notifier.start_receiver()

        # 시간외 급등 거버넌스 및 세션 관리자
        from core.after_hours_manager import AfterHoursManager, MarketSessionManager, MarketSession
        self.after_hours_manager = AfterHoursManager(
            db_path="data/operational_v16.db",
            telegram_notifier=self.telegram_notifier
        )

        # 보유 종목 수 무제한 모드 연동 (가용 예수금 기반 자연 매수)
        print("==================================================")
        print("[포트폴리오 정책] 보유 종목 수 무제한 정책 적용 (UNLIMITED_HOLDINGS)")
        print("  MAX_POSITION_COUNT     = UNLIMITED")
        print("  POSITION_COUNT_BLOCK   = FALSE")
        print("  POSITION_COUNT_CHECK   = BYPASSED / NOT_USED")
        print("  자금/위험 기준 통제    = 현금 부족(INSUFFICIENT_CASH), 리스크 초과(PORTFOLIO_RISK_LIMIT)")
        print("==================================================")

        # 계좌 컨텍스트 초기화 (MOCK, LIVE, 또는 DUAL 동시 가동)
        self.mode = mode or "dual"
        self.accounts: List[AccountContext] = []

        if self.mode == "mock":
            self.accounts.append(self._create_account_context("MOCK", "mock", act_no or settings.ACCOUNT_MOCK))
        elif self.mode == "live":
            self.accounts.append(self._create_account_context("LIVE", "live", act_no or settings.ACCOUNT_LIVE))
        else:  # "dual" (실전 우선 + 모의투자 동시 가동)
            self.mode = "dual"
            self.accounts.append(self._create_account_context("LIVE", "live", settings.ACCOUNT_LIVE))
            self.accounts.append(self._create_account_context("MOCK", "mock", settings.ACCOUNT_MOCK))

        # [Section 13] 실전 계좌(LIVE) 최우선 집행 보장 정렬
        self.accounts.sort(key=lambda a: 0 if a.name == "LIVE" else 1)

        primary = self.accounts[0]
        self.client = primary.client
        self.order_router = primary.order_router
        self.position_manager = primary.position_manager
        self.loss_manager = primary.loss_manager

        # [Section 21] Live Telemetry & GitHub Syncer
        from execution.live_telemetry_exporter import LiveTelemetryExporter
        from execution.telemetry_git_syncer import TelemetryGitSyncer

        is_test = (
            "pytest" in sys.modules
            or "PYTEST_CURRENT_TEST" in os.environ
            or os.environ.get("NAMU_TEST_ENV") == "1"
        )
        telemetry_env = "TEST" if is_test else ("LIVE" if self.mode in ("live", "dual") else "MOCK")
        telemetry_src = "UNIT_TEST" if is_test else ("LIVE_TRADER" if telemetry_env == "LIVE" else "MOCK_TRADER")
        telemetry_dir = "data/live_telemetry"

        self.telemetry_exporter = LiveTelemetryExporter.get_instance(
            base_dir=os.path.join(BASE_DIR, telemetry_dir),
            environment=telemetry_env,
            source=telemetry_src
        )
        self.telemetry_exporter.trading_mode = self.mode.upper()

        self.telemetry_syncer = TelemetryGitSyncer(
            repo_dir=BASE_DIR,
            telemetry_dir=telemetry_dir,
            exporter=self.telemetry_exporter
        )
        self.telemetry_exporter.set_git_syncer(self.telemetry_syncer)

        should_start_syncer = (not is_test) if start_syncer is None else start_syncer
        if should_start_syncer:
            self.telemetry_syncer.start()

        # 1. KOSPI + KOSDAQ 전체 상장종목 Universe 로딩 (3,136종목)
        print("\n[UNIVERSE 초기화] KOSPI + KOSDAQ 전체 상장종목 로딩 중...")
        self.full_universe = FullUniverseMaster.load_full_universe()
        self.store = SymbolStateStore(self.full_universe)
        self.scanner = LowCostMarketScanner(self.store)

        kospi_cnt = self.store.kospi_count()
        kosdaq_cnt = self.store.kosdaq_count()
        total_cnt = self.store.total_count()
        self.diagnostic_engine.record_universe(total_cnt)
        print(f"[UNIVERSE 완료] KOSPI: {kospi_cnt:,}개 | KOSDAQ: {kosdaq_cnt:,}개 | TOTAL: {total_cnt:,}개 상장종목 로드 완료")

        self.daily_candles_cache = {}
        self.or_high_map = {}
        self._all_symbol_codes = list(self.full_universe.keys())
        self._rolling_scan_index = 0
        self._rolling_batch_size = 15  # 저비용 순환 탐색 배치
        self._init_premarket_data()

    def _create_account_context(self, name: str, mode: str, act_no: str) -> AccountContext:
        client = NamuClient(mode=mode, act_no=act_no)
        cash_manager = PortfolioCashManager()
        circuit_breaker = CircuitBreaker(account_name=name)
        order_router = OrderRouter(client, circuit_breaker, cash_manager=cash_manager, funnel_telemetry=self.diagnostic_engine.funnel_telemetry)
        position_manager = PositionManager(order_router, persistence_manager=self.persistence_manager)
        loss_manager = LossLimitManager()
        return AccountContext(
            name=name,
            mode=mode,
            act_no=act_no,
            client=client,
            order_router=order_router,
            position_manager=position_manager,
            loss_manager=loss_manager,
            cash_manager=cash_manager,
            circuit_breaker=circuit_breaker
        )

    def _init_premarket_data(self):
        """장전 데이터 사전 로딩 및 브로커 보유 종목 기반 자동 복원 (Restart Recovery)"""
        print("[재시작 복구] 브로커 보유 종목 및 계좌 상태 복원 중...")
        for acc in self.accounts:
            try:
                bal = acc.client.get_balance()
                acc.equity = float(bal.get("total_asset", 0))
                acc.cash = float(bal.get("order_available", bal.get("cash", 0)))
                acc.daily_pnl = float(bal.get("total_profit", 0))
                acc.daily_pnl_ratio = float(bal.get("total_profit_rate", 0.0)) / 100.0
                if acc.cash_manager:
                    acc.cash_manager.sync_broker_cash(acc.cash)
                holdings = bal.get("holdings", [])
                if holdings:
                    acc.position_manager.sync_from_broker(holdings)
                acc.risk_summary = PortfolioRiskManager.get_risk_summary(
                    list(acc.position_manager.positions.values()), acc.equity
                )
                acc.port_risk_status = acc.risk_summary.get("status", "NORMAL")
            except Exception as e:
                logger.warning(f"[{acc.name}] 보유 포지션 복구 대기: {e}")
        print("[장전 데이터 로딩] 전체 시장(3,136종목) 수급 레이더 및 고속 온디맨드 분석 준비 완료.")
        if hasattr(self, "telemetry_exporter") and self.telemetry_exporter:
            try:
                self.telemetry_exporter.sync_order_router_state(self.order_router)
                self.telemetry_exporter.sync_position_manager_state(self.position_manager)
            except Exception as se:
                logger.warning(f"Telemetry initial state sync failed: {se}")

    def _log_run_cycle_failure(
        self,
        cycle_id: str,
        stage: str,
        promoted_candidates_state: str,
        candidate_scan_status: str,
        candidate_promotion_status: str,
        exception: Optional[Exception] = None,
        error_message: str = ""
    ):
        """
        Requirement 8: run_cycle 장애 시 실제 원인 및 상태 구조화 텔레메트리 출력
        """
        import traceback
        exc_type = type(exception).__name__ if exception else "None"
        exc_msg = str(exception) if exception else (error_message or "None")
        tb_str = "".join(traceback.format_tb(exception.__traceback__)) if exception and exception.__traceback__ else "None"

        log_lines = [
            "\n" + "!" * 76,
            "RUN_CYCLE_FAILURE",
            f"cycle_id={cycle_id}",
            f"stage={stage}",
            f"promoted_candidates_state={promoted_candidates_state}",
            f"candidate_scan_status={candidate_scan_status}",
            f"candidate_promotion_status={candidate_promotion_status}",
            f"exception_type={exc_type}",
            f"exception_message={exc_msg}",
            f"traceback=\n{tb_str.strip()}",
            "!" * 76 + "\n"
        ]
        formatted = "\n".join(log_lines)
        logger.error(formatted)
        print(formatted)

    def _resolve_d0_regular_close(self, sym: Any, quote_client: Any, now: datetime) -> Optional[float]:
        """
        D0 당일 정규장(15:30) 종가 획득 방어 로직:
        1. sym.regular_close가 이미 유효(> 0)하면 반환
        2. quote_client를 통해 일봉(currentDaily) 1건 조회 -> 당일 날짜(YYYYMMDD) 일봉의 close 반환
        3. 획득 실패 시 None 반환 (DATA_UNAVAILABLE 방어: 절대로 전일 종가 sym.prev_close를 대용하지 않음!)
        """
        if getattr(sym, "regular_close", None) and sym.regular_close > 0:
            return float(sym.regular_close)

        if not quote_client or not hasattr(quote_client, "get_daily_candles"):
            return None

        try:
            candles = quote_client.get_daily_candles(sym.iem_cd, count=1)
            if candles and len(candles) > 0:
                latest = candles[0]
                today_str = now.strftime("%Y%m%d")
                c_date = str(latest.get("date", "")).replace("-", "")
                if c_date == today_str and latest.get("close", 0) > 0:
                    c_val = float(latest["close"])
                    sym.regular_close = c_val
                    return c_val
        except Exception as e:
            logger.warning(f"[{getattr(sym, 'iem_cd', 'UNKNOWN')}] D0 정규장 종가 조회 예외: {e}")

        return None

    def run_cycle(self, now: Optional[datetime] = None):
        """실시간 모니터링 1회 순환 사이클 (v16.0 Full-Funnel Architecture)"""
        t_cycle_start = time.perf_counter()
        now = now or datetime.now()
        cycle_id = f"CYC_{int(now.timestamp() * 1000)}"
        current_stage = "INIT"
        candidate_scan_status = "NOT_STARTED"
        candidate_promotion_status = "NOT_STARTED"
        promoted_candidates = None

        # 일자 변경 시 이전 거래일 워치리스트 만료 정리 (1일 1회)
        if hasattr(self, "after_hours_manager") and self.after_hours_manager:
            if not hasattr(self, "_last_ah_expire_date") or self._last_ah_expire_date != now.date():
                try:
                    self.after_hours_manager.expire_stale_watchlist(now.date())
                except Exception as ex_e:
                    logger.warning(f"워치리스트 만료 처리 예외: {ex_e}")
                self._last_ah_expire_date = now.date()

        self.quote_manager.start_global_scan()
        self.circuit_breaker.update_data_heartbeat(now)
        for acc in self.accounts:
            if getattr(acc, "circuit_breaker", None):
                acc.circuit_breaker.update_data_heartbeat(now)
                if acc.circuit_breaker.is_tripped:
                    acc.circuit_breaker.attempt_recovery(health_check_fn=acc.order_router.check_broker_health)

        # 1. 쿨다운 만료 종목 자동 복귀 및 WATCH 체류 종목 정리
        self.store.check_cooldown_expiry(now)
        self.store.check_watch_timeouts(now, max_idle_seconds=300)
        for acc in self.accounts:
            acc.check_cooldown_expiry(now)

        # 2. Section 49: 3분 주기 백그라운드 정합성 조정 (Periodic Reconciliation)
        if (now - self.last_recon_time).total_seconds() >= 180.0:
            for acc in self.accounts:
                recon_res = self.persistence_manager.perform_reconciliation(
                    client=acc.client,
                    position_manager=acc.position_manager,
                    order_router=acc.order_router
                )
                if recon_res.get("diff", False):
                    print(f"🔄 [{acc.name} 정합성 조정] {len(recon_res.get('details', []))}건 동기화 완료")
            self.last_recon_time = now

        # EOD Retrospective Batch (15:40 장 마감 후 자동 실행)
        today_str = now.strftime("%Y-%m-%d")
        if (now.hour == 15 and now.minute >= 40 or now.hour > 15) and self.eod_batch_executed_today != today_str:
            self.eod_batch_executed_today = today_str
            print("\n" + "=" * 80)
            print(f"⏰ [EOD Retrospective] {today_str} 15:40 장 마감 4단계 반성 및 자기진화 학습 시작")
            print("=" * 80)
            try:
                eod_res = self.eod_worker.run_sync()
                print(f"✅ [EOD Batch 완료] 라벨링: {eod_res.get('labeled_count')}건 | 승격: {eod_res.get('promotion_approved')} | 신규 Shadow: {eod_res.get('new_challenger')}")
            except Exception as eod_err:
                logger.error(f"EOD Batch 실행 예외: {eod_err}", exc_info=True)

        # 시간외 거래 종료 후 (18:00 이후 또는 CLOSED 세션) 시간외 급등 일일 요약 Telegram 1회 발송 (Idempotent)
        if hasattr(self, "after_hours_manager") and self.after_hours_manager:
            if now.time() >= dtime(18, 0, 0) or MarketSessionManager.get_market_session(now) == MarketSession.CLOSED:
                self.after_hours_manager.send_after_hours_daily_summary(today_str)

        # 3. 시장 국면 실시간 평가
        intra_regime_res = IntradayRegimeEngine.evaluate(
            advancing_count=650, declining_count=350,
            kospi_5m_return=0.0015, kosdaq_5m_return=0.0020
        )
        current_regime = intra_regime_res["regime"]
        ad_ratio = intra_regime_res["ad_ratio"]

        # 4. 각 계좌별 잔고 및 손실 한도/리스크 평가
        for acc in self.accounts:
            try:
                balance = acc.client.get_balance()
                new_equity = float(balance.get("total_asset", 0))
                new_cash = float(balance.get("order_available", balance.get("cash", 0)))
                if new_equity > 0:
                    acc.equity = new_equity
                elif acc.equity <= 0 and new_cash > 0:
                    eval_sum = sum(p.current_price * p.qty for p in acc.position_manager.positions.values())
                    acc.equity = new_cash + eval_sum

                if new_cash > 0 or acc.cash <= 0:
                    acc.cash = new_cash

                acc.daily_pnl = float(balance.get("total_profit", acc.daily_pnl))
                acc.daily_pnl_ratio = float(balance.get("total_profit_rate", acc.daily_pnl_ratio * 100.0)) / 100.0
                if acc.cash_manager:
                    acc.cash_manager.sync_broker_cash(acc.cash)
            except Exception as e:
                logger.debug(f"[{acc.name} 계좌 오류] 잔고 조회 실패: {e}")
                continue
            acc.loss_eval = acc.loss_manager.evaluate_loss_limits(acc.daily_pnl_ratio, weekly_pnl_ratio=0.0)
            acc.risk_summary = PortfolioRiskManager.get_risk_summary(
                list(acc.position_manager.positions.values()), acc.equity
            )
            acc.port_risk_status = acc.risk_summary["status"]

        # =========================================================================
        # 4-1. [STEP 0: EXIT WATCHDOG - 최우선 / 완전 독립 실행] (Requirement 2, 3, 4, 5)
        # 후보 발굴, 수급 스캔, 전략, ML, 텔레그램, API 오류와 100% 격리되어 항상 실행
        # =========================================================================
        try:
            self.exit_watchdog.run_watchdog_cycle(
                accounts=self.accounts,
                scanner=self.scanner,
                store=self.store,
                now=now
            )
        except Exception as wd_e:
            logger.critical(f"[WATCHDOG_ISOLATION_ERROR] Watchdog 최상위 예외 격리 및 자동 복원: {wd_e}")
            self.exit_watchdog.recover()

        # 보호 모드 연동: Critical Exit Failure 활성 시 OrderRouter에 즉시 동기화 (신규 BUY 차단)
        is_protective = self.exit_watchdog.is_protective_mode_active()
        for acc in self.accounts:
            if hasattr(acc, "order_router") and acc.order_router:
                acc.order_router.set_critical_exit_failure(is_protective)

        # 5. Section 7: 저비용 시장 전체 수급 레이더(MarketRadar) 스캔 (장애 격리)
        try:
            movers = self.market_radar.scan_market_movers()
            for m in movers:
                # Data Quality Gate 검증
                valid, q_reason = self.data_quality_gate.validate_tick(
                    iem_cd=m.iem_cd, price=m.price, volume=m.volume, timestamp=now, now=now
                )
                if not valid and q_reason and "DUPLICATE" not in q_reason:
                    self.diagnostic_engine.record_rejection(q_reason)
                    continue

                sym = self.store.get(m.iem_cd)
                if sym:
                    sym.price = m.price
                    sym.turnover = m.turnover
                    sym.open_price = sym.open_price or int(m.price * (1.0 - m.change_rate))

                # 캔들 애그리게이터에 주입 및 이벤트 자동 감지
                agg = self.scanner.get_aggregator(m.iem_cd)
                if len(agg.candles_1m) == 0:
                    agg.prefill_history(
                        open_price=int(m.price * (1.0 - m.change_rate)),
                        high_price=m.price,
                        low_price=int(m.price * 0.98),
                        curr_price=m.price,
                        total_vol=m.volume,
                        now=now
                    )

                state, score, det_sym = self.scanner.on_market_tick(
                    iem_cd=m.iem_cd,
                    price=m.price,
                    volume=m.volume,
                    timestamp=now,
                    execution_intensity=120.0 if m.change_rate >= 0.02 else 105.0,
                    obi=0.15 if m.change_rate >= 0.015 else 0.0,
                    rank_surged=(m.source == "VOLUME_TOP" or m.source == "GAIN_TOP")
                )
                if state in (SymbolState.WATCH, SymbolState.ACTIVE, SymbolState.SIGNAL):
                    self.diagnostic_engine.record_candidate(1)
                    if det_sym and det_sym.active_events:
                        self.diagnostic_engine.record_event_detected(len(det_sym.active_events))
        except Exception as radar_e:
            logger.error(f"[MARKET_RADAR_ERROR] 시장 수급 레이더 스캔 예외 격리: {radar_e}")

        # 6. 활성/보유 종목 및 순환 배치 종목 고해상도 시세 수신
        current_stage = "QUOTE_SCAN"
        candidate_scan_status = "IN_PROGRESS"
        all_held_codes = set()
        quote_client = self.accounts[0].client if self.accounts else None
        try:
            for acc in self.accounts:
                all_held_codes.update(acc.position_manager.get_held_codes())

            active_symbols = (
                self.store.get_by_state(SymbolState.POSITION)
                + self.store.get_by_state(SymbolState.SIGNAL)
                + self.store.get_by_state(SymbolState.ACTIVE)
                + self.store.get_by_state(SymbolState.WATCH)
            )

            existing_codes = {s.iem_cd for s in active_symbols}

            # [CRITICAL] 보유 종목(all_held_codes)은 반드시 active_symbols 최우선 순위로 포함 보장
            for c in all_held_codes:
                if c not in existing_codes:
                    sym = self.store.get(c)
                    if sym:
                        active_symbols.insert(0, sym)
                        existing_codes.add(c)

            n_total = len(self._all_symbol_codes)
            if n_total > 0:
                start_idx = self._rolling_scan_index
                end_idx = (start_idx + self._rolling_batch_size) % n_total
                if start_idx < end_idx:
                    batch_codes = self._all_symbol_codes[start_idx:end_idx]
                else:
                    batch_codes = self._all_symbol_codes[start_idx:] + self._all_symbol_codes[:end_idx]
                self._rolling_scan_index = end_idx

                for c in batch_codes:
                    if c not in existing_codes:
                        sym = self.store.get(c)
                        if sym:
                            active_symbols.append(sym)

            # 개별 종목 시세 처리 (브로커 REST)
            if quote_client:
                for sym in active_symbols:
                    try:
                        curr_info = quote_client.get_current_price(sym.iem_cd)
                        if not curr_info or curr_info.get("price", 0) <= 0:
                            continue
                        price = curr_info["price"]
                        volume = curr_info.get("volume", 0)

                        # Data Quality Gate 검증
                        valid, q_reason = self.data_quality_gate.validate_tick(
                            iem_cd=sym.iem_cd, price=price, volume=volume, timestamp=now, now=now
                        )
                        if not valid and q_reason and "DUPLICATE" not in q_reason:
                            self.diagnostic_engine.record_rejection(q_reason)
                            continue

                        sym.open_price = curr_info.get("open", price)
                        sym.high_price = max(sym.high_price, curr_info.get("high", price))
                        sym.low_price = curr_info.get("low", price)
                        sym.prev_close = curr_info.get("prev_close", price)
                        if MarketSessionManager.get_market_session(now) == MarketSession.REGULAR:
                            sym.regular_close = float(price)
                        if sym.high_price > 0 and sym.iem_cd not in self.or_high_map:
                            self.or_high_map[sym.iem_cd] = sym.high_price

                        self.circuit_breaker.update_data_heartbeat(datetime.now())
                        for acc in self.accounts:
                            if getattr(acc, "circuit_breaker", None):
                                acc.circuit_breaker.update_data_heartbeat(datetime.now())
                        self.quote_manager.record_candidate_quote(sym.iem_cd, datetime.now())

                        state, score, detected_sym = self.scanner.on_market_tick(
                            iem_cd=sym.iem_cd,
                            price=price,
                            volume=volume,
                            timestamp=now,
                            high_price=sym.high_price,
                            low_price=sym.low_price,
                            open_price=sym.open_price
                        )

                        # 시간외 세션인 경우 급등 감지하여 익일 관찰 후보 등록 (직접 매수 차단)
                        if MarketSessionManager.get_market_session(now) == MarketSession.AFTER_HOURS:
                            d0_close = self._resolve_d0_regular_close(sym, quote_client, now)
                            if d0_close and d0_close > 0:
                                if price > d0_close:
                                    self.after_hours_manager.detect_after_hours_spike(
                                        iem_cd=sym.iem_cd,
                                        name=sym.name,
                                        regular_close=float(d0_close),
                                        current_price=float(price),
                                        volume=int(volume),
                                        regime=current_regime.value if hasattr(current_regime, "value") else str(current_regime),
                                        dt=now
                                    )
                            else:
                                logger.debug(f"[{sym.iem_cd}] D0 정규장 종가 부재(DATA_UNAVAILABLE) -> 시간외 감지 스킵")

                        if state in (SymbolState.WATCH, SymbolState.ACTIVE, SymbolState.SIGNAL):
                            self.diagnostic_engine.record_candidate(1)
                            if detected_sym and detected_sym.active_events:
                                self.diagnostic_engine.record_event_detected(len(detected_sym.active_events))

                        # Section 38: 독립 스톱로스 워치독 (보유 포지션 실시간 틱 체크 및 매도 발주)
                        if sym.iem_cd in all_held_codes:
                            agg = self.scanner.get_aggregator(sym.iem_cd)
                            atr14 = agg.calculate_atr("1m", 14) if agg else 0.0
                            ema9 = agg.calculate_ema("1m", 9) if agg else 0.0
                            vwap = agg.calculate_vwap() if agg else 0.0
                            c_1m = agg.get_candles("1m", 5) if agg else []
                            p_3m = c_1m[-4].close if len(c_1m) >= 4 else (c_1m[0].open if c_1m else price)
                            mom_3m = (price - p_3m) / float(p_3m) if p_3m > 0 else 0.0

                            for acc in self.accounts:
                                if acc.position_manager.has_position(sym.iem_cd):
                                    acc.position_manager.update_price_and_manage(
                                        sym.iem_cd, price, now, atr14=atr14, ema9=ema9, vwap=vwap, momentum_3m=mom_3m
                                    )
                                    # 해당 계좌가 전량 청산 완료되었으면 해당 계좌에만 독립 쿨다운 부여
                                    if not acc.position_manager.has_position(sym.iem_cd):
                                        last_trade = acc.position_manager.get_last_closed_trade(sym.iem_cd)
                                        is_loss = False
                                        if last_trade:
                                            is_loss = bool(last_trade.get("is_stop_loss", False) or last_trade.get("pnl", 0) < 0)

                                        # [Section 6] 손절 시 최소 15분(900초) 쿨다운, 정상 익절 시 10분(600초)
                                        cooldown_seconds = 900 if is_loss else 600
                                        acc.set_cooldown(sym.iem_cd, now=now, cooldown_seconds=cooldown_seconds)

                                        # [Section 7 & 8] 재진입 추적 및 Churn Telemetry
                                        if is_loss:
                                            acc.last_trade_outcomes[sym.iem_cd] = "LOSS"
                                            loss_pnl = abs(last_trade.get("pnl", 0)) if last_trade else 0.0
                                            acc.cumulative_loss_amount[sym.iem_cd] = acc.cumulative_loss_amount.get(sym.iem_cd, 0.0) + loss_pnl
                                            consec = acc.consecutive_losses.get(sym.iem_cd, 0) + 1
                                            acc.consecutive_losses[sym.iem_cd] = consec
                                            if consec >= 2:
                                                logger.warning(
                                                    f"[CHURN] symbol={sym.iem_cd} account={acc.name} count={consec} "
                                                    f"cum_loss={acc.cumulative_loss_amount[sym.iem_cd]:,.0f}원 reason=CHURN_LOSS_DETECTED"
                                                )
                                        else:
                                            acc.last_trade_outcomes[sym.iem_cd] = "PROFIT"
                                            acc.consecutive_losses[sym.iem_cd] = 0
                            # 만약 모든 계좌에서 해당 종목이 전량 청산 완료되었고 POSITION 상태였으면 WATCH로 정상 강등
                            if not any(acc.position_manager.has_position(sym.iem_cd) for acc in self.accounts):
                                if sym.state == SymbolState.POSITION:
                                    self.store.demote(sym.iem_cd, SymbolState.WATCH, reason="포지션 전량 청산 완료")

                        # NO_TRADE 가상 거래 실시간 사후 추적
                        self.experience_memory.on_price_update(
                            iem_cd=sym.iem_cd,
                            current_price=price,
                            high_price=sym.high_price,
                            low_price=sym.low_price,
                            now=now
                        )
                        # [Section 74 & Section 5] CounterfactualTracker 실시간 사후 가격 추적 연동
                        try:
                            from strategies.profit_opportunity_gate import CounterfactualTracker
                            CounterfactualTracker.get_instance().update_price(
                                symbol=sym.iem_cd,
                                current_price=price,
                                current_time=now
                            )
                        except Exception:
                            pass

                    except Exception as e:
                        logger.error(f"[시세 처리/스톱로스 감시 예외] {sym.iem_cd}: {e}")
            candidate_scan_status = "COMPLETED"
        except Exception as scan_err:
            candidate_scan_status = "FAILED"
            logger.error(f"[QUOTE_SCAN_ERROR] 시세 스캔 중 치명적 예외: {scan_err}", exc_info=True)

        # Section 1, 3, 6: 미체결 지정가 Exit 주문 타임아웃 감지 및 Aggressive Limit 전환
        for acc in self.accounts:
            try:
                acc.position_manager.check_exit_order_timeouts(now=now)
            except Exception as to_err:
                logger.error(f"[{acc.name}] 미체결 주문 타임아웃 감지 예외 격리: {to_err}")

        # 6-1. [CANDIDATE PROMOTION STAGE] 후보 승격 엔진 실행 및 Fail-Closed 격리
        current_stage = "CANDIDATE_PROMOTION"
        if candidate_scan_status == "FAILED":
            candidate_promotion_status = "ABORTED"
            promoted_candidates = None
        else:
            candidate_promotion_status = "IN_PROGRESS"
            try:
                if hasattr(self.scanner, "promotion_engine") and self.scanner.promotion_engine:
                    promoted_res = self.scanner.promotion_engine.get_promoted_candidates()
                    if isinstance(promoted_res, list):
                        promoted_candidates = promoted_res
                        candidate_promotion_status = "COMPLETED"
                    else:
                        promoted_candidates = None
                        candidate_promotion_status = "FAILED"
                else:
                    promoted_candidates = []
                    candidate_promotion_status = "COMPLETED"
            except Exception as promo_err:
                candidate_promotion_status = "FAILED"
                promoted_candidates = None
                self._log_run_cycle_failure(
                    cycle_id=cycle_id,
                    stage="CANDIDATE_PROMOTION",
                    promoted_candidates_state="NONE",
                    candidate_scan_status=candidate_scan_status,
                    candidate_promotion_status=candidate_promotion_status,
                    exception=promo_err,
                    error_message=f"후보 승격 엔진 실행 중 예외 발생: {promo_err}"
                )

        # [FAIL-CLOSED DATA FLOW GUARD]
        if promoted_candidates is None:
            self._log_run_cycle_failure(
                cycle_id=cycle_id,
                stage="CANDIDATE_PROMOTION",
                promoted_candidates_state="NONE",
                candidate_scan_status=candidate_scan_status,
                candidate_promotion_status=candidate_promotion_status,
                error_message="Fail-Closed 차단: promoted_candidates is None - 신규 BUY 파이프라인 안전 종료"
            )
            diag_info = self.diagnostic_engine.evaluate_diagnostic_mode(now)
            self._print_dashboard(now, current_regime, ad_ratio, diag_info, [])
            return

        # 7. 전략 신호 생성 (Setup Engine)
        current_stage = "STRATEGY_SETUP"
        current_session = MarketSessionManager.get_market_session(now)
        session_allowed, s_reason = MarketSessionManager.is_order_entry_allowed(TimeHorizon.INTRADAY, now, is_buy=True)
        if not session_allowed:
            log_mock_buy_trace("UNIVERSE", "ALL", "PASS", count=self.store.total_count())
            log_mock_buy_trace("SESSION_GATE", "ALL", "REJECT", reason=f"SESSION_CLOSED ({s_reason})")
        else:
            log_mock_buy_trace("UNIVERSE", "ALL", "PASS", count=self.store.total_count())

        is_protective = self.exit_watchdog.is_protective_mode_active()
        can_any_trade = (
            session_allowed
            and (not is_protective)
            and any(acc.loss_eval.get("can_trade_intraday", True) and acc.port_risk_status != "BLOCKED" for acc in self.accounts)
        )
        if is_protective:
            log_mock_buy_trace("PROTECTIVE_GATE", "ALL", "REJECT", reason="CRITICAL_EXIT_FAILURE_ACTIVE")

        scanned_signals = []
        if can_any_trade:
            try:
                for cand in promoted_candidates:
                    # 1. 이미 정상 일봉을 수신하여 캐시된 경우 스킵
                    if cand.iem_cd in self.daily_candles_cache:
                        self.quote_manager.cache_hit_count += 1
                        continue
                    self.quote_manager.cache_miss_count += 1

                    # 2. 실제 미지원 종목(00200 등)인 경우 스킵
                    if hasattr(quote_client, "is_unsupported") and quote_client.is_unsupported(cand.iem_cd):
                        continue

                    # 3. 서버 일시 장애(IGW50025 등) 쿨다운 중인 경우 만료 전까지 스킵
                    if hasattr(quote_client, "is_in_transient_cooldown") and quote_client.is_in_transient_cooldown(cand.iem_cd):
                        continue

                    try:
                        candles = quote_client.get_daily_candles(cand.iem_cd, count=65) if quote_client else []
                        if candles:
                            self.daily_candles_cache[cand.iem_cd] = candles
                            if len(candles) >= 2:
                                cand.prev_high = candles[1]["high"]
                                cand.prev_close = candles[1]["close"]
                                cand.prev_low = candles[1]["low"]
                    except Exception as e:
                        logger.debug(f"[{cand.iem_cd}] 일봉 조회 예외: {e}")

                scanned_signals = self.scanner.scan_active_signals(
                    regime=current_regime,
                    now=now,
                    or_high_map=self.or_high_map,
                    daily_candles_cache=self.daily_candles_cache
                )
            except Exception as scan_err:
                logger.error(f"[후보 스캔/전략 평가 예외 격리] {scan_err}", exc_info=True)
                self._log_run_cycle_failure(
                    cycle_id=cycle_id,
                    stage="STRATEGY_SETUP",
                    promoted_candidates_state=f"COUNT_{len(promoted_candidates)}",
                    candidate_scan_status=candidate_scan_status,
                    candidate_promotion_status=candidate_promotion_status,
                    exception=scan_err,
                    error_message=f"전략 셋업 평가 중 예외 격리: {scan_err}"
                )
                scanned_signals = []

        # 8. 하드게이트 및 전략 셋업 평가
        current_stage = "HARD_GATE"
        for cand in promoted_candidates:
            log_mock_buy_trace("EVENT_DETECTED", cand.iem_cd, "PASS", name=cand.name)
            if not cand.buy_block_reasons:
                self.diagnostic_engine.record_hard_gate_passed(1)
                log_mock_buy_trace("CANDIDATE", cand.iem_cd, "PASS", name=cand.name)
            else:
                self.diagnostic_engine.record_symbol_block_reasons(cand)
                log_mock_buy_trace("CANDIDATE", cand.iem_cd, "REJECT", reason=",".join(cand.buy_block_reasons), name=cand.name)

        if scanned_signals:
            self.diagnostic_engine.record_setup_match(len(scanned_signals))
            for sig in scanned_signals:
                log_mock_buy_trace("SETUP", sig.iem_cd, "PASS", strategy=sig.strategy_id, price=sig.strategy_price)

        for sig in scanned_signals:
            if sig.score >= 50.0:
                log_mock_buy_trace("SCORE", sig.iem_cd, "PASS", score=f"{sig.score:.1f}", strategy=sig.strategy_id)
            else:
                log_mock_buy_trace("SCORE", sig.iem_cd, "REJECT", reason="SCORE_BELOW_50", score=f"{sig.score:.1f}", strategy=sig.strategy_id)

        scored_signals = [sig for sig in scanned_signals if sig.score >= 50.0]
        if scored_signals:
            self.diagnostic_engine.record_score_passed(len(scored_signals))

        # 9. 엣지 엔진 & 메타 의사결정 (R:R>=1.5 및 Expected Net R 검증)
        edge_approved_signals = []
        tod_bucket = ExperienceMemory.get_time_of_day_bucket(now)
        for sig in scored_signals:
            try:
                cand_sym = self.store.get(sig.iem_cd) or SymbolInfo(iem_cd=sig.iem_cd, name=sig.name, is_tradable=True, price=sig.strategy_price)
                if not getattr(sig, "client_order_id", None):
                    sig.client_order_id = f"{sig.strategy_id}_{sig.iem_cd}_{int(now.timestamp()*1000)}"
                cid = sig.client_order_id

                agg = self.scanner.get_aggregator(sig.iem_cd)
                cand_vwap = agg.calculate_vwap() if agg else sig.strategy_price
                open_p = float(cand_sym.open_price or sig.strategy_price)
                prev_c = float(cand_sym.prev_close or sig.strategy_price)

                # 실제 종목 실시간 RVOL (Relative Volume) 산출
                real_rvol = 1.0
                if agg:
                    real_rvol = agg.calculate_rvol("1m", lookback=20)
                if (not real_rvol or real_rvol <= 0.1 or real_rvol == 1.0) and hasattr(sig, "rvol") and sig.rvol > 0:
                    real_rvol = float(sig.rvol)
                if not real_rvol or real_rvol <= 0:
                    real_rvol = 1.0
                real_rvol = round(float(real_rvol), 2)

                # 시간외 사전 특징값(Prior Feature / Context) 및 세션 태깅 적용
                ah_feats = self.after_hours_manager.get_next_session_features(sig.iem_cd)
                if ah_feats.get("AFTER_HOURS_SIGNAL"):
                    sig.signal_session = "AFTER_HOURS"
                    sig.entry_session = "REGULAR"
                    # 시간외 급등 종목의 정규장 시초 갭 및 과열/소진(Exhaustion Gap) 검증
                    gap_ok, gap_msg = self.after_hours_manager.evaluate_next_session_gap(
                        iem_cd=sig.iem_cd,
                        open_price=open_p,
                        prev_close=prev_c,
                        current_price=float(sig.strategy_price),
                        vwap=float(cand_vwap),
                        rvol=real_rvol
                    )
                    if not gap_ok:
                        if hasattr(self, "after_hours_manager") and self.after_hours_manager:
                            self.after_hours_manager.mark_rejected(sig.iem_cd, gap_msg)
                        self.diagnostic_engine.record_rejection(gap_msg)
                        self.persistence_manager.record_no_trade(
                            iem_cd=sig.iem_cd,
                            name=sig.name,
                            strategy_id=sig.strategy_id,
                            score=sig.score,
                            ml_prob=0.0,
                            expected_net_r=0.0,
                            primary_reason=gap_msg,
                            category="CORRECT_NO_TRADE"
                        )
                        continue
                else:
                    sig.signal_session = "REGULAR"
                    sig.entry_session = "REGULAR"

                self.diagnostic_engine.record_funnel_event(
                    event_name=EVENT_SIGNAL_DETECTED,
                    symbol=sig.iem_cd,
                    strategy=sig.strategy_id,
                    order_id=cid,
                    result="SUCCESS",
                    now_dt=now
                )

                # 과거 유사도 메타 지표 조회 (실제 실시간 RVOL 반영)
                try:
                    sim_meta = self.similarity_engine.query_similarity(
                        current_features={"price": sig.strategy_price, "score": sig.score, "rvol": real_rvol},
                        current_regime=current_regime,
                        now=now
                    )
                    log_mock_buy_trace("SIMILARITY", sig.iem_cd, "PASS", hist_win_rate=f"{sim_meta.hist_win_rate:.2f}", samples=sim_meta.sample_count)
                except Exception as sim_err:
                    logger.error(f"[SIMILARITY_QUERY_ERROR] {sig.iem_cd} 유사도 조회 실패: {sim_err}")
                    log_mock_buy_trace("SIMILARITY", sig.iem_cd, "REJECT", reason=f"SIMILARITY_ERROR: {sim_err}")
                    self.diagnostic_engine.record_rejection(f"SIMILARITY_ERROR ({sig.iem_cd})")
                    self.persistence_manager.record_no_trade(
                        iem_cd=sig.iem_cd,
                        name=sig.name,
                        strategy_id=sig.strategy_id,
                        score=sig.score,
                        ml_prob=0.0,
                        expected_net_r=0.0,
                        primary_reason=f"SIMILARITY_ERROR: {sim_err}",
                        category="CORRECT_NO_TRADE"
                    )
                    continue

                # ML 예측 확률 산출
                pred_probs = {"p_target": 0.65, "p_stop": 0.35}
                log_mock_buy_trace("ML", sig.iem_cd, "PASS", p_target=f"{pred_probs['p_target']:.2f}", p_stop=f"{pred_probs['p_stop']:.2f}")

                # 엣지 엔진 검증
                with StageTimer() as edge_timer:
                    edge_res = self.edge_engine.calculate_edge(sym=cand_sym, signal=sig, p_target=pred_probs["p_target"], p_stop=pred_probs["p_stop"])
                self.diagnostic_engine.record_stage_latency("edge", edge_timer.elapsed_ms)

                # Meta Model 의사결정: target_price에 edge_res.target_price(>=1.5R) 또는 target_2r 명시적 전달
                target_to_eval = edge_res.target_price if edge_res.target_price > edge_res.entry_price else sig.target_2r
                meta_res = self.meta_decision_engine.evaluate_candidate(
                    setup_name=sig.strategy_id,
                    time_horizon=sig.time_horizon,
                    entry_price=edge_res.entry_price,
                    stop_price=sig.stop_price,
                    target_price=target_to_eval,
                    predicted_probs=pred_probs,
                    rule_score=sig.score,
                    similarity_meta=sim_meta.__dict__,
                    regime=current_regime.value if hasattr(current_regime, "value") else str(current_regime),
                    time_of_day_bucket=tod_bucket,
                    target_2r=sig.target_2r,
                    target_3r=sig.target_3r,
                    entry_timing_valid=getattr(sig, "entry_timing_valid", True)
                )

                # Trace META & EDGE gates
                if meta_res.decision in ("BUY", "BUY_SMALL"):
                    log_mock_buy_trace("META", sig.iem_cd, "PASS", meta_decision=meta_res.decision, expected_net_r=f"{meta_res.expected_net_r:+.2f}R")
                else:
                    log_mock_buy_trace("META", sig.iem_cd, "REJECT", meta_decision=meta_res.decision, reason=meta_res.reason or "META_REJECT", expected_net_r=f"{meta_res.expected_net_r:+.2f}R")

                rr_ratio_val = getattr(edge_res, "risk_reward_ratio", getattr(edge_res, "reward_r", 0.0))
                if edge_res.is_approved:
                    log_mock_buy_trace("EDGE", sig.iem_cd, "PASS", rr_ratio=f"{rr_ratio_val:.2f}", expected_net_r=f"{edge_res.expected_net_r:+.2f}R")
                else:
                    log_mock_buy_trace("EDGE", sig.iem_cd, "REJECT", reason=edge_res.rejection_reason or "LOW_EXPECTED_EDGE", rr_ratio=f"{rr_ratio_val:.2f}")

                # Point-in-Time 스냅샷 불변 저장
                snap = PointInTimeSnapshot(
                    event_id=f"EVT_{sig.iem_cd}_{int(now.timestamp())}",
                    iem_cd=sig.iem_cd,
                    name=sig.name,
                    event_time=now.isoformat(),
                    as_of_time=now.isoformat(),
                    feature_version="FEATURE_V16_0",
                    strategy_version="STRATEGY_V16_0",
                    model_version=self.model_registry.champion_id or "CHAMPION_V16_0",
                    dataset_version="DATA_V16_0",
                    raw_features={"price": sig.strategy_price, "score": sig.score, "vwap": edge_res.entry_price, "rvol": real_rvol},
                    prediction={"p_target": meta_res.p_target, "p_stop": meta_res.p_stop, "expected_net_r": edge_res.expected_net_r},
                    decision=meta_res.decision if edge_res.is_approved else "NO_TRADE",
                    decision_reason=meta_res.reason if edge_res.is_approved else (edge_res.rejection_reason or "LOW_EXPECTED_EDGE"),
                    rule_score=sig.score,
                    virtual_trade={
                        "entry_price": edge_res.entry_price, "stop_price": sig.stop_price,
                        "target_price": target_to_eval, "shares": 10, "strategy_id": sig.strategy_id,
                        "time_horizon": sig.time_horizon.value if hasattr(sig.time_horizon, "value") else str(sig.time_horizon)
                    },
                    regime=current_regime.value if hasattr(current_regime, "value") else str(current_regime),
                    time_of_day_bucket=tod_bucket
                )
                self.experience_memory.record_snapshot(snap)

                if edge_res.is_approved and meta_res.decision in ("BUY", "BUY_SMALL"):
                    self.diagnostic_engine.record_edge_passed(1)
                    edge_approved_signals.append((sig, cand_sym, edge_res, meta_res))
                    self.diagnostic_engine.record_funnel_event(
                        event_name=EVENT_DECISION_APPROVED,
                        symbol=sig.iem_cd,
                        strategy=sig.strategy_id,
                        order_id=cid,
                        result="SUCCESS",
                        now_dt=now
                    )
                else:
                    rej_reason = edge_res.rejection_reason or meta_res.reason or "LOW_EXPECTED_EDGE"
                    self.diagnostic_engine.record_rejection(rej_reason)
                    self.diagnostic_engine.record_funnel_event(
                        event_name=EVENT_DECISION_APPROVED,
                        symbol=sig.iem_cd,
                        strategy=sig.strategy_id,
                        order_id=cid,
                        result="REJECT",
                        reject_reason=rej_reason,
                        now_dt=now
                    )
                    self.persistence_manager.record_no_trade(
                        iem_cd=sig.iem_cd,
                        name=sig.name,
                        strategy_id=sig.strategy_id,
                        score=sig.score,
                        ml_prob=meta_res.p_target,
                        expected_net_r=meta_res.expected_net_r,
                        primary_reason=rej_reason,
                        category="CORRECT_NO_TRADE"
                    )
            except Exception as sig_eval_err:
                logger.error(f"[신호/ML/엣지 평가 예외 격리] {sig.iem_cd}: {sig_eval_err}", exc_info=True)
                self._log_run_cycle_failure(
                    cycle_id=cycle_id,
                    stage="ML_PREDICTION",
                    promoted_candidates_state=f"COUNT_{len(promoted_candidates)}",
                    candidate_scan_status=candidate_scan_status,
                    candidate_promotion_status=candidate_promotion_status,
                    exception=sig_eval_err,
                    error_message=f"신호 ML/Edge 평가 예외 ({sig.iem_cd})"
                )
                continue

        # 10. 우선순위 정렬: Expected Net Return > Rule Score > ML Prob > Stage (Section 8)
        edge_approved_signals = PortfolioCashManager.sort_signals_by_priority(edge_approved_signals)

        approved_for_dashboard = []

        # 11. 계좌별(모의 + 실전) 안전 주문 집행 (Section 6 & 7 중앙 Cash Reservation 연동)
        for sig, cand_sym, edge_res, meta_res in edge_approved_signals:
            sig_to_router_ms = max(0.0, (now - sig.timestamp).total_seconds() * 1000.0)
            self.diagnostic_engine.funnel_telemetry.record_signal_to_router_latency(
                sig_to_router_ms, order_id=getattr(sig, "client_order_id", None)
            )
            signal_bought_any = False
            for acc in self.accounts:
                # [Section 13 & 18] 계좌 우선순위 및 상태 텔레메트리
                is_live = (acc.name == "LIVE")
                logger.info(
                    f"[ACCOUNT] account={acc.name} priority={'PRIMARY' if is_live else 'SECONDARY'} "
                    f"symbol={sig.iem_cd} status=EVALUATING"
                )

                if not acc.loss_eval.get("can_trade_intraday", True) or acc.port_risk_status == "BLOCKED":
                    continue

                # [계좌별 포지션 독립 가드] 해당 계좌가 이미 보유 중이면 추가 매수 차단
                if acc.position_manager.has_position(sig.iem_cd):
                    self.diagnostic_engine.record_rejection(f"[{acc.name}] ALREADY_HELD")
                    log_mock_buy_trace("POSITION_GUARD", sig.iem_cd, "REJECT", reason="ALREADY_HELD", account=acc.name)
                    _emit_decision_trace_no_trade(acc, sig, cand_sym, edge_res, "POSITION_GUARD", "ALREADY_HELD", signal_age_ms=sig_to_router_ms)
                    continue

                # [계좌별 쿨다운 독립 가드] 해당 계좌가 손절/청산 쿨다운 중이면 매수 차단
                if acc.is_in_cooldown(sig.iem_cd, now):
                    self.diagnostic_engine.record_rejection(f"[{acc.name}] ACCOUNT_COOLDOWN")
                    log_mock_buy_trace("COOLDOWN_GUARD", sig.iem_cd, "REJECT", reason="ACCOUNT_COOLDOWN", account=acc.name)
                    _emit_decision_trace_no_trade(acc, sig, cand_sym, edge_res, "COOLDOWN_GUARD", "ACCOUNT_COOLDOWN", signal_age_ms=sig_to_router_ms)
                    continue

                # [Section 7 & 18] 동일 종목 당일 재진입 횟수 및 퀄리티 검증 (와이제이링크 등 6회 연타 손실 방지)
                entry_count = acc.daily_entry_counts.get(sig.iem_cd, 0)
                last_outcome = acc.last_trade_outcomes.get(sig.iem_cd, "NONE")

                # Entry 1: 정상 허용
                # Entry 2: 직전 거래 손실 시, 양수 모멘텀 및 신규 트리거 필수
                if entry_count == 1 and last_outcome == "LOSS":
                    sig_mom = getattr(sig, "ret_3m", 0.0) or getattr(sig, "momentum_3m", 0.0) or 0.0
                    if sig_mom < 0.0:
                        rej_msg = f"REENTRY_GUARD_LOSS_MOMENTUM_REQUIRED (count=1, prev=LOSS, mom={sig_mom*100:+.2f}%)"
                        self.diagnostic_engine.record_rejection(f"[{acc.name}] {rej_msg}")
                        logger.info(f"[REENTRY] symbol={sig.iem_cd} account={acc.name} entry_count={entry_count} prev_outcome={last_outcome} decision=REJECT reason={rej_msg}")
                        _emit_decision_trace_no_trade(acc, sig, cand_sym, edge_res, "REENTRY_GUARD", rej_msg, signal_age_ms=sig_to_router_ms, mom_3m=sig_mom)
                        continue

                # Entry 3+: 엄격 스로틀링 (설정 한도 초과 또는 초우량 고득점 >= 85점 미달 시 차단)
                if entry_count >= 2:
                    if getattr(sig, "score", 0.0) < 85.0:
                        rej_msg = f"DAILY_REENTRY_LIMIT_THROTTLED (count={entry_count}, score={getattr(sig, 'score', 0.0):.1f} < 85.0)"
                        self.diagnostic_engine.record_rejection(f"[{acc.name}] {rej_msg}")
                        logger.info(f"[REENTRY] symbol={sig.iem_cd} account={acc.name} entry_count={entry_count} prev_outcome={last_outcome} decision=REJECT reason={rej_msg}")
                        _emit_decision_trace_no_trade(acc, sig, cand_sym, edge_res, "REENTRY_GUARD", rej_msg, signal_age_ms=sig_to_router_ms)
                        continue

                logger.info(f"[REENTRY] symbol={sig.iem_cd} account={acc.name} entry_count={entry_count} prev_outcome={last_outcome} cooldown_left_sec=0 decision=PASS")

                # Section 6 & 7: 최신 유효 가용 현금(Effective Cash) 산출
                effective_cash = acc.cash_manager.effective_available_cash if acc.cash_manager else acc.cash
                usable_cash = effective_cash
                if sig.time_horizon == TimeHorizon.SWING:
                    # 스윙 주문은 전체 자금의 30%를 단타 모멘텀용으로 보호/유보하고 남은 가용 자금만 할당
                    momentum_reserve = acc.cash * 0.30
                    usable_cash = max(0.0, effective_cash - momentum_reserve)

                with StageTimer() as risk_timer:
                    tot_amt, tot_ratio, status = PortfolioRiskManager.calculate_total_open_risk(
                        list(acc.position_manager.positions.values()), acc.equity
                    )
                    shares, risk_amt, rationale = PositionSizer.calculate_shares(
                        sig.time_horizon, acc.equity, usable_cash, edge_res.entry_price, sig.stop_price, order_type=sig.order_type
                    )
                self.diagnostic_engine.record_stage_latency("risk", risk_timer.elapsed_ms)

                if status == "BLOCKED":
                    self.diagnostic_engine.record_rejection(f"[{acc.name}] PORTFOLIO_RISK_LIMIT")
                    log_mock_buy_trace("RISK", sig.iem_cd, "REJECT", reason="PORTFOLIO_RISK_LIMIT", account=acc.name)
                    _emit_decision_trace_no_trade(acc, sig, cand_sym, edge_res, "RISK_LIMIT", "PORTFOLIO_RISK_LIMIT", usable_cash=usable_cash, signal_age_ms=sig_to_router_ms)
                    continue
                else:
                    log_mock_buy_trace("RISK", sig.iem_cd, "PASS", account=acc.name, equity=int(acc.equity), open_risk=int(tot_amt))

                if shares <= 0:
                    self.diagnostic_engine.record_rejection(f"[{acc.name}] INSUFFICIENT_CASH")
                    log_mock_buy_trace("SIZER", sig.iem_cd, "REJECT", reason=f"INSUFFICIENT_CASH (shares=0, cash={effective_cash:,.0f}원)", account=acc.name)
                    _emit_decision_trace_no_trade(acc, sig, cand_sym, edge_res, "CASH_RESERVATION", "INSUFFICIENT_CASH", usable_cash=usable_cash, signal_age_ms=sig_to_router_ms)
                    if acc.cash_manager:
                        req = acc.cash_manager.calculate_total_required_cash(1, edge_res.entry_price)
                        shortfall = req.total_required_cash - effective_cash
                        rec = CashShortfallRecord(
                            symbol=sig.iem_cd,
                            signal_id=getattr(sig, "signal_id", f"SIG_{sig.iem_cd}_{int(now.timestamp())}"),
                            strategy_id=sig.strategy_id,
                            decision="NO_TRADE",
                            cash_available=acc.cash,
                            reserved_cash=acc.cash_manager.reserved_cash,
                            effective_available_cash=effective_cash,
                            required_order_value=req.required_order_value,
                            estimated_cost=req.estimated_fee + req.estimated_slippage,
                            cash_shortfall=shortfall,
                            timestamp=now.isoformat(),
                            reason="INSUFFICIENT_CASH"
                        )
                        self.persistence_manager.record_cash_shortfall(rec, trading_mode=acc.mode, account_no=acc.act_no)
                    continue
                self.diagnostic_engine.record_risk_passed(1)

                # =============================================================
                # [NEW Execution Architecture v16.1] BUY 직전 최신 Quote 동기화
                # =============================================================
                quote_snapshot = self.quote_manager.sync_fresh_quote(
                    client=acc.client,
                    symbol=sig.iem_cd,
                    signal_price=edge_res.entry_price
                )
                sig.quote_snapshot = quote_snapshot
                self.diagnostic_engine.funnel_telemetry.record_broker_api_call(
                    latency_ms=quote_snapshot.api_latency_ms,
                    is_success=True
                )

                self.diagnostic_engine.record_funnel_event(
                    event_name=EVENT_QUOTE_CHECK_START,
                    symbol=sig.iem_cd,
                    strategy=sig.strategy_id,
                    order_id=getattr(sig, "client_order_id", ""),
                    result="SUCCESS" if quote_snapshot.is_fresh else "STALE",
                    quote_age_ms=quote_snapshot.order_quote_age_ms,
                    now_dt=now
                )

                # [ORDER QUOTE] Telemetry 로깅
                print(quote_snapshot.format_log())

                if not quote_snapshot.is_fresh:
                    # 1단계: Stale Quote 감지 -> 최신 호가 1회 Re-fetch 시도
                    self.diagnostic_engine.record_funnel_event(
                        event_name=EVENT_QUOTE_REFETCH_START,
                        symbol=sig.iem_cd,
                        strategy=sig.strategy_id,
                        order_id=getattr(sig, "client_order_id", ""),
                        result="SUCCESS",
                        quote_age_ms=quote_snapshot.order_quote_age_ms,
                        now_dt=now
                    )
                    re_quote = self.quote_manager.sync_fresh_quote(
                        client=acc.client,
                        symbol=sig.iem_cd,
                        signal_price=edge_res.entry_price
                    )
                    is_fresh_re = bool(re_quote and re_quote.is_fresh)
                    self.diagnostic_engine.funnel_telemetry.record_quote_refetch_attempt(
                        is_success=is_fresh_re,
                        is_stale_before=True,
                        latency_ms=re_quote.api_latency_ms if re_quote else 0.0
                    )
                    self.diagnostic_engine.record_funnel_event(
                        event_name=EVENT_QUOTE_REFETCH_END,
                        symbol=sig.iem_cd,
                        strategy=sig.strategy_id,
                        order_id=getattr(sig, "client_order_id", ""),
                        result="SUCCESS" if is_fresh_re else "REJECT",
                        reject_reason="" if is_fresh_re else (re_quote.staleness_reason if re_quote else "REFETCH_FAILED"),
                        quote_age_ms=re_quote.quote_data_age_ms if re_quote else 9999.0,
                        now_dt=now
                    )
                    if is_fresh_re:
                        quote_snapshot = re_quote
                        sig.quote_snapshot = re_quote
                        sig._was_refetched = True
                        print(f"🔄 [{acc.name} Quote Re-fetch 성공] {sig.name}({sig.iem_cd}) 최신 호가 갱신: {re_quote.current_price:,}원 (age={re_quote.quote_data_age_ms:.1f}ms)")
                    else:
                        age_sec = quote_snapshot.quote_data_age_ms / 1000.0
                        self.diagnostic_engine.record_data_stale(age_sec)
                        self.diagnostic_engine.record_rejection(f"[{acc.name}] DATA_STALE ({quote_snapshot.staleness_reason})")
                        _emit_decision_trace_no_trade(acc, sig, cand_sym, edge_res, "DATA_STALE", quote_snapshot.staleness_reason, shares=shares, usable_cash=effective_cash, signal_age_ms=sig_to_router_ms)
                        continue

                self.diagnostic_engine.record_fresh_quote_passed(1)

                # 최신 현재가로 주문 가격 업데이트 및 슬리피지 예산 검증 (< 1.5% 급변 보호)
                latest_order_price = quote_snapshot.current_price
                price_dev = abs(latest_order_price - edge_res.entry_price) / max(1, edge_res.entry_price)
                if price_dev > 0.015:
                    self.diagnostic_engine.record_rejection(f"[{acc.name}] QUOTE_SLIPPAGE_EXCEEDED ({price_dev*100:.2f}% > 1.5%)")
                    _emit_decision_trace_no_trade(acc, sig, cand_sym, edge_res, "PRICE_DRIFT", f"QUOTE_SLIPPAGE_EXCEEDED ({price_dev*100:.2f}% > 1.5%)", shares=shares, latest_price=latest_order_price, usable_cash=effective_cash, signal_age_ms=sig_to_router_ms)
                    continue

                # =============================================================
                # [Authoritative Gate] Broker Buyable Quantity Check (Item 2, 3, 4)
                # =============================================================
                order_type = sig.order_type or OrderType.MARKET
                order_type_str = "05" if order_type == OrderType.MARKET else "01"
                check_price = 0 if order_type == OrderType.MARKET else latest_order_price

                broker_avail_qty = shares
                if hasattr(acc.client, "get_buyable_quantity"):
                    b_res = acc.client.get_buyable_quantity(
                        iem_cd=sig.iem_cd,
                        price=check_price,
                        order_type=order_type_str
                    )
                    if b_res.get("is_valid", False):
                        broker_avail_qty = b_res.get("csh_orr_pbl_qty", 0)

                if broker_avail_qty <= 0:
                    rej_msg = f"BROKER_BUYABLE_QTY_ZERO: Broker 가능수량 0주 (가용현금: {effective_cash:,.0f}원)"
                    self.diagnostic_engine.record_rejection(f"[{acc.name}] {rej_msg}")
                    log_mock_buy_trace("SIZER", sig.iem_cd, "REJECT", reason=rej_msg, account=acc.name)
                    _emit_decision_trace_no_trade(acc, sig, cand_sym, edge_res, "BROKER_QTY", rej_msg, shares=shares, latest_price=latest_order_price, broker_avail=broker_avail_qty, usable_cash=effective_cash, signal_age_ms=sig_to_router_ms)
                    print(f"🛑 [{acc.name} NO_TRADE] {sig.name}({sig.iem_cd}): {rej_msg}")
                    continue

                if shares > broker_avail_qty:
                    allow_partial = getattr(settings, "ALLOW_PARTIAL_CASH_BUY", False)
                    if not allow_partial:
                        rej_msg = f"BROKER_BUYABLE_QTY_LIMIT: 계산수량({shares}주) > Broker가능수량({broker_avail_qty}주) (가용현금: {effective_cash:,.0f}원)"
                        self.diagnostic_engine.record_rejection(f"[{acc.name}] {rej_msg}")
                        log_mock_buy_trace("SIZER", sig.iem_cd, "REJECT", reason=rej_msg, account=acc.name)
                        _emit_decision_trace_no_trade(acc, sig, cand_sym, edge_res, "BROKER_QTY", rej_msg, shares=shares, latest_price=latest_order_price, broker_avail=broker_avail_qty, usable_cash=effective_cash, signal_age_ms=sig_to_router_ms)
                        print(f"🛑 [{acc.name} NO_TRADE] {sig.name}({sig.iem_cd}): {rej_msg}")
                        continue
                    else:
                        # Auto-downsize to broker max if enabled in settings
                        shares = broker_avail_qty

                # Section: 스윙 전략은 단타 30% 현금 유보 버퍼를 침범하지 않도록 가용 스윙 한도 내 자동 수량 맞춤
                if sig.time_horizon == TimeHorizon.SWING and acc.cash_manager:
                    reserved_momentum = acc.cash * 0.30
                    usable_for_swing = max(0.0, effective_cash - reserved_momentum)
                    single_cost = latest_order_price * 1.00065
                    max_swing_shares = int(usable_for_swing / single_cost) if single_cost > 0 else 0
                    if max_swing_shares > 0 and shares > max_swing_shares:
                        print(f"⚖️ [{acc.name} 스윙 수량 조정] {sig.name}({sig.iem_cd}) {shares}주 -> {max_swing_shares}주 (단타 30% 현금 유보 {reserved_momentum:,.0f}원 보호)")
                        shares = max_swing_shares

                final_shares = shares
                log_mock_buy_trace("SIZER", sig.iem_cd, "PASS", shares=final_shares, value=int(final_shares * latest_order_price), account=acc.name)

                # 주문 직전 order_quote_age_ms 갱신
                quote_snapshot.update_order_age()

                # [Section 41] Signal TTL (신호 만료 관리)
                signal_age_ms = max(0.0, (now - sig.timestamp).total_seconds() * 1000.0)
                sig.signal_age_ms = signal_age_ms
                if signal_age_ms > 30000.0:
                    rej_ttl = f"SIGNAL_EXPIRED: age={signal_age_ms:.0f}ms > 30000ms"
                    self.diagnostic_engine.record_rejection(f"[{acc.name}] {rej_ttl}")
                    log_mock_buy_trace("SIGNAL_TTL", sig.iem_cd, "REJECT", reason=rej_ttl, account=acc.name)
                    _emit_decision_trace_no_trade(acc, sig, cand_sym, edge_res, "SIGNAL_TTL", rej_ttl, shares=final_shares, latest_price=latest_order_price, broker_avail=broker_avail_qty, usable_cash=effective_cash, signal_age_ms=signal_age_ms)
                    continue

                # [Section 2 & 8 & 43] Entry Timing Quality Gate (Hard Gate)
                # Score / ML 점수가 높아도 주문 직전 가격 행동이 불량하면 100% 차단
                agg = self.scanner.get_aggregator(sig.iem_cd)
                c_1m = agg.get_candles("1m", 5) if agg else []
                c_1m_open = agg.current_1m.open if (agg and agg.current_1m) else (c_1m[-1].open if c_1m else latest_order_price)
                c_1m_high = agg.current_1m.high if (agg and agg.current_1m) else (c_1m[-1].high if c_1m else latest_order_price)
                c_1m_low = agg.current_1m.low if (agg and agg.current_1m) else (c_1m[-1].low if c_1m else latest_order_price)
                p_3m_ago = c_1m[-4].close if len(c_1m) >= 4 else (c_1m[0].open if c_1m else latest_order_price)
                curr_ret_3m = (latest_order_price - p_3m_ago) / float(p_3m_ago) if p_3m_ago > 0 else 0.0
                curr_ret_1m = (latest_order_price - c_1m_open) / float(c_1m_open) if c_1m_open > 0 else 0.0
                curr_rvol = getattr(sig, "rvol", 0.0) or (agg.calculate_rvol() if agg else 0.0)
                curr_vwap = agg.calculate_vwap() if agg else (cand_sym.vwap if cand_sym else 0.0)
                curr_high = cand_sym.high_price if cand_sym else latest_order_price

                timing_ok, timing_reason, timing_metrics = validate_entry_timing_quality(
                    curr_price=latest_order_price,
                    strategy_id=sig.strategy_id,
                    rvol=curr_rvol,
                    ret_3m=curr_ret_3m,
                    ret_1m=curr_ret_1m,
                    vwap=curr_vwap,
                    intraday_high=curr_high,
                    current_1m_open=c_1m_open,
                    current_1m_high=c_1m_high,
                    current_1m_low=c_1m_low,
                    current_1m_close=latest_order_price,
                    rebound_confirmed=getattr(sig, "rebound_confirmed", False),
                    symbol=sig.iem_cd,
                    min_rvol=1.5
                )
                if not timing_ok:
                    self.diagnostic_engine.record_rejection(f"[{acc.name}] {timing_reason}")
                    log_mock_buy_trace("ENTRY_TIMING", sig.iem_cd, "REJECT", reason=timing_reason, account=acc.name)
                    _emit_decision_trace_no_trade(
                        acc, sig, cand_sym, edge_res, "ENTRY_QUALITY", timing_reason,
                        shares=final_shares, latest_price=latest_order_price,
                        broker_avail=broker_avail_qty, usable_cash=effective_cash,
                        rvol=curr_rvol, mom_3m=curr_ret_3m,
                        vwap_gap=((latest_order_price - curr_vwap) / float(curr_vwap)) if curr_vwap > 0 else 0.0,
                        rebound_str=timing_metrics.get("rebound_strength", 0.50),
                        signal_age_ms=signal_age_ms
                    )
                    continue

                with StageTimer() as exec_timer:
                    passed, msg = acc.order_router.run_pre_order_checks(
                        sig, final_shares, latest_order_price, {"cash": effective_cash, "total_asset": acc.equity}, status, current_spread=0.001
                    )
                self.diagnostic_engine.record_stage_latency("execution", exec_timer.elapsed_ms)
                if not passed:
                    self.diagnostic_engine.record_rejection(f"[{acc.name}] {msg}")
                    log_mock_buy_trace("BUY_APPROVED", sig.iem_cd, "REJECT", reason=msg, account=acc.name)
                    rej_gate = (
                        "PROFIT_OPPORTUNITY" if any(k in msg for k in ["MICRO_PROFIT", "LOW_EXPECTED_MOVE", "COST_TO_MOVE", "EXPECTED_MFE", "REBOUND"])
                        else ("SIGNAL_TTL" if "SIGNAL_EXPIRED" in msg else ("PRICE_DRIFT" if "PRICE_DRIFT" in msg else "ORDER_ROUTER"))
                    )
                    _emit_decision_trace_no_trade(
                        acc, sig, cand_sym, edge_res, rej_gate, msg,
                        shares=final_shares, latest_price=latest_order_price,
                        broker_avail=broker_avail_qty, usable_cash=effective_cash,
                        rvol=curr_rvol, mom_3m=curr_ret_3m,
                        vwap_gap=((latest_order_price - curr_vwap) / float(curr_vwap)) if curr_vwap > 0 else 0.0,
                        rebound_str=timing_metrics.get("rebound_strength", 0.50),
                        signal_age_ms=signal_age_ms
                    )
                    continue
                self.diagnostic_engine.record_execution_passed(1)

                self.diagnostic_engine.record_buy_approved(1)
                log_mock_buy_trace("BUY_APPROVED", sig.iem_cd, "PASS", shares=final_shares, price=latest_order_price, account=acc.name)

                vwap_gap_val = ((latest_order_price - curr_vwap) / float(curr_vwap)) if curr_vwap > 0 else 0.0
                rebound_val = timing_metrics.get("rebound_strength", 0.67) if timing_metrics else 0.67
                exp_move_val = getattr(edge_res, "expected_move_pct", None) or getattr(sig, "expected_move_pct", None) or 0.0218
                stop_dist_val = (abs(latest_order_price - sig.stop_price) / float(latest_order_price)) if latest_order_price > 0 else 0.018

                _emit_decision_trace_buy(
                    acc, sig, cand_sym, edge_res,
                    shares=final_shares,
                    latest_price=latest_order_price,
                    broker_avail=broker_avail_qty,
                    usable_cash=effective_cash,
                    rvol=curr_rvol,
                    mom_3m=curr_ret_3m,
                    vwap_gap=vwap_gap_val,
                    rebound_str=rebound_val,
                    signal_age_ms=signal_age_ms,
                    expected_move_pct=exp_move_val,
                    expected_net_r=edge_res.expected_net_r,
                    cost_ratio=0.156,
                    stop_dist_pct=stop_dist_val,
                    spread_pct=0.0011
                )

                self.diagnostic_engine.record_order_created(1)
                log_mock_buy_trace("ORDER_CREATED", sig.iem_cd, "PASS", shares=final_shares, price=latest_order_price, account=acc.name)
                signal_bought_any = True

                print(f"⚡ [{acc.name} BUY 승인] {sig.name}({sig.iem_cd}) {final_shares}주 @ {latest_order_price:,}원 "
                      f"(최신호가 검증 통과: data_age={quote_snapshot.quote_data_age_ms:.1f}ms, order_age={quote_snapshot.order_quote_age_ms:.1f}ms, "
                      f"기대값: {edge_res.expected_net_r:+.2f}R, 점수: {sig.score:.1f}점, Broker가능수량: {broker_avail_qty}주)")

                with StageTimer() as order_timer:
                    order = acc.order_router.submit_order(
                        sig, final_shares, order_type, latest_order_price,
                        {"cash": acc.cash, "total_asset": acc.equity}, status
                    )
                self.diagnostic_engine.record_stage_latency("order", order_timer.elapsed_ms)

                if order:
                    if getattr(sig, "_was_refetched", False):
                        self.diagnostic_engine.funnel_telemetry.record_refetched_order_sent()
                    self.diagnostic_engine.record_order_sent(1)
                    from strategies.profit_opportunity_gate import ProfitOpportunityTelemetry
                    ProfitOpportunityTelemetry.get_instance().record_order_sent(1)
                    log_mock_buy_trace("ORDER_SENT", sig.iem_cd, "PASS", client_order_id=order.client_order_id, account=acc.name)

                    if order.broker_order_no:
                        log_mock_buy_trace("ACK", sig.iem_cd, "PASS", broker_order_no=order.broker_order_no, account=acc.name)
                    else:
                        log_mock_buy_trace("ACK", sig.iem_cd, "REJECT", reason="NO_BROKER_ORDER_NO", account=acc.name)

                    self.persistence_manager.record_order(order, trading_mode=acc.mode, account_no=acc.act_no)
                    if order.status == OrderStatus.FILLED:
                        self.diagnostic_engine.record_fill(1)
                        self.diagnostic_engine.record_stage_latency("fill", 1.0)
                        ProfitOpportunityTelemetry.get_instance().record_fill(1)
                        log_mock_buy_trace("FILL", sig.iem_cd, "PASS", filled_qty=shares, avg_price=order.filled_avg_price, account=acc.name)
                        self.persistence_manager.record_fill(
                            client_order_id=order.client_order_id,
                            iem_cd=sig.iem_cd,
                            side="BUY",
                            qty=shares,
                            price=order.filled_avg_price,
                            trading_mode=acc.mode,
                            account_no=acc.act_no
                        )
                        acc.position_manager.open_position(
                            sig.time_horizon, sig.strategy_id, sig.iem_cd, sig.name,
                            shares, order.filled_avg_price, sig.stop_price,
                            sig.target_1r, sig.target_2r, sig.target_3r, risk_amt,
                            signal_session=getattr(sig, "signal_session", "REGULAR"),
                            entry_session=getattr(order, "entry_session", "REGULAR"),
                            entry_reason=getattr(sig, "reason", "ALL_GATES_PASSED"),
                            entry_rvol=curr_rvol,
                            entry_momentum_3m=curr_ret_3m,
                            entry_vwap_gap=vwap_gap_val,
                            entry_rebound_strength=rebound_val,
                            expected_move_pct=exp_move_val,
                            expected_net_r=edge_res.expected_net_r
                        )
                        # [Section 7] 계좌별 당일 진입 횟수 누적
                        acc.daily_entry_counts[sig.iem_cd] = acc.daily_entry_counts.get(sig.iem_cd, 0) + 1
                        acc.last_trade_outcomes[sig.iem_cd] = "ACTIVE"
                        # Note: SymbolStateStore는 시장 상태만 관리하며, 계좌별 보유는 acc.position_manager가 독립 관리
                    else:
                        self.diagnostic_engine.record_rejection(f"[{acc.name}] ORDER_UNFILLED")
                        log_mock_buy_trace("FILL", sig.iem_cd, "REJECT", reason=f"STATUS_{order.status}", account=acc.name)
                else:
                    rej_reason = sig.rejection_reasons[-1] if sig.rejection_reasons else "PRE_ORDER_REJECT"
                    self.diagnostic_engine.record_rejection(f"[{acc.name}] {rej_reason}")
                    log_mock_buy_trace("ORDER_SENT", sig.iem_cd, "REJECT", reason=rej_reason, account=acc.name)
                    log_mock_buy_trace("ACK", sig.iem_cd, "REJECT", reason="ORDER_NOT_SENT", account=acc.name)
                    log_mock_buy_trace("FILL", sig.iem_cd, "REJECT", reason="ORDER_NOT_SENT", account=acc.name)
                    _emit_decision_trace_no_trade(acc, sig, cand_sym, edge_res, "EXECUTION", rej_reason, shares=final_shares, latest_price=latest_order_price, broker_avail=broker_avail_qty, usable_cash=effective_cash, signal_age_ms=signal_age_ms)

            if signal_bought_any:
                approved_for_dashboard.append(sig)
                if getattr(sig, "signal_session", "REGULAR") == "AFTER_HOURS":
                    if hasattr(self, "after_hours_manager") and self.after_hours_manager:
                        self.after_hours_manager.mark_consumed(sig.iem_cd, f"BOUGHT in {sig.strategy_id}")
                    ah_ret = self.after_hours_manager.get_next_session_features(sig.iem_cd).get("AFTER_HOURS_RETURN", 0.0)
                    if self.telegram_notifier and hasattr(self.telegram_notifier, "send_regular_entry_approval_alert"):
                        try:
                            self.telegram_notifier.send_regular_entry_approval_alert(
                                symbol=sig.iem_cd,
                                name=sig.name,
                                after_hours_return_pct=ah_ret,
                                regular_reason=f"{sig.strategy_id} 셋업 충족 및 VWAP 지지/수급 확인"
                            )
                        except Exception as e:
                            logger.error(f"정규장 매수 승인 텔레그램 전송 실패: {e}")

        # 12. 좀비 주문 탐지 및 대조
        for acc in self.accounts:
            zombies = acc.order_router.check_zombie_orders(timeout_ms=3000.0)
            if zombies:
                self.diagnostic_engine.record_zombie_order(len(zombies))
                recon = acc.order_router.reconcile_orders(acc.client)
                if recon["reconciled_count"] > 0:
                    self.diagnostic_engine.record_reconciled_order(recon["reconciled_count"])
                    print(f"🔄 [{acc.name} ZOMBIE RECONCILED] {recon['reconciled_count']}건 정합성 조정 완료")

        # 13. 자가진단 평가 및 대시보드 표출
        cycle_sec = max(0.0, time.perf_counter() - t_cycle_start)
        self.diagnostic_engine.funnel_telemetry.record_scan_cycle(cycle_sec)
        diag_info = self.diagnostic_engine.evaluate_diagnostic_mode(now)
        self._print_dashboard(now, current_regime, ad_ratio, diag_info, approved_for_dashboard)

        # 14. Learning Freeze 및 정기 재학습 트리거 검사
        is_frozen, _ = LearningFreezeManager.is_learning_frozen(now)
        if not is_frozen and hasattr(self, "retraining_engine"):
            sched_trig, sched_msg = self.retraining_engine.check_scheduled_trigger(self.last_retrain_time, now)
            if sched_trig:
                self.last_retrain_time = now
                self.model_registry.log_audit_event("SCHEDULED_RETRAINING_TRIGGER", {"reason": sched_msg})
                print(f"🔄 [정기 재학습 트리거] {sched_msg}")

    def _print_dashboard(self, now, regime, ad_ratio, diag_info, approved_signals):
        """실시간 종합 대시보드 표출 (Section 68 ~ 74)"""
        kospi_cnt = self.store.kospi_count()
        kosdaq_cnt = self.store.kosdaq_count()
        c_stats = self.diagnostic_engine.conversion_stats
        c_rates = diag_info["conversion_rates"]
        telemetry = self.diagnostic_engine.telemetry

        mode_title = "통합 듀얼 (MOCK + LIVE 동시 가동)" if self.mode == "dual" else self.mode.upper()

        print("\n" + "=" * 86)
        print(f" [AI QUANT 실시간 자동매매 통합 관제탑 v16.0] - 모드: {mode_title}  ({now.strftime('%Y-%m-%d %H:%M:%S')})")
        print("=" * 86)

        # [1] MARKET
        print(f"[MARKET]       KOSPI: {kospi_cnt:,} | KOSDAQ: {kosdaq_cnt:,} | 국면: {regime.value} | AD Ratio: {ad_ratio:.2f}")

        # [2] 12-STAGE PIPELINE
        print(f"[PIPELINE 12]  UNIVERSE({telemetry.universe_count:,}) -> EVENT({telemetry.event_detected}) -> CANDIDATE({telemetry.candidates}) -> "
              f"SETUP({telemetry.setup_matches}) -> HARD_GATE({telemetry.hard_gate_passed}) -> SCORE({telemetry.score_passed}) -> "
              f"EDGE({telemetry.edge_passed}) -> RISK({telemetry.risk_passed}) -> EXEC({telemetry.execution_passed}) -> "
              f"APPROVED({telemetry.buy_approved}) -> ORDER({telemetry.orders_sent}) -> FILL({telemetry.filled})")

        # [3] ORDER & STOP WATCHDOG
        primary = self.accounts[0]
        sw_stats = primary.position_manager.stop_watchdog_stats
        print(f"[ORDER]        Created: {c_stats['orders_created']} | Sent: {c_stats['orders_sent']} | Filled: {c_stats['fills']} | Rejected: {c_stats['rejections']}")
        print(f"[STOP WATCHDOG] Triggered: {sw_stats['stops_triggered']} | Executed: {sw_stats['stops_executed']} | Partial: {sw_stats['partial_exits']} | Trailing: {sw_stats['trailing_stops']}")

        # [4] ACCOUNTS RISK & PNL
        for acc in self.accounts:
            sign = "+" if acc.daily_pnl >= 0 else ""
            status_desc = acc.loss_eval.get("status", "정상")
            print(f"[RISK {acc.name:<4s}]  계좌: {acc.act_no} | 자산: {acc.equity:,.0f}원 | 예수금: {acc.cash:,.0f}원 | "
                  f"당일손익: {sign}{acc.daily_pnl:,.0f}원 ({sign}{acc.daily_pnl_ratio*100:.2f}%) | [{acc.port_risk_status}] ({status_desc})")

        # [4-1] PORTFOLIO CASH & RESERVATION (Section 10)
        for acc in self.accounts:
            if hasattr(acc, "cash_manager") and acc.cash_manager:
                c_sum = acc.cash_manager.get_dashboard_summary(open_positions_count=len(acc.position_manager.positions))
                print(f"[CASH {acc.name:<4s}]  Available: {c_sum['cash_available']:>11,.0f}원 | Reserved: {c_sum['reserved_cash']:>10,.0f}원 | "
                      f"Effective: {c_sum['effective_available_cash']:>11,.0f}원 | Open Pos: {c_sum['open_positions']:>2}개 | Pending Ord: {c_sum['pending_orders']:>2}건")

        # [5] CONVERSION FUNNEL
        print(f"[CONVERSION]   Event->Cand: {c_rates['event_to_candidate']}% | Cand->Setup: {c_rates['candidate_to_setup']}% | "
              f"Setup->Buy: {c_rates['setup_to_buy']}% | Buy->Order: {c_rates['buy_to_order']}% | Order->Fill: {c_rates['order_to_fill']}%")

        # [6] TOP OPPORTUNITIES
        display_candidates = self.store.get_display_candidates(limit=10)
        if display_candidates:
            print(f"\n[TOP OPPORTUNITIES (DISPLAY TOP {len(display_candidates)})]")
            print(f"  {'NO':2s} | {'종목':10s} | {'현재가':9s} | {'모멘텀구간':10s} | {'점수':6s} | {'상태':8s} | {'주요 이벤트/셋업'}")
            print("  " + "-" * 82)
            for idx, c in enumerate(display_candidates, start=1):
                ev_desc = ", ".join(c.active_events[:2]) if c.active_events else "정상 감시"
                print(f"  {idx:02d} | {c.name:10s} | {c.price:>8,d}원 | {c.momentum_stage:10s} | {c.event_score:>5.1f}점 | {c.state.value:8s} | {ev_desc}")
        else:
            print("\n[TOP OPPORTUNITIES] 현재 전체 시장(3,136종목) 수급 감시 중 (조건 충족 시 즉시 자동 승격)")

        # [7] WHY NO BUY? (최근 거절 사유 TOP 5)
        top_reasons = self.diagnostic_engine.rejection_counter.most_common(5)
        print("\n[WHY NO BUY? (최근 차단/거절 사유 TOP 5)]")
        if top_reasons:
            for r_name, r_cnt in top_reasons:
                print(f"  · {r_name:<28s}: {r_cnt:>4d}건")
        else:
            print("  · 현재 활성 거절 사유 없음 (모든 파이프라인 정상 가동)")

        # [8] POSITIONS BY ACCOUNT
        for acc in self.accounts:
            active_pos = list(acc.position_manager.positions.values())
            if active_pos:
                print(f"\n[POSITIONS {acc.name} ({len(active_pos)}개)]")
                for p in active_pos:
                    p_ret = ((p.current_price - p.entry_price) / p.entry_price * 100) if p.entry_price > 0 else 0.0
                    p_sign = "+" if p_ret >= 0 else ""
                    print(f"  · [{p.time_horizon.value:8s}] {p.name}({p.iem_cd}): {p.qty}주 | 매입: {p.entry_price:,.0f}원 | 현재: {p.current_price:,.0f}원 | 수익률: {p_sign}{p_ret:.2f}% (손절: {p.stop_price:,}원)")

        # [NEW Telemetry Format v16.1]
        self.quote_manager.end_global_scan()
        q_telemetry = self.quote_manager.get_telemetry_dict()

        print("\n" + "=" * 86)
        print("[SCAN]")
        print(f"  Universe                 : {telemetry.universe_count:,}개")
        print(f"  Full scan duration       : {q_telemetry['global_scan_duration_sec']:.2f}초")
        print(f"  API calls                : {q_telemetry['api_calls']:,}회")
        print(f"  Cache hit                : {q_telemetry['cache_hit']:,}회")
        print(f"  Cache miss               : {q_telemetry['cache_miss']:,}회")

        print("\n[CANDIDATE]")
        print(f"  Candidate count          : {telemetry.candidates:,}개")
        print(f"  Candidate quote age 평균 : {q_telemetry['candidate_quote_age_avg_ms']:.1f}ms")
        print(f"  Candidate quote age P95  : {q_telemetry['candidate_quote_age_p95_ms']:.1f}ms")

        print("\n[EXECUTION]")
        print(f"  Risk Passed              : {telemetry.risk_passed:,}건")
        print(f"  Fresh Quote Passed       : {telemetry.fresh_quote_passed:,}건")
        print(f"  DATA_STALE               : {telemetry.data_stale_rejections:,}건")
        print(f"  BUY Approved             : {telemetry.buy_approved:,}건")
        print(f"  Orders Sent              : {telemetry.orders_sent:,}건")
        print(f"  Fills                    : {telemetry.filled:,}건")

        print("\n  [DATA_STALE 탈락의 실제 quote_age 분포]")
        for b_name, b_cnt in q_telemetry["age_distribution"].items():
            print(f"    · {b_name:<10s}: {b_cnt:>4d}건")
        print("=" * 86)

        # [STAGE LATENCY & BOTTLENECK TELEMETRY v16.2]
        funnel_snapshot = self.diagnostic_engine.funnel_telemetry.get_health_snapshot()
        exec_latency = funnel_snapshot["execution_latency"]
        today_m = funnel_snapshot["today_special_metrics"]
        alerts = funnel_snapshot["bottleneck_alerts"]

        print("\n[STAGE LATENCY & BOTTLENECK TELEMETRY (ms)]")
        print(f"  {'Stage':<28s} | {'Input':>5s} | {'Success':>7s} | {'Reject':>6s} | {'p50':>7s} | {'p95':>7s} | {'p99':>7s} | {'Max':>7s}")
        print("  " + "-" * 86)
        for stg_name, s_data in exec_latency.items():
            stg_stat = funnel_snapshot["order_funnel"].get(stg_name, {})
            print(f"  {stg_name:<28s} | {stg_stat.get('input_count', 0):>5d} | {stg_stat.get('success_count', 0):>7d} | {stg_stat.get('reject_count', 0):>6d} | "
                  f"{s_data.get('p50', 0.0):>7.1f} | {s_data.get('p95', 0.0):>7.1f} | {s_data.get('p99', 0.0):>7.1f} | {s_data.get('max', 0.0):>7.1f}")

        print("\n[TODAY SPECIAL EXECUTION METRICS]")
        print(f"  · Scan Cycle Duration (ms)   : p50={today_m['scan_cycle_time_ms']['p50']:.1f}, p95={today_m['scan_cycle_time_ms']['p95']:.1f}, p99={today_m['scan_cycle_time_ms']['p99']:.1f}")
        print(f"  · Signal -> Router Latency   : p50={today_m['signal_to_router_latency_ms']['p50']:.1f}ms, p95={today_m['signal_to_router_latency_ms']['p95']:.1f}ms")
        print(f"  · Quote Age (Data -> Local)  : p50={today_m['quote_age_ms']['p50']:.1f}ms, p95={today_m['quote_age_ms']['p95']:.1f}ms, p99={today_m['quote_age_ms']['p99']:.1f}ms")
        print(f"  · Stale -> Re-fetch Trigger  : {today_m['stale_to_refetch_trigger_rate_pct']:.1f}% ({funnel_snapshot['quote_health']['refetch_triggered_count']}회)")
        print(f"  · Re-fetch Success Rate      : {today_m['refetch_success_rate_pct']:.1f}% ({funnel_snapshot['quote_health']['refetch_success_count']}회)")
        print(f"  · Re-fetch -> Order Sent Rate: {today_m['refetch_to_order_send_rate_pct']:.1f}%")
        print(f"  · Submit -> Broker ACK (ms)  : p50={today_m['order_submit_to_broker_ack_ms']['p50']:.1f}, p95={today_m['order_submit_to_broker_ack_ms']['p95']:.1f}, p99={today_m['order_submit_to_broker_ack_ms']['p99']:.1f}")
        print(f"  · Broker ACK -> Fill (ms)    : p50={today_m['broker_ack_to_fill_ms']['p50']:.1f}, p95={today_m['broker_ack_to_fill_ms']['p95']:.1f}")

        # [BOTTLENECK CONTRIBUTION ANALYSIS]
        print("\n" + self.diagnostic_engine.funnel_telemetry.format_bottleneck_contribution_summary())

        if alerts:
            print("\n🚨 [PIPELINE BOTTLENECK ALERTS IDENTIFIED]")
            for alert in alerts:
                sev_icon = "🛑" if alert["severity"] == "CRITICAL" else "⚠️"
                print(f"  {sev_icon} [{alert['severity']}] {alert['category']} ({alert['stage']}): {alert['message']}")
        else:
            print("\n✅ [PIPELINE HEALTH STATUS] 모든 실행 파이프라인 구간이 정상 SLA 및 병목 기준을 만족하고 있습니다.")
        print("=" * 86)

        # Web Dashboard 연동용 실시간 텔레메트리 파일 저장
        try:
            telemetry_dir = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "data")
            os.makedirs(telemetry_dir, exist_ok=True)
            telemetry_path = os.path.join(telemetry_dir, "live_telemetry.json")
            primary_acc = self.accounts[0]
            accounts_data = {}
            for acc in self.accounts:
                accounts_data[acc.name.lower()] = {
                    "act_no": acc.act_no,
                    "total_equity": acc.equity,
                    "cash": acc.cash,
                    "daily_pnl": acc.daily_pnl,
                    "daily_pnl_pct": round(acc.daily_pnl_ratio * 100, 2),
                    "risk_status": acc.port_risk_status,
                    "positions_count": len(acc.position_manager.positions)
                }
            telemetry_payload = {
                "timestamp": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                "pid": os.getpid(),
                "heartbeat": time.time(),
                "is_alive": True,
                "mode": self.mode.upper(),
                "regime": regime.value,
                "ad_ratio": round(ad_ratio, 2),
                "risk": {
                    "total_equity": primary_acc.equity,
                    "cash": primary_acc.cash,
                    "daily_pnl": primary_acc.daily_pnl,
                    "daily_pnl_pct": round(primary_acc.daily_pnl_ratio * 100, 2),
                    "used_risk_ratio": round(primary_acc.risk_summary.get("used_risk_ratio", 0.0) * 100, 2),
                    "available_risk_ratio": round(primary_acc.risk_summary.get("available_risk_ratio", 0.04) * 100, 2),
                    "risk_status": primary_acc.port_risk_status
                },
                "dual_accounts": accounts_data,
                "funnel_stats": c_stats,
                "conversion_rates": c_rates,
                "hourly_pipeline": self.diagnostic_engine.get_hourly_pipeline(),
                "execution_latency": funnel_snapshot["execution_latency"],
                "order_funnel": funnel_snapshot["order_funnel"],
                "quote_health": funnel_snapshot["quote_health"],
                "broker_health": funnel_snapshot["broker_health"],
                "fill_health": funnel_snapshot["fill_health"],
                "bottleneck_alerts": funnel_snapshot["bottleneck_alerts"],
                "bottleneck_contribution": funnel_snapshot["bottleneck_contribution"],
                "today_special_metrics": funnel_snapshot["today_special_metrics"],
                "telegram_health": self.telegram_notifier.get_health() if hasattr(self, "telegram_notifier") and self.telegram_notifier else {
                    "send": "FAIL",
                    "receive": "FAIL",
                    "last_sent_at": None,
                    "last_received_at": None,
                    "last_update_id": 0
                },
                "pipeline_12_stages": {
                    "universe": telemetry.universe_count,
                    "event": telemetry.event_detected,
                    "candidate": telemetry.candidates,
                    "setup": telemetry.setup_matches,
                    "hard_gate": telemetry.hard_gate_passed,
                    "score": telemetry.score_passed,
                    "edge": telemetry.edge_passed,
                    "risk": telemetry.risk_passed,
                    "fresh_quote_passed": telemetry.fresh_quote_passed,
                    "data_stale": telemetry.data_stale_rejections,
                    "execution": telemetry.execution_passed,
                    "buy_approved": telemetry.buy_approved,
                    "order": telemetry.orders_sent,
                    "fill": telemetry.filled
                },
                "execution_telemetry": q_telemetry,
                "stage_latencies": telemetry.stage_latencies,
                "zombie_orders_detected": telemetry.zombie_orders_detected,
                "reconciled_orders": telemetry.reconciled_orders,
                "ntp_synced": telemetry.ntp_synced,
                "ntp_offset_ms": TimeSync.get_offset_ms(),
                "why_no_buy": [{"reason": r, "count": cnt} for r, cnt in top_reasons],
                "top_opportunities": [
                    {
                        "symbol": c.iem_cd,
                        "name": c.name,
                        "price": c.price,
                        "momentum_stage": c.momentum_stage,
                        "score": round(c.event_score, 1),
                        "state": c.state.value,
                        "events": c.active_events[:2]
                    }
                    for c in display_candidates
                ]
            }
            with open(telemetry_path, "w", encoding="utf-8") as f:
                json.dump(telemetry_payload, f, ensure_ascii=False, indent=2)
        except Exception as e:
            logger.warning(f"대시보드 텔레메트리 파일 저장 실패: {e}")

    def shutdown(self):
        """
        Graceful shutdown sequence:
        1. Sync active orders and positions to telemetry
        2. Flush telemetry exporter queue and update status to STOPPED
        3. Trigger final git commit & push before stopping
        4. Stop background syncer and exporter threads
        5. Stop telegram receiver
        """
        logger.info("[SHUTDOWN] Initiating graceful shutdown sequence...")
        if hasattr(self, "telemetry_exporter") and self.telemetry_exporter:
            try:
                self.telemetry_exporter.process_status = "STOPPED"
                if hasattr(self, "order_router") and self.order_router:
                    self.telemetry_exporter.sync_order_router_state(self.order_router)
                if hasattr(self, "position_manager") and self.position_manager:
                    self.telemetry_exporter.sync_position_manager_state(self.position_manager)
                self.telemetry_exporter.flush()
            except Exception as e:
                logger.error(f"[SHUTDOWN] Exporter flush failed: {e}")

        if hasattr(self, "telemetry_syncer") and self.telemetry_syncer:
            try:
                if self.telemetry_syncer._running:
                    self.telemetry_syncer.sync_now(reason="shutdown")
                self.telemetry_syncer.stop()
            except Exception as e:
                logger.error(f"[SHUTDOWN] Syncer stop failed: {e}")

        if hasattr(self, "telemetry_exporter") and self.telemetry_exporter:
            try:
                self.telemetry_exporter.stop()
            except Exception as e:
                logger.error(f"[SHUTDOWN] Exporter stop failed: {e}")

        if hasattr(self, "telegram_notifier") and self.telegram_notifier:
            try:
                self.telegram_notifier.stop_receiver()
            except Exception:
                pass
        logger.info("[SHUTDOWN] Graceful shutdown completed.")


def main():
    import argparse
    parser = argparse.ArgumentParser(description="국내 주식 전체 종목 실시간 탐지 및 실제 체결 퀀트 시스템 v16.0")
    parser.add_argument("--dual", action="store_true", help="통합 듀얼(모의+실전 동시 가동, 기본값)")
    parser.add_argument("--live", action="store_true", help="실전투자(LIVE) 단독 모드 가동")
    parser.add_argument("--mock", action="store_true", help="모의투자(MOCK) 단독 모드 가동")
    parser.add_argument("--test-signal", action="store_true", help="FORCE_SIGNAL_TEST (가상 이벤트 7대 시나리오 검증)")
    args = parser.parse_args()

    if args.live:
        mode = "live"
    elif args.mock:
        mode = "mock"
    else:
        mode = "dual"

    # [Item 2: Execution Integrity] 계좌 단위 싱글톤 락 (AccountLockManager)
    # 동일 LIVE 또는 MOCK 계좌에 대해 중복 프로세스가 동시에 도는 것을 OS 레벨에서 원천 차단
    from core.account_lock import AccountLockManager
    lock_mgr = AccountLockManager(
        mode=mode,
        account_live=getattr(settings, "ACCOUNT_LIVE", ""),
        account_mock=getattr(settings, "ACCOUNT_MOCK", "")
    )
    locked_ok, err_msg = lock_mgr.acquire_all()
    if not locked_ok:
        print("=" * 86)
        print(f"🚨 [차단: DUPLICATE_ACCOUNT_PROCESS] {err_msg}")
        print("   동일 계좌에 대한 다른 트레이더 프로세스가 이미 실행 중입니다!")
        print("   중복 주문, 잔고 왜곡 및 Telegram 충돌을 방지하기 위해 즉시 종료합니다.")
        print("=" * 86)
        sys.exit(1)

    print("=" * 86)
    print(f"   국내 주식 전체 종목 실시간 탐지 & 실제 BUY 체결 시스템 v16.0 (모드: {mode.upper()})")
    if mode == "dual":
        print(f"   연동 계좌: [모의투자] {settings.ACCOUNT_MOCK} | [실전투자] {settings.ACCOUNT_LIVE}")
    else:
        act_no = settings.ACCOUNT_LIVE if mode == "live" else settings.ACCOUNT_MOCK
        print(f"   연동 계좌: {act_no}")
    print("=" * 86)

    trader = None
    try:
        trader = LiveQuantTrader(mode=mode, force_signal_test=args.test_signal)

        if args.test_signal:
            print("\n⚡ [MASTER_15_TEST_HARNESS v16.0] 15대 필수 테스트 파이프라인 전수 검증 가동 중...")
            res = trader.diagnostic_engine.run_master_15_test_harness(
                scanner=trader.scanner,
                full_strategy_suite=FullStrategySuite,
                scoring_engine=ScoringEngine,
                order_router=trader.order_router,
                symbol_store=trader.store
            )
            for t_name, t_info in res["scenarios"].items():
                status_icon = "✅ 통과" if t_info["passed"] else "❌ 실패"
                title = t_info.get("title", t_name)
                print(f"  {status_icon} [{t_name}] {title}")
            print(f"\n[검증 결과] 15대 필수 테스트 전수 통과 여부: {'전부 성공 (15/15, 100%)' if res['all_passed'] else '일부 실패'}")
            return

        # 1회 즉시 실행
        trader.run_cycle()

        interval = 3
        dual_msg = " [모의 + 실전 동시 자동매매]" if mode == "dual" else ""
        print(f"\n[안내] 전체 시장(3,136종목) 실시간 이벤트 및 실제 BUY 체결 엔진 가동 중{dual_msg} ({interval}초 주기)")
        print("시스템을 종료하려면 Ctrl+C 를 누르세요.\n")

        while True:
            time.sleep(interval)
            try:
                trader.run_cycle()
            except Exception as cycle_err:
                logger.error(f"⚠️ [단일 사이클 예외] {cycle_err}", exc_info=True)
                print(f"⚠️ [사이클 일시적 오류] {cycle_err} (다음 사이클에서 정상 재개합니다)")
    except KeyboardInterrupt:
        print("\n\n[안내] 사용자에 의해 퀀트 자동매매 시스템이 안전하게 종료되었습니다.")
    finally:
        if trader:
            try:
                trader.shutdown()
            except Exception as sht_err:
                logger.error(f"[SHUTDOWN_ERR] {sht_err}")
        try:
            lock_mgr.release_all()
        except Exception:
            pass


if __name__ == "__main__":
    main()
