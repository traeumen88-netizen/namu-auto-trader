"""실시간 파이프라인 진단 및 관제 엔진 (Execution Diagnostic Engine v8.0)
- Conversion Funnel 추적 (Universe -> Event -> Candidate -> Setup -> Approved -> Sent -> Filled)
- BUY_BLOCK_REASON 실시간 집계 및 TOP 10 거절 사유 분석
- 최근 30분 무체결 시 자동 DIAGNOSTIC MODE 발동 및 병목 원인 진단
- 5대 가상 시나리오(TEST A~E) 기반 FORCE_SIGNAL_TEST 파이프라인 검증기
"""

import time
import logging
from collections import Counter
from datetime import datetime, timedelta
from typing import Dict, List, Any, Optional, Tuple

from core.models import (
    SymbolInfo, SymbolState, TradeSignal, Order, OrderStatus, OrderType,
    OrderSide, TimeHorizon, MarketRegime, PipelineTelemetry, Tick
)
from execution.execution_funnel_telemetry import ExecutionFunnelTelemetry

logger = logging.getLogger("DiagnosticEngine")


class DiagnosticEngine:
    """실시간 주문 흐름 및 매수 차단 원인 자가 진단 관제 엔진"""

    def __init__(self):
        self.telemetry = PipelineTelemetry()
        self.funnel_telemetry = ExecutionFunnelTelemetry()
        self.rejection_counter = Counter()
        self.conversion_stats = {
            "events": 0,
            "candidates": 0,
            "setup_matches": 0,
            "buy_approved": 0,
            "orders_created": 0,
            "orders_sent": 0,
            "fills": 0,
            "rejections": 0
        }
        self.start_time = datetime.now()
        self.last_buy_time: Optional[datetime] = None
        self.diagnostic_active: bool = False
        self.diagnostic_report: str = "정상 가동 중"
        # 시간대별(09:00~15:00) 파이프라인 전환 추이 집계 (후보 발굴, 셋업 생성, 체결)
        self.hourly_pipeline: Dict[str, Dict[str, int]] = {
            f"{h:02d}:00": {"candidates": 0, "setups": 0, "fills": 0}
            for h in range(9, 16)
        }
        self.stage_latency_samples: Dict[str, List[float]] = {
            "universe": [],
            "event": [],
            "candidate": [],
            "setup": [],
            "hard_gate": [],
            "score": [],
            "edge": [],
            "risk": [],
            "execution": [],
            "buy_approved": [],
            "order": [],
            "fill": []
        }

    def record_universe(self, count: int = 3136):
        self.telemetry.universe_count = count

    def record_event_detected(self, count: int = 1):
        self.conversion_stats["events"] += count
        self.telemetry.event_detected += count

    def record_candidate(self, count: int = 1):
        self.conversion_stats["candidates"] += count
        self.telemetry.candidates += count
        h_key = f"{datetime.now().hour:02d}:00"
        if h_key in self.hourly_pipeline:
            self.hourly_pipeline[h_key]["candidates"] += count

    def record_setup_match(self, count: int = 1):
        self.conversion_stats["setup_matches"] += count
        self.telemetry.setup_matches += count
        self.telemetry.buy_candidates += count
        h_key = f"{datetime.now().hour:02d}:00"
        if h_key in self.hourly_pipeline:
            self.hourly_pipeline[h_key]["setups"] += count

    def record_hard_gate_passed(self, count: int = 1):
        self.telemetry.hard_gate_passed += count

    def record_score_passed(self, count: int = 1):
        self.telemetry.score_passed += count

    def record_edge_passed(self, count: int = 1):
        self.telemetry.edge_passed += count

    def record_risk_passed(self, count: int = 1):
        self.telemetry.risk_passed += count

    def record_fresh_quote_passed(self, count: int = 1):
        self.telemetry.fresh_quote_passed += count

    def record_data_stale(self, age_sec: float, count: int = 1):
        self.telemetry.data_stale_rejections += count
        bucket = "0~1초" if age_sec < 1.0 else ("1~3초" if age_sec < 3.0 else ("3~5초" if age_sec < 5.0 else ("5~10초" if age_sec < 10.0 else ("10~30초" if age_sec < 30.0 else "30초 이상"))))
        self.telemetry.data_stale_distribution[bucket] = self.telemetry.data_stale_distribution.get(bucket, 0) + count

    def record_execution_passed(self, count: int = 1):
        self.telemetry.execution_passed += count

    def record_buy_approved(self, count: int = 1):
        self.conversion_stats["buy_approved"] += count
        self.telemetry.buy_approved += count
        self.last_buy_time = datetime.now()
        self.telemetry.last_buy_time = self.last_buy_time

    def record_order_created(self, count: int = 1):
        self.conversion_stats["orders_created"] += count
        self.telemetry.orders_created += count

    def record_order_sent(self, count: int = 1):
        self.conversion_stats["orders_sent"] += count
        self.telemetry.orders_sent += count

    def record_fill(self, count: int = 1):
        self.conversion_stats["fills"] += count
        self.telemetry.filled += count
        h_key = f"{datetime.now().hour:02d}:00"
        if h_key in self.hourly_pipeline:
            self.hourly_pipeline[h_key]["fills"] += count

    def get_hourly_pipeline(self) -> List[Dict[str, Any]]:
        """시간대별 파이프라인 집계 데이터 (차트 표출용)"""
        return [
            {
                "time": t,
                "candidates": stats["candidates"],
                "setups": stats["setups"],
                "fills": stats["fills"]
            }
            for t, stats in sorted(self.hourly_pipeline.items())
        ]

    def record_zombie_order(self, count: int = 1):
        self.telemetry.zombie_orders_detected += count

    def record_reconciled_order(self, count: int = 1):
        self.telemetry.reconciled_orders += count

    def record_stage_latency(self, stage: str, latency_ms: float):
        """단계별 지연시간(Avg, P95, Max) 표본 축적 및 백분위수 갱신"""
        if stage not in self.stage_latency_samples:
            self.stage_latency_samples[stage] = []
        samples = self.stage_latency_samples[stage]
        samples.append(latency_ms)
        if len(samples) > 200:
            samples.pop(0)

        sorted_s = sorted(samples)
        p95_idx = int(len(sorted_s) * 0.95)
        p95_val = sorted_s[min(p95_idx, len(sorted_s) - 1)]
        avg_val = sum(sorted_s) / float(len(sorted_s))
        max_val = sorted_s[-1]

        self.telemetry.stage_latencies[stage] = {
            "avg": round(avg_val, 2),
            "p95": round(p95_val, 2),
            "max": round(max_val, 2)
        }

    def record_rejection(self, reason: str, count: int = 1):
        self.conversion_stats["rejections"] += count
        self.telemetry.rejected += count
        # 이유 단순화 정규화
        clean_reason = reason.split(":")[0].split("(")[0].strip()
        self.rejection_counter[clean_reason] += count
        self.telemetry.top_rejection_reasons = dict(self.rejection_counter.most_common(10))

    def record_symbol_block_reasons(self, sym: SymbolInfo):
        if sym.buy_block_reasons:
            for r in sym.buy_block_reasons:
                self.record_rejection(r)

    def get_conversion_rates(self) -> Dict[str, float]:
        """단계별 전환율(Conversion Rate) 산출"""
        ev = max(1, self.conversion_stats["events"])
        cand = self.conversion_stats["candidates"]
        setup = self.conversion_stats["setup_matches"]
        buy = self.conversion_stats["buy_approved"]
        sent = self.conversion_stats["orders_sent"]
        fills = self.conversion_stats["fills"]

        return {
            "event_to_candidate": round((cand / ev) * 100, 1),
            "candidate_to_setup": round((setup / max(1, cand)) * 100, 1),
            "setup_to_buy": round((buy / max(1, setup)) * 100, 1),
            "buy_to_order": round((sent / max(1, buy)) * 100, 1),
            "order_to_fill": round((fills / max(1, sent)) * 100, 1)
        }

    def evaluate_diagnostic_mode(self, now: Optional[datetime] = None) -> Dict[str, Any]:
        """
        최근 30분 무체결 시 자동 자가진단(Diagnostic Mode) 활성화 (Section 23 & 25 & 26)
        """
        now = now or datetime.now()
        idle_duration = (now - (self.last_buy_time or self.start_time)).total_seconds()
        
        # 30분 이상 주문 없을 시 DIAGNOSTIC MODE 활성화
        self.diagnostic_active = idle_duration >= 1800.0 or (self.conversion_stats["candidates"] >= 10 and self.conversion_stats["buy_approved"] == 0)
        self.telemetry.diagnostic_active = self.diagnostic_active

        bottlenecks = []
        c = self.conversion_stats

        if c["candidates"] >= 10 and c["setup_matches"] == 0:
            bottlenecks.append("SETUP FILTER BOTTLENECK: 후보는 발생하나 전략 조건(Setup)과 매칭되지 않음")
        elif c["setup_matches"] >= 5 and c["buy_approved"] == 0:
            bottlenecks.append("ENTRY SCORE BOTTLENECK: 셋업은 매칭되나 점수(Soft Score < 60) 미달로 탈락")
        elif c["buy_approved"] >= 1 and c["orders_sent"] == 0:
            bottlenecks.append("RISK / EXECUTION BOTTLENECK: BUY 승인되었으나 리스크 한도 또는 10단계 사전검증에서 반려")
        elif c["orders_sent"] >= 1 and c["fills"] == 0:
            bottlenecks.append("ORDER FILL BOTTLENECK: 주문 전송되었으나 브로커 API 거절 또는 호가 미체결")

        top_reasons = self.rejection_counter.most_common(5)
        reasons_summary = ", ".join(f"{k}({v}건)" for k, v in top_reasons) if top_reasons else "없음"

        if bottlenecks:
            msg = " / ".join(bottlenecks) + f" | 주요 거절: {reasons_summary}"
        elif self.diagnostic_active:
            msg = f"최근 {idle_duration/60:.1f}분간 주문 없음 (후보: {c['candidates']}건, 셋업: {c['setup_matches']}건) | 주요 사유: {reasons_summary}"
        else:
            msg = "정상 탐지 및 집행 상태 유지 중"

        self.diagnostic_report = msg
        self.telemetry.diagnostic_message = msg

        return {
            "is_active": self.diagnostic_active,
            "idle_minutes": round(idle_duration / 60, 1),
            "bottlenecks": bottlenecks,
            "message": msg,
            "conversion_rates": self.get_conversion_rates(),
            "top_rejection_reasons": dict(top_reasons)
        }

    def record_funnel_event(
        self,
        event_name: str,
        symbol: str,
        strategy: str,
        order_id: str,
        result: str = "SUCCESS",
        reject_reason: str = "",
        quote_age_ms: float = 0.0,
        elapsed_ms: Optional[float] = None,
        now_dt: Optional[datetime] = None,
        metadata: Optional[Dict[str, Any]] = None
    ):
        """퍼널 전 구간 10대 이벤트 및 지연시간 계측 위임"""
        return self.funnel_telemetry.record_funnel_event(
            event_name=event_name,
            symbol=symbol,
            strategy=strategy,
            order_id=order_id,
            result=result,
            reject_reason=reject_reason,
            quote_age_ms=quote_age_ms,
            elapsed_ms=elapsed_ms,
            now_dt=now_dt,
            metadata=metadata
        )

    def get_funnel_health_snapshot(self) -> Dict[str, Any]:
        """6대 실시간 Health Snapshot 반환 위임"""
        return self.funnel_telemetry.get_health_snapshot()

    def run_force_signal_test(
        self,
        scanner,
        full_strategy_suite,
        scoring_engine,
        order_router,
        symbol_store,
        now: Optional[datetime] = None
    ) -> Dict[str, Any]:
        now = now or datetime.now()
        results = {}

        if order_router is None:
            class _DummyCircuitBreaker:
                is_tripped = False
                trip_reason = ""
                def check_data_staleness(self, dt): return False
                def record_order_success(self): pass
                def record_order_failure(self, err): pass
            from execution.order_router import OrderRouter
            order_router = OrderRouter(namu_client=None, circuit_breaker=_DummyCircuitBreaker())

        # -------------------------------------------------------------
        # TEST A: Momentum Ignition (가격 +2%, RVOL 3.0, Above VWAP, EMA상승 -> BUY)
        # -------------------------------------------------------------
        sym_a = SymbolInfo(iem_cd="SIM_A", name="모멘텀점화테스트A", market="KOSPI", price=51000, open_price=50000, is_tradable=True)
        symbol_store.register_symbol(sym_a)
        agg_a = scanner.get_aggregator("SIM_A")
        from core.models import Candle
        t_base = now - timedelta(minutes=5)
        for i in range(5):
            c = Candle(timestamp=t_base + timedelta(minutes=i), timeframe="1m", open=50000 + i*100, high=50200 + i*100, low=49900 + i*100, close=50100 + i*100, volume=10000, is_closed=True)
            agg_a.candles_1m.append(c)
        agg_a.current_1m = Candle(timestamp=now, timeframe="1m", open=50000, high=51200, low=50000, close=51000, volume=35000)
        agg_a.cum_volume = 85000
        agg_a.cum_turnover = 85000 * 50500
        agg_a.vwap = 50500.0

        sigs_a = full_strategy_suite.evaluate_intraday_all(
            sym=sym_a, agg=agg_a, regime=MarketRegime.STRONG_BULL, now=now,
            patterns={"is_ignition": True}
        )
        results["TEST_A"] = {
            "setup": "MOMENTUM_IGNITION",
            "passed": len(sigs_a) > 0 and sigs_a[0].score >= 60.0,
            "signals_count": len(sigs_a),
            "signal": sigs_a[0].reason if sigs_a else sym_a.buy_block_reasons
        }

        # -------------------------------------------------------------
        # TEST B: Breakout (전일고가 돌파, RVOL 2.0 -> BUY)
        # -------------------------------------------------------------
        sym_b = SymbolInfo(iem_cd="SIM_B", name="돌파테스트B", market="KOSPI", price=72000, prev_high=70000, open_price=69000, is_tradable=True)
        symbol_store.register_symbol(sym_b)
        agg_b = scanner.get_aggregator("SIM_B")
        for i in range(5):
            c = Candle(timestamp=t_base + timedelta(minutes=i), timeframe="1m", open=69500, high=70000, low=69000, close=69800, volume=5000, is_closed=True)
            agg_b.candles_1m.append(c)
        agg_b.current_1m = Candle(timestamp=now, timeframe="1m", open=70000, high=72500, low=70000, close=72000, volume=15000)
        agg_b.vwap = 70500.0

        sigs_b = full_strategy_suite.evaluate_intraday_all(
            sym=sym_b, agg=agg_b, regime=MarketRegime.BULL, now=now, patterns={}
        )
        results["TEST_B"] = {
            "setup": "BREAKOUT",
            "passed": len(sigs_b) > 0 and sigs_b[0].score >= 60.0,
            "signals_count": len(sigs_b),
            "signal": sigs_b[0].reason if sigs_b else sym_b.buy_block_reasons
        }

        # -------------------------------------------------------------
        # TEST C: VWAP Pullback 후 재상승 -> BUY
        # -------------------------------------------------------------
        sym_c = SymbolInfo(iem_cd="SIM_C", name="눌림목테스트C", market="KOSDAQ", price=20050, open_price=19500, is_tradable=True)
        symbol_store.register_symbol(sym_c)
        agg_c = scanner.get_aggregator("SIM_C")
        agg_c.vwap = 20000.0
        agg_c.current_1m = Candle(timestamp=now, timeframe="1m", open=20000, high=20100, low=19990, close=20050, volume=8000)
        c_prev = Candle(timestamp=now - timedelta(minutes=1), timeframe="1m", open=20100, high=20150, low=19990, close=20010, volume=3000, is_closed=True)
        agg_c.candles_1m.append(c_prev)

        sigs_c = full_strategy_suite.evaluate_intraday_all(
            sym=sym_c, agg=agg_c, regime=MarketRegime.BULL, now=now, patterns={}
        )
        results["TEST_C"] = {
            "setup": "VWAP_PULLBACK",
            "passed": len(sigs_c) > 0 and sigs_c[0].score >= 60.0,
            "signals_count": len(sigs_c),
            "signal": sigs_c[0].reason if sigs_c else sym_c.buy_block_reasons
        }

        # -------------------------------------------------------------
        # TEST D: Compression Breakout -> BUY
        # -------------------------------------------------------------
        sym_d = SymbolInfo(iem_cd="SIM_D", name="압축돌파테스트D", market="KOSPI", price=35500, open_price=35000, is_tradable=True)
        symbol_store.register_symbol(sym_d)
        agg_d = scanner.get_aggregator("SIM_D")
        agg_d.current_1m = Candle(timestamp=now, timeframe="1m", open=35000, high=35600, low=35000, close=35500, volume=20000)
        agg_d.vwap = 35100.0

        sigs_d = full_strategy_suite.evaluate_intraday_all(
            sym=sym_d, agg=agg_d, regime=MarketRegime.BULL, now=now,
            patterns={"is_compression_breakout": True}
        )
        results["TEST_D"] = {
            "setup": "COMPRESSION_BREAKOUT",
            "passed": len(sigs_d) > 0 and sigs_d[0].score >= 60.0,
            "signals_count": len(sigs_d),
            "signal": sigs_d[0].reason if sigs_d else sym_d.buy_block_reasons
        }

        # -------------------------------------------------------------
        # TEST E: 관심종목 밖 미등록 종목 갑자기 거래량 5배 폭증 -> Candidate 생성 -> BUY 판단
        # -------------------------------------------------------------
        code_e = "SIM_E_NEW"
        sym_e = SymbolInfo(iem_cd=code_e, name="신규발굴급등주E", market="KOSDAQ", price=15300, open_price=15000, is_tradable=True)
        symbol_store.register_symbol(sym_e)
        agg_e = scanner.get_aggregator(code_e)
        # 20분간 평이한 거래량 베이스라인 구축 (평균 1,000주)
        for i in range(20):
            c = Candle(timestamp=t_base - timedelta(minutes=20-i), timeframe="1m", open=15000, high=15050, low=14950, close=15000, volume=1000, is_closed=True)
            agg_e.candles_1m.append(c)
        agg_e.cum_volume = 20000
        agg_e.cum_turnover = 20000 * 15000
        agg_e.vwap = 15000.0

        # 신규 급등 틱 유입 (거래량 50,000주 폭증, 체결강도 130%, 순위급등)
        state_e, score_e, sym_e_res = scanner.on_market_tick(
            iem_cd=code_e, price=15300, volume=50000, timestamp=now,
            execution_intensity=130.0, rank_surged=True
        )
        sigs_e = full_strategy_suite.evaluate_intraday_all(
            sym=sym_e_res, agg=agg_e, regime=MarketRegime.BULL, now=now,
            patterns={"is_ignition": True}
        )
        results["TEST_E"] = {
            "setup": "DYNAMIC_DISCOVERY_TO_BUY",
            "passed": (len(sigs_e) > 0 and sigs_e[0].score >= 60.0 and state_e in (SymbolState.WATCH, SymbolState.ACTIVE)),
            "detected_state": state_e.value,
            "signals_count": len(sigs_e),
            "signal": sigs_e[0].reason if sigs_e else sym_e_res.buy_block_reasons
        }

        # -------------------------------------------------------------
        # TEST F: Edge Engine Verification (Ask 1 Entry & Net R >= +0.15R)
        # -------------------------------------------------------------
        from core.edge_engine import EdgeEngine
        edge_eng = EdgeEngine(min_expected_net_r=0.15)
        sym_f = SymbolInfo(iem_cd="SIM_F", name="엣지엔진테스트F", market="KOSPI", price=50000, ask1_price=50100, is_tradable=True)
        sig_f = TradeSignal(
            strategy_id="INT_BREAKOUT",
            time_horizon=TimeHorizon.INTRADAY,
            iem_cd="SIM_F",
            name="엣지엔진테스트F",
            side=OrderSide.BUY,
            strategy_price=50000,
            stop_price=49000,
            target_1r=52000,
            score=85.0,
            reason="Edge Engine Test",
            timestamp=now
        )
        edge_res = edge_eng.calculate_edge(sym=sym_f, signal=sig_f)
        results["TEST_F"] = {
            "setup": "EDGE_ENGINE_ASK1_EXPECTED_NET_R",
            "passed": edge_res.is_approved and edge_res.entry_price == 50100 and edge_res.expected_net_r >= 0.15,
            "entry_price": edge_res.entry_price,
            "expected_net_r": edge_res.expected_net_r,
            "is_approved": edge_res.is_approved
        }

        # -------------------------------------------------------------
        # TEST G: Zombie Order Detection & Reconciliation (> 3000ms ACK Missing)
        # -------------------------------------------------------------
        sym_g = SymbolInfo(iem_cd="SIM_G", name="좀비주문정합성테스트G", market="KOSPI", price=30000, is_tradable=True)
        zombie_order = Order(
            client_order_id="TEST_ZOMBIE_001",
            iem_cd="SIM_G",
            side=OrderSide.BUY,
            order_type=OrderType.MARKET,
            qty=10,
            price=30000,
            strategy_id="INT_MOMENTUM_IGNITION",
            time_horizon=TimeHorizon.INTRADAY,
            status=OrderStatus.PENDING,
            created_at=now - timedelta(seconds=5),
            sent_at=now - timedelta(seconds=5)
        )
        order_router.pending_orders[zombie_order.client_order_id] = zombie_order
        zombies_detected = order_router.check_zombie_orders(timeout_ms=3000)
        self.record_zombie_order(len(zombies_detected))

        class MockClientForReconciliation:
            def get_balance(self):
                return {"cash": 10000000, "holdings": [{"iem_cd": "SIM_G", "qty": 10, "avg_price": 30000}]}

        recon_res = order_router.reconcile_orders(client=MockClientForReconciliation())
        self.record_reconciled_order(recon_res["reconciled_count"])

        results["TEST_G"] = {
            "setup": "ZOMBIE_ORDER_RECONCILIATION",
            "passed": len(zombies_detected) >= 1 and recon_res["filled_count"] >= 1 and zombie_order.client_order_id not in order_router.pending_orders,
            "zombies_detected": len(zombies_detected),
            "reconciled_count": recon_res["reconciled_count"],
            "filled_count": recon_res["filled_count"]
        }

        all_passed = all(t["passed"] for t in results.values())
        return {
            "all_passed": all_passed,
            "scenarios": results,
            "timestamp": now.strftime("%Y-%m-%d %H:%M:%S")
        }

    def run_master_15_test_harness(
        self,
        scanner,
        full_strategy_suite,
        scoring_engine,
        order_router,
        symbol_store,
        now: Optional[datetime] = None
    ) -> Dict[str, Any]:
        """[FINAL MASTER v11.0] Section 90: 개발자가 반드시 수행할 15대 필수 테스트 전수 검증"""
        from core.setup_detector import SetupDetector
        from core.priority_queue import EventPriorityQueue
        from core.edge_engine import EdgeEngine
        from core.event_detector import EventDetector
        from risk.loss_limits import LossLimitManager
        from core.models import Candle
        from execution.order_router import OrderRouter

        now = now or datetime.now()
        results = {}
        setup_det = SetupDetector()
        edge_eng = EdgeEngine(min_expected_net_r=0.15)

        class _DummyCircuitBreaker:
            is_tripped = False
            trip_reason = ""
            def check_data_staleness(self, dt): return False
            def record_order_success(self): pass
            def record_order_failure(self, err): pass

        if order_router is None:
            order_router = OrderRouter(namu_client=None, circuit_breaker=_DummyCircuitBreaker())

        # -----------------------------------------------------------------
        # TEST 1: 관심종목에 없는 종목에서 거래량 5배 -> 자동 탐지
        # -----------------------------------------------------------------
        c1 = "TEST_01"
        sym_1 = SymbolInfo(iem_cd=c1, name="미등록거래량5배1", market="KOSPI", price=10500, open_price=10000, is_tradable=True)
        symbol_store.register_symbol(sym_1)
        agg_1 = scanner.get_aggregator(c1)
        for i in range(20):
            agg_1.candles_1m.append(Candle(timestamp=now - timedelta(minutes=20-i), timeframe="1m", open=10000, high=10050, low=9950, close=10000, volume=1000, is_closed=True))
        agg_1.vwap = 10000.0
        state_1, score_1, sym_res_1 = scanner.on_market_tick(iem_cd=c1, price=10500, volume=50000, timestamp=now, execution_intensity=130.0, rank_surged=True)
        results["TEST_01"] = {
            "title": "관심종목 밖 거래량 5배 폭증 자동 탐지",
            "passed": state_1 in (SymbolState.WATCH, SymbolState.ACTIVE, SymbolState.CANDIDATE),
            "detected_state": state_1.value,
            "score": score_1
        }

        # -----------------------------------------------------------------
        # TEST 2: 3분 +2% -> 자동 Candidate
        # -----------------------------------------------------------------
        c2 = "TEST_02"
        sym_2 = SymbolInfo(iem_cd=c2, name="3분급등후보2", market="KOSDAQ", price=20500, open_price=20000, is_tradable=True)
        symbol_store.register_symbol(sym_2)
        agg_2 = scanner.get_aggregator(c2)
        for i in range(5):
            agg_2.candles_1m.append(Candle(timestamp=now - timedelta(minutes=5-i), timeframe="1m", open=20000, high=20100, low=19900, close=20000, volume=3000, is_closed=True))
        agg_2.candles_1m[-3].open = 20000
        agg_2.current_1m = Candle(timestamp=now, timeframe="1m", open=20300, high=20600, low=20300, close=20500, volume=15000)
        score_2, evts_2, pri_2, _ = EventDetector.evaluate_events(sym_2, agg_2, now)
        has_ret3m_evt = any(e.event_type.value == "RETURN_3M_2PCT" or "3분 급등" in e.description for e in evts_2)
        results["TEST_02"] = {
            "title": "3분 +2% 급등 자동 Candidate 승격",
            "passed": has_ret3m_evt or pri_2.value in ("ACTIVE PRIORITY 1", "ACTIVE PRIORITY 2", "WATCH"),
            "events": [e.description for e in evts_2]
        }

        # -----------------------------------------------------------------
        # TEST 3: 전일고가 돌파 -> 자동 Setup
        # -----------------------------------------------------------------
        c3 = "TEST_03"
        sym_3 = SymbolInfo(iem_cd=c3, name="전일고가돌파3", market="KOSPI", price=72000, prev_high=70000, open_price=69000, is_tradable=True)
        symbol_store.register_symbol(sym_3)
        agg_3 = scanner.get_aggregator(c3)
        for i in range(5):
            agg_3.candles_1m.append(Candle(timestamp=now - timedelta(minutes=5-i), timeframe="1m", open=69500, high=70000, low=69000, close=69800, volume=5000, is_closed=True))
        agg_3.current_1m = Candle(timestamp=now, timeframe="1m", open=70500, high=72500, low=70500, close=72000, volume=25000)
        agg_3.vwap = 70500.0
        insps_3, sigs_3 = setup_det.evaluate_setups(sym_3, agg_3, MarketRegime.BULL, now, {})
        pdh_ok = any(i.strategy == "PDH_BREAKOUT" and i.is_valid_setup for i in insps_3)
        results["TEST_03"] = {
            "title": "전일고가 돌파 자동 Setup 성립",
            "passed": pdh_ok,
            "valid_setups": [i.strategy for i in insps_3 if i.is_valid_setup]
        }

        # -----------------------------------------------------------------
        # TEST 4: VWAP 눌림 후 재돌파 -> Setup
        # -----------------------------------------------------------------
        c4 = "TEST_04"
        sym_4 = SymbolInfo(iem_cd=c4, name="VWAP눌림목4", market="KOSDAQ", price=20020, open_price=19500, is_tradable=True)
        symbol_store.register_symbol(sym_4)
        agg_4 = scanner.get_aggregator(c4)
        agg_4.vwap = 20000.0
        for i in range(5):
            agg_4.candles_1m.append(Candle(timestamp=now - timedelta(minutes=5-i), timeframe="1m", open=20000, high=20100, low=19980, close=20010, volume=4000, is_closed=True))
        agg_4.current_1m = Candle(timestamp=now, timeframe="1m", open=20000, high=20050, low=19990, close=20020, volume=8000)
        insps_4, sigs_4 = setup_det.evaluate_setups(sym_4, agg_4, MarketRegime.BULL, now, {})
        vwap_ok = any(i.strategy == "VWAP_PULLBACK" and i.is_valid_setup for i in insps_4)
        results["TEST_04"] = {
            "title": "VWAP 눌림목 지지 반등 Setup 성립",
            "passed": vwap_ok,
            "valid_setups": [i.strategy for i in insps_4 if i.is_valid_setup]
        }

        # -----------------------------------------------------------------
        # TEST 5: Compression Breakout -> Setup
        # -----------------------------------------------------------------
        c5 = "TEST_05"
        sym_5 = SymbolInfo(iem_cd=c5, name="압축돌파5", market="KOSPI", price=35500, open_price=35000, is_tradable=True)
        symbol_store.register_symbol(sym_5)
        agg_5 = scanner.get_aggregator(c5)
        agg_5.current_1m = Candle(timestamp=now, timeframe="1m", open=35000, high=35600, low=35000, close=35500, volume=30000)
        agg_5.vwap = 35100.0
        insps_5, sigs_5 = setup_det.evaluate_setups(sym_5, agg_5, MarketRegime.BULL, now, patterns={"is_compression_breakout": True})
        comp_ok = any(i.strategy == "COMPRESSION_BREAKOUT" and i.is_valid_setup for i in insps_5)
        results["TEST_05"] = {
            "title": "가격/변동성 압축 후 확장 돌파 Setup 성립",
            "passed": comp_ok,
            "valid_setups": [i.strategy for i in insps_5 if i.is_valid_setup]
        }

        # -----------------------------------------------------------------
        # TEST 6: Candidate -> Setup 정상 승격 및 탈락 이유 기록
        # -----------------------------------------------------------------
        c6_fail = "TEST_06_FAIL"
        sym_6_fail = SymbolInfo(iem_cd=c6_fail, name="탈락후보6", market="KOSPI", price=50000, is_tradable=True)
        symbol_store.register_symbol(sym_6_fail)
        agg_6_fail = scanner.get_aggregator(c6_fail)
        agg_6_fail.current_1m = Candle(timestamp=now, timeframe="1m", open=50000, high=50000, low=49900, close=49950, volume=100)
        insps_6, _ = setup_det.evaluate_setups(sym_6_fail, agg_6_fail, MarketRegime.BULL, now, {})
        reasons_logged = [i.block_reason for i in insps_6 if i.block_reason != ""]
        results["TEST_06"] = {
            "title": "Candidate -> Setup 평가 및 탈락 사유 투명한 기록",
            "passed": len(reasons_logged) > 0 and all(not i.is_valid_setup for i in insps_6),
            "sample_reasons": reasons_logged[:3]
        }

        # -----------------------------------------------------------------
        # TEST 7: Setup -> Entry Ready (추격매수 Overextension 필터 분리)
        # -----------------------------------------------------------------
        c7_late = "TEST_07_LATE"
        sym_7_late = SymbolInfo(iem_cd=c7_late, name="과열종목7", market="KOSPI", price=54500, open_price=50000, is_tradable=True) # +9%
        symbol_store.register_symbol(sym_7_late)
        agg_7_late = scanner.get_aggregator(c7_late)
        agg_7_late.current_1m = Candle(timestamp=now, timeframe="1m", open=54000, high=55000, low=54000, close=54500, volume=40000)
        agg_7_late.vwap = 51000.0
        insps_7_late, _ = setup_det.evaluate_setups(sym_7_late, agg_7_late, MarketRegime.BULL, now, {"is_ignition": True})
        late_item = [i for i in insps_7_late if i.strategy == "MOMENTUM"][0]
        results["TEST_07"] = {
            "title": "Setup 성립 후 Overextended 과열 진입 차단 (Setup!=EntryReady)",
            "passed": (late_item.is_valid_setup and not late_item.is_entry_ready and "OVEREXTENDED" in late_item.block_reason),
            "is_valid_setup": late_item.is_valid_setup,
            "is_entry_ready": late_item.is_entry_ready,
            "block_reason": late_item.block_reason
        }

        # -----------------------------------------------------------------
        # TEST 8: Entry Ready -> BUY Approved (Edge Engine Expected Net R >= +0.15R)
        # -----------------------------------------------------------------
        sym_8 = SymbolInfo(iem_cd="TEST_08", name="기대값승인8", market="KOSPI", price=50000, ask1_price=50100, is_tradable=True)
        sig_8_good = TradeSignal(
            strategy_id="INT_BREAKOUT", time_horizon=TimeHorizon.INTRADAY, iem_cd="TEST_08",
            name="기대값승인8", side=OrderSide.BUY, strategy_price=50000, stop_price=49000,
            target_1r=52000, score=85.0, reason="High Edge", timestamp=now
        )
        edge_good = edge_eng.calculate_edge(sym=sym_8, signal=sig_8_good)
        results["TEST_08"] = {
            "title": "Entry Ready -> BUY Approved 기대값(+0.15R) 수학적 통과",
            "passed": edge_good.is_approved and edge_good.expected_net_r >= 0.15,
            "expected_net_r": edge_good.expected_net_r,
            "is_approved": edge_good.is_approved
        }

        # -----------------------------------------------------------------
        # TEST 9: BUY Approved -> Order Sent
        # -----------------------------------------------------------------
        sig_9 = TradeSignal(
            strategy_id="INT_BREAKOUT", time_horizon=TimeHorizon.INTRADAY, iem_cd="TEST_09",
            name="주문발주9", side=OrderSide.BUY, strategy_price=50000, stop_price=49000,
            target_1r=52000, score=80.0, reason="Order Sent Test", timestamp=now,
            order_type=OrderType.MARKET
        )
        router_9 = OrderRouter(namu_client=None, circuit_breaker=_DummyCircuitBreaker())
        ord_9 = router_9.submit_order(sig_9, shares=10, order_price=50000)
        results["TEST_09"] = {
            "title": "BUY Approved -> Order Sent 주문 생성 및 발주",
            "passed": ord_9 is not None and ord_9.status in (OrderStatus.PENDING, OrderStatus.FILLED),
            "order_id": ord_9.client_order_id if ord_9 else None,
            "status": ord_9.status.value if ord_9 else None
        }

        # -----------------------------------------------------------------
        # TEST 10: Order Sent -> Fill
        # -----------------------------------------------------------------
        class MockFillClient:
            def buy_market(self, code, qty):
                return {"rt_cd": "0", "ord_no": "ORD_101010"}
        router_10 = OrderRouter(namu_client=MockFillClient(), circuit_breaker=_DummyCircuitBreaker())
        ord_10 = router_10.submit_order(sig_9, shares=10, order_price=50000)
        # Fill 처리 시뮬레이션
        router_10.on_fill(ord_10.client_order_id, filled_qty=10, fill_price=50000)
        results["TEST_10"] = {
            "title": "Order Sent -> Fill 체결 및 상태 전이",
            "passed": ord_10.status == OrderStatus.FILLED and ord_10.filled_qty == 10,
            "order_status": ord_10.status.value,
            "filled_qty": ord_10.filled_qty
        }

        # -----------------------------------------------------------------
        # TEST 11: API Timeout -> Reconciliation
        # -----------------------------------------------------------------
        router_11 = OrderRouter(namu_client=None, circuit_breaker=_DummyCircuitBreaker())
        zombie_11 = Order(
            client_order_id="ZOMBIE_TEST_11", iem_cd="TEST_11", side=OrderSide.BUY,
            order_type=OrderType.MARKET, qty=20, price=70000, strategy_id="INT_BREAKOUT",
            time_horizon=TimeHorizon.INTRADAY, status=OrderStatus.PENDING,
            created_at=now - timedelta(seconds=5), sent_at=now - timedelta(seconds=5)
        )
        router_11.pending_orders[zombie_11.client_order_id] = zombie_11
        zombies_11 = router_11.check_zombie_orders(timeout_ms=3000)
        class MockReconClient:
            def get_balance(self):
                return {"cash": 5000000, "holdings": [{"iem_cd": "TEST_11", "qty": 20, "avg_price": 70000}]}
        recon_11 = router_11.reconcile_orders(client=MockReconClient())
        results["TEST_11"] = {
            "title": "API Timeout (>3000ms) 좀비 주문 탐지 및 계좌 정합성 복구",
            "passed": len(zombies_11) >= 1 and recon_11["filled_count"] >= 1 and zombie_11.status == OrderStatus.FILLED,
            "zombies_detected": len(zombies_11),
            "reconciled": recon_11["reconciled_count"]
        }

        # -----------------------------------------------------------------
        # TEST 12: 시장 급락 -> Risk Firewall
        # -----------------------------------------------------------------
        loss_mgr = LossLimitManager()
        eval_loss = loss_mgr.evaluate_loss_limits(daily_pnl_ratio=-0.035, weekly_pnl_ratio=-0.05)
        results["TEST_12"] = {
            "title": "시장 급락 및 일일 손실 한도 초과 시 자동매매 전면 차단(Risk Firewall)",
            "passed": (not eval_loss["can_trade_intraday"] and not eval_loss["can_trade_swing"] and eval_loss["risk_multiplier"] == 0.0),
            "can_trade": eval_loss["can_trade_intraday"],
            "status": eval_loss["status"]
        }

        # -----------------------------------------------------------------
        # TEST 13: ML Model Failure -> Rule Engine fallback
        # -----------------------------------------------------------------
        class BrokenMLModel:
            def predict(self, x): raise RuntimeError("ML Service Down")
            def predict_proba(self, x): raise RuntimeError("ML Service Down")
        sym_13 = SymbolInfo(iem_cd="TEST_13", name="ML장애대체13", market="KOSPI", price=100000, is_tradable=True)
        sig_13 = TradeSignal(
            strategy_id="INT_BREAKOUT", time_horizon=TimeHorizon.INTRADAY, iem_cd="TEST_13",
            name="ML장애대체13", side=OrderSide.BUY, strategy_price=100000, stop_price=98000,
            target_1r=104000, score=80.0, reason="ML Failure Test", timestamp=now
        )
        edge_13 = edge_eng.calculate_edge(sym=sym_13, signal=sig_13, ml_model=BrokenMLModel())
        results["TEST_13"] = {
            "title": "ML 모델 장애 시 무중단 Rule Fallback 확률 적용 및 매매 지속",
            "passed": edge_13.is_approved and edge_13.p_target == 0.60,
            "p_target": edge_13.p_target,
            "is_approved": edge_13.is_approved
        }

        # -----------------------------------------------------------------
        # TEST 14: 동시에 100개 이벤트 -> Priority Queue
        # -----------------------------------------------------------------
        pq = EventPriorityQueue()
        for i in range(100):
            net_r = 0.10 + (i * 0.01) # 0.10 to 1.09
            score = 50.0 + (i * 0.4)
            pq.push(item_data=f"EVENT_{i}", expected_net_r=net_r, setup_score=score)
        top_event = pq.pop()
        results["TEST_14"] = {
            "title": "동시 100개 Event Storm 시 기대값/점수 기반 Priority Queue 정렬",
            "passed": top_event == "EVENT_99" and len(pq) == 99,
            "top_event": top_event,
            "remaining": len(pq)
        }

        # -----------------------------------------------------------------
        # TEST 15: 한 번 탈락한 종목의 재급등 -> Dynamic Rediscovery
        # -----------------------------------------------------------------
        c15 = "TEST_15"
        sym_15 = SymbolInfo(iem_cd=c15, name="재급등발굴15", market="KOSDAQ", price=10000, open_price=10000, is_tradable=True)
        symbol_store.register_symbol(sym_15)
        # 1. 탈락 (INACTIVE)
        symbol_store.transition_state(c15, SymbolState.INACTIVE, reason="이전 이벤트 종료")
        was_inactive = (symbol_store.get(c15).state == SymbolState.INACTIVE)
        # 2. 1시간 후 신규 급등 발생 (거래량 20배 폭증 + 가격 급등)
        agg_15 = scanner.get_aggregator(c15)
        state_15, score_15, _ = scanner.on_market_tick(iem_cd=c15, price=10500, volume=80000, timestamp=now + timedelta(hours=1), rank_surged=True)
        results["TEST_15"] = {
            "title": "탈락 종목 새 이벤트 발생 시 Dynamic Rediscovery 자동 재승격",
            "passed": was_inactive and state_15 in (SymbolState.WATCH, SymbolState.ACTIVE, SymbolState.CANDIDATE) and symbol_store.get(c15).state != SymbolState.INACTIVE,
            "new_state": state_15.value,
            "score": score_15
        }

        all_passed = all(t["passed"] for t in results.values())
        return {
            "all_passed": all_passed,
            "scenarios": results,
            "timestamp": now.strftime("%Y-%m-%d %H:%M:%S")
        }

