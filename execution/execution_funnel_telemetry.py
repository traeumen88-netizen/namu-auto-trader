"""
실시간 주문 퍼널 전 구간 지연시간 및 거절 텔레메트리 엔진 (Execution Funnel Telemetry v16.2)
- 신호 발생부터 체결까지 전 구간 10대 이벤트 타임스탬프 및 elapsed_ms 정밀 계측
- 단계별 통계 산출: input, success, reject, timeout, avg, p50, p95, p99, max
- 병목 후보(Bottleneck) 자동 식별 및 경보 체계 (SLA 초과, p99 급증, Reject 급증, Scan 초과 등)
- 오늘 이슈(Stale Quote, Re-fetch, Scan Time, Broker Latency) 집중 추적
- 실시간 Health Snapshot (execution_latency, order_funnel, quote_health, broker_health, fill_health, bottleneck_alerts)
"""

import time
import logging
from datetime import datetime
from typing import Dict, List, Any, Optional, Tuple
from dataclasses import dataclass, field
from collections import Counter, deque

logger = logging.getLogger("ExecutionFunnelTelemetry")


# =============================================================================
# 1. 10대 표준 퍼널 이벤트 명세
# =============================================================================
EVENT_SIGNAL_DETECTED   = "signal_detected"
EVENT_DECISION_APPROVED = "decision_approved"
EVENT_ORDER_CREATED     = "order_created"
EVENT_QUOTE_CHECK_START = "quote_check_start"
EVENT_QUOTE_REFETCH_START = "quote_refetch_start"
EVENT_QUOTE_REFETCH_END   = "quote_refetch_end"
EVENT_RISK_RECHECK      = "risk_recheck"
EVENT_CASH_CHECK        = "cash_check"
EVENT_ORDER_SUBMIT_START = "order_submit_start"
EVENT_BROKER_ACK        = "broker_ack"
EVENT_FILL_RECEIVED     = "fill_received"

FUNNEL_STAGES = [
    "signal_to_decision",
    "decision_to_order_created",
    "order_created_to_quote_check",
    "quote_check",
    "quote_refetch",
    "risk_recheck",
    "cash_check",
    "order_submit",
    "broker_ack",
    "fill_received"
]

# 단계별 SLA 기준치 (p95 임계값 ms)
DEFAULT_STAGE_SLA_MS: Dict[str, float] = {
    "scan_cycle": 3000.0,                   # 전체 스캔 주기 3초 초과 시 경보
    "signal_to_decision": 500.0,            # 셋업 탐지 -> 메타 의사결정
    "decision_to_order_created": 150.0,     # 사이징 및 주문 파라미터 구성
    "order_created_to_quote_check": 100.0,  # Quote 체크 진입
    "quote_check": 300.0,                   # 호가 신선도 검사
    "quote_refetch": 800.0,                 # On-demand REST Re-fetch
    "risk_recheck": 150.0,                  # 리스크/가격변동/스프레드/RR 검증
    "cash_check": 100.0,                    # 현금 유보/다운사이징/예약
    "order_submit": 500.0,                  # 네트워크 발주
    "broker_ack": 1500.0,                   # 브로커 주문 접수(ACK)
    "fill_received": 3000.0,                # 체결 수신
    "signal_to_router": 1000.0,             # 신호 생성 -> OrderRouter 도착
    "broker_api": 1000.0                    # 브로커 REST API 단일 호출
}

# 표본 신뢰도 등급 상수 (Sample Sufficiency Thresholds)
CONFIDENCE_DIAGNOSTIC_ONLY = "DIAGNOSTIC_ONLY"  # sample_count < 10
CONFIDENCE_PROVISIONAL     = "PROVISIONAL"      # 10 <= sample_count < 30
CONFIDENCE_RELIABLE        = "RELIABLE"         # sample_count >= 30

MIN_SAMPLES_PROVISIONAL = 10
MIN_SAMPLES_RELIABLE    = 30


@dataclass
class FunnelEventRecord:
    """단일 퍼널 이벤트 계측 레코드"""
    event_name: str
    timestamp: str
    elapsed_ms: float
    symbol: str
    strategy: str
    order_id: str
    result: str  # "SUCCESS", "REJECT", "TIMEOUT", "SKIPPED"
    reject_reason: str = ""
    quote_age_ms: float = 0.0
    metadata: Dict[str, Any] = field(default_factory=dict)


@dataclass
class OrderBottleneckRecord:
    """단일 주문의 단계별 지연시간, 기여율 및 최상위 병목 식별 레코드"""
    order_id: str
    symbol: str
    strategy: str
    timestamp: str
    stage_latencies_ms: Dict[str, float] = field(default_factory=dict)
    total_pipeline_elapsed_ms: float = 0.0
    stage_contributions_pct: Dict[str, float] = field(default_factory=dict)
    primary_bottleneck: str = "NONE"
    primary_contribution_pct: float = 0.0
    secondary_bottleneck: str = ""
    secondary_contribution_pct: float = 0.0


class StageStats:
    """개별 단계의 실시간 통계 집계기 (Sliding Window & Percentiles)"""
    def __init__(self, stage_name: str, max_samples: int = 500):
        self.stage_name = stage_name
        self.max_samples = max_samples
        self.input_count: int = 0
        self.success_count: int = 0
        self.reject_count: int = 0
        self.timeout_count: int = 0
        self.total_elapsed_ms: float = 0.0
        self.latency_samples: deque = deque(maxlen=max_samples)
        self.rejection_reasons: Counter = Counter()

    def record_entry(self):
        self.input_count += 1

    def record_success(self, latency_ms: float):
        self.success_count += 1
        lat = max(0.0, float(latency_ms))
        self.total_elapsed_ms += lat
        self.latency_samples.append(lat)

    def record_reject(self, reason: str, latency_ms: float = 0.0):
        self.reject_count += 1
        lat = max(0.0, float(latency_ms))
        if lat > 0:
            self.total_elapsed_ms += lat
            self.latency_samples.append(lat)
        clean_reason = reason.split(":")[0].split("(")[0].strip()
        self.rejection_reasons[clean_reason] += 1

    def record_timeout(self, latency_ms: float = 0.0):
        self.timeout_count += 1
        lat = max(0.0, float(latency_ms))
        if lat > 0:
            self.total_elapsed_ms += lat
            self.latency_samples.append(lat)

    def compute_percentiles(self) -> Dict[str, float]:
        if not self.latency_samples:
            return {
                "avg": 0.0,
                "p50": 0.0,
                "p95": 0.0,
                "p99": 0.0,
                "max": 0.0
            }
        s = sorted(self.latency_samples)
        n = len(s)
        avg_val = sum(s) / float(n)
        p50_val = s[int(n * 0.50)]
        p95_val = s[min(int(n * 0.95), n - 1)]
        p99_val = s[min(int(n * 0.99), n - 1)]
        max_val = s[-1]
        return {
            "avg": round(avg_val, 2),
            "p50": round(p50_val, 2),
            "p95": round(p95_val, 2),
            "p99": round(p99_val, 2),
            "max": round(max_val, 2)
        }

    def get_summary(self) -> Dict[str, Any]:
        p = self.compute_percentiles()
        reject_rate = (self.reject_count / max(1, self.input_count)) * 100.0
        success_rate = (self.success_count / max(1, self.input_count)) * 100.0
        return {
            "input_count": self.input_count,
            "success_count": self.success_count,
            "reject_count": self.reject_count,
            "timeout_count": self.timeout_count,
            "reject_rate_pct": round(reject_rate, 2),
            "success_rate_pct": round(success_rate, 2),
            "latencies_ms": p,
            "top_rejections": dict(self.rejection_reasons.most_common(3))
        }


class ExecutionFunnelTelemetry:
    """
    주문 퍼널 전 구간 계측, 단계별 통계 산출, 병목 자동 식별 관제 엔진
    """
    def __init__(self, sla_thresholds: Optional[Dict[str, float]] = None):
        self.sla_thresholds = sla_thresholds or DEFAULT_STAGE_SLA_MS
        
        # 1. 단계별 통계 집계기
        self.stages: Dict[str, StageStats] = {
            stg: StageStats(stg) for stg in FUNNEL_STAGES
        }
        # 특별 측정 항목 통계기
        self.stages["scan_cycle"] = StageStats("scan_cycle")
        self.stages["signal_to_router"] = StageStats("signal_to_router")
        self.stages["broker_api"] = StageStats("broker_api")

        # 2. 최근 퍼널 이벤트 레코드 (최대 1,000건 저장)
        self.event_records: deque = deque(maxlen=1000)

        # 3. 신호/주문별 최근 이벤트 타임스탬프 트래커 (order_id -> {event_name: time.perf_counter()})
        self._active_timings: Dict[str, Dict[str, float]] = {}

        # 4. 오늘 문제(Stale Quote, Re-fetch, Scan Time, Execution) 집중 추적기
        self.quote_ages_ms: deque = deque(maxlen=500)
        self.scan_cycle_times_sec: deque = deque(maxlen=200)
        self.stale_quote_detected_count: int = 0
        self.refetch_triggered_count: int = 0
        self.refetch_success_count: int = 0
        self.refetch_failed_count: int = 0
        self.refetch_to_order_sent_count: int = 0

        # 브로커 & 체결 헬스 카운터
        self.broker_api_calls_count: int = 0
        self.broker_api_errors_count: int = 0
        self.orders_sent_count: int = 0
        self.orders_filled_count: int = 0
        self.orders_partial_filled_count: int = 0
        self.orders_unfilled_count: int = 0

        # 5. 병목 알림 목록 (최근 50건 보관)
        self.active_bottleneck_alerts: List[Dict[str, Any]] = []

        # 6. 주문별 병목 기여율 분석 저장소 (최근 500건 보관)
        self.order_bottleneck_records: deque = deque(maxlen=500)
        self._order_stage_timings: Dict[str, Dict[str, float]] = {}
        self._order_metadata: Dict[str, Dict[str, str]] = {}

    # =========================================================================
    # 퍼널 이벤트 계측 API
    # =========================================================================
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
    ) -> FunnelEventRecord:
        """
        퍼널의 10대 핵심 이벤트를 정밀 타임스탬프 단위로 계측하고 기록
        """
        now_dt = now_dt or datetime.now()
        t_now = time.perf_counter()
        
        # 이전 이벤트로부터 elapsed_ms 자동 계산 (명시적 elapsed_ms가 없을 경우)
        order_key = order_id or f"{strategy}_{symbol}"
        if order_key not in self._active_timings:
            self._active_timings[order_key] = {}
        
        prev_times = self._active_timings[order_key]
        if elapsed_ms is None:
            # 직전 이벤트 타임스탬프 찾기
            if prev_times:
                last_t = max(prev_times.values())
                calc_elapsed = (t_now - last_t) * 1000.0
            else:
                calc_elapsed = 0.0
            elapsed_ms = max(0.0, calc_elapsed)

        prev_times[event_name] = t_now

        # 레코드 생성
        rec = FunnelEventRecord(
            event_name=event_name,
            timestamp=now_dt.strftime("%Y-%m-%d %H:%M:%S.%f")[:-3],
            elapsed_ms=round(elapsed_ms, 2),
            symbol=symbol,
            strategy=strategy,
            order_id=order_id,
            result=result,
            reject_reason=reject_reason,
            quote_age_ms=round(quote_age_ms, 1),
            metadata=metadata or {}
        )
        self.event_records.append(rec)

        # Quote Age 표본 등록
        if quote_age_ms > 0:
            self.quote_ages_ms.append(quote_age_ms)

        # 연결된 퍼널 단계 통계 업데이트
        self._map_event_to_stage_stats(event_name, result, elapsed_ms, reject_reason)

        # 주문별 단계별 지연시간 수집 및 종결 처리
        if order_id:
            if order_id not in self._order_stage_timings:
                self._order_stage_timings[order_id] = {}
            if order_id not in self._order_metadata:
                self._order_metadata[order_id] = {"symbol": symbol, "strategy": strategy}

            stage_map_order = {
                EVENT_SIGNAL_DETECTED: "signal_detected",
                EVENT_DECISION_APPROVED: "decision_approved",
                EVENT_QUOTE_CHECK_START: "quote_check",
                EVENT_QUOTE_REFETCH_END: "quote_refetch",
                EVENT_RISK_RECHECK: "risk_recheck",
                EVENT_CASH_CHECK: "cash_check",
                EVENT_ORDER_SUBMIT_START: "broker_submit",
                EVENT_BROKER_ACK: "broker_ack",
                EVENT_FILL_RECEIVED: "fill_received"
            }
            mapped_stage = stage_map_order.get(event_name)
            if mapped_stage and elapsed_ms is not None:
                self._order_stage_timings[order_id][mapped_stage] = max(0.0, float(elapsed_ms))

            if event_name == EVENT_FILL_RECEIVED:
                self.finalize_order_bottleneck(order_id, symbol=symbol, strategy=strategy, now_dt=now_dt)

        return rec

    def _map_event_to_stage_stats(self, event_name: str, result: str, elapsed_ms: float, reject_reason: str):
        """이벤트를 해당 퍼널 단계 통계로 자동 매핑"""
        stage_map = {
            EVENT_DECISION_APPROVED: "signal_to_decision",
            EVENT_ORDER_CREATED: "decision_to_order_created",
            EVENT_QUOTE_CHECK_START: "order_created_to_quote_check",
            EVENT_QUOTE_REFETCH_END: "quote_refetch",
            EVENT_RISK_RECHECK: "risk_recheck",
            EVENT_CASH_CHECK: "cash_check",
            EVENT_ORDER_SUBMIT_START: "order_submit",
            EVENT_BROKER_ACK: "broker_ack",
            EVENT_FILL_RECEIVED: "fill_received"
        }
        stg_name = stage_map.get(event_name)
        if stg_name and stg_name in self.stages:
            stg = self.stages[stg_name]
            stg.record_entry()
            if result == "SUCCESS":
                stg.record_success(elapsed_ms)
            elif result == "TIMEOUT":
                stg.record_timeout(elapsed_ms)
            else:
                stg.record_reject(reject_reason or "REJECTED", elapsed_ms)

    # =========================================================================
    # 특별 계측 API (오늘 문제 집중 추적)
    # =========================================================================
    def record_scan_cycle(self, duration_sec: float):
        """전체 종목 스캔 시간 기록"""
        self.scan_cycle_times_sec.append(duration_sec)
        dur_ms = duration_sec * 1000.0
        self.stages["scan_cycle"].record_entry()
        self.stages["scan_cycle"].record_success(dur_ms)

    def record_signal_to_router_latency(self, latency_ms: float, order_id: Optional[str] = None):
        """신호 발생 -> order_router 도착 시간 기록"""
        lat = max(0.0, float(latency_ms))
        self.stages["signal_to_router"].record_entry()
        self.stages["signal_to_router"].record_success(lat)
        if order_id:
            if order_id not in self._order_stage_timings:
                self._order_stage_timings[order_id] = {}
            self._order_stage_timings[order_id]["signal_to_router"] = lat

    def record_quote_refetch_attempt(self, is_success: bool, is_stale_before: bool = True, latency_ms: float = 0.0):
        """Quote Stale 감지 및 Re-fetch 시도/결과 기록"""
        if is_stale_before:
            self.stale_quote_detected_count += 1
        self.refetch_triggered_count += 1
        if is_success:
            self.refetch_success_count += 1
            self.stages["quote_refetch"].record_entry()
            self.stages["quote_refetch"].record_success(latency_ms)
        else:
            self.refetch_failed_count += 1
            self.stages["quote_refetch"].record_entry()
            self.stages["quote_refetch"].record_reject("REFETCH_STALE_OR_FAILED", latency_ms)

    def record_refetched_order_sent(self):
        """Re-fetch 성공 후 실제 주문 전송 성공 기록"""
        self.refetch_to_order_sent_count += 1

    def record_broker_api_call(self, latency_ms: float, is_success: bool = True, is_timeout: bool = False):
        """브로커 REST API 호출 지연시간 기록"""
        self.broker_api_calls_count += 1
        stg = self.stages["broker_api"]
        stg.record_entry()
        if is_timeout:
            self.broker_api_errors_count += 1
            stg.record_timeout(latency_ms)
        elif is_success:
            stg.record_success(latency_ms)
        else:
            self.broker_api_errors_count += 1
            stg.record_reject("API_ERROR", latency_ms)

    def record_order_lifecycle(self, is_sent: bool = True, is_filled: bool = False, is_partial: bool = False):
        """주문 발주, 체결, 부분체결 카운트 기록"""
        if is_sent:
            self.orders_sent_count += 1
        if is_filled:
            self.orders_filled_count += 1
        elif is_partial:
            self.orders_partial_filled_count += 1
        elif is_sent and not is_filled:
            self.orders_unfilled_count += 1

    # =========================================================================
    # 병목 기여도 분석 엔진 (Bottleneck Contribution Analysis)
    # =========================================================================
    def record_order_pipeline_latency(
        self,
        order_id: str,
        symbol: str,
        strategy: str,
        stage_latencies_ms: Dict[str, float],
        now_dt: Optional[datetime] = None
    ) -> OrderBottleneckRecord:
        """
        단일 주문의 전체 단계별 지연시간(ms)을 등록하고,
        지연 기여율(contribution_pct), 최상위 병목(primary_bottleneck),
        차상위 병목(secondary_bottleneck)을 산출하여 기록
        """
        now_dt = now_dt or datetime.now()
        clean_latencies = {k: max(0.0, float(v)) for k, v in stage_latencies_ms.items()}
        total_pipeline_elapsed_ms = sum(clean_latencies.values())

        stage_contributions_pct: Dict[str, float] = {}
        if total_pipeline_elapsed_ms > 0:
            for stg, lat in clean_latencies.items():
                stage_contributions_pct[stg] = round((lat / total_pipeline_elapsed_ms) * 100.0, 2)
        else:
            for stg in clean_latencies:
                stage_contributions_pct[stg] = 0.0

        # Sort stages by latency descending to determine primary & secondary bottlenecks
        sorted_stages = sorted(clean_latencies.items(), key=lambda x: x[1], reverse=True)
        primary_b = sorted_stages[0][0].upper() if sorted_stages and sorted_stages[0][1] > 0 else "NONE"
        primary_pct = stage_contributions_pct.get(sorted_stages[0][0], 0.0) if sorted_stages else 0.0

        secondary_b = ""
        secondary_pct = 0.0
        if len(sorted_stages) > 1 and sorted_stages[1][1] > 0:
            secondary_b = sorted_stages[1][0].upper()
            secondary_pct = stage_contributions_pct.get(sorted_stages[1][0], 0.0)

        rec = OrderBottleneckRecord(
            order_id=order_id,
            symbol=symbol,
            strategy=strategy,
            timestamp=now_dt.strftime("%Y-%m-%d %H:%M:%S.%f")[:-3],
            stage_latencies_ms=clean_latencies,
            total_pipeline_elapsed_ms=round(total_pipeline_elapsed_ms, 2),
            stage_contributions_pct=stage_contributions_pct,
            primary_bottleneck=primary_b,
            primary_contribution_pct=round(primary_pct, 1),
            secondary_bottleneck=secondary_b,
            secondary_contribution_pct=round(secondary_pct, 1)
        )
        self.order_bottleneck_records.append(rec)
        return rec

    def finalize_order_bottleneck(
        self,
        order_id: str,
        symbol: str = "",
        strategy: str = "",
        now_dt: Optional[datetime] = None
    ) -> Optional[OrderBottleneckRecord]:
        """주문 체결/종료 시점에 수집된 단계별 지연시간을 취합하여 병목 기여도 레코드로 확정"""
        timings = self._order_stage_timings.get(order_id, {})
        if not timings:
            return None
        meta = self._order_metadata.get(order_id, {})
        sym = symbol or meta.get("symbol", "")
        strat = strategy or meta.get("strategy", "")
        return self.record_order_pipeline_latency(
            order_id=order_id,
            symbol=sym,
            strategy=strat,
            stage_latencies_ms=timings,
            now_dt=now_dt
        )

    def get_bottleneck_contribution_analysis(self, recent_n: Optional[int] = None) -> Dict[str, Any]:
        """
        당일 누적 병목 기여도 분석 집계
        - 최근 N개 주문 또는 당일 전체 주문 대상
        - sample_count, overall_status (DIAGNOSTIC_ONLY, PROVISIONAL, RELIABLE)
        - stage별: sample_count, p50, p95, p99, avg, total_elapsed_ms, contribution_pct
        - top_bottleneck 식별
        """
        records = list(self.order_bottleneck_records)
        if recent_n and recent_n > 0:
            records = records[-recent_n:]

        order_count = len(records)
        if order_count > 0:
            sample_count = order_count
        else:
            sample_count = max((len(stg.latency_samples) for stg in self.stages.values()), default=0)

        # Sample sufficiency status determination (상수 기준)
        if sample_count < MIN_SAMPLES_PROVISIONAL:
            overall_status = CONFIDENCE_DIAGNOSTIC_ONLY
        elif sample_count < MIN_SAMPLES_RELIABLE:
            overall_status = CONFIDENCE_PROVISIONAL
        else:
            overall_status = CONFIDENCE_RELIABLE

        stage_samples: Dict[str, List[float]] = {}

        if records:
            for rec in records:
                for stg, lat in rec.stage_latencies_ms.items():
                    if stg not in stage_samples:
                        stage_samples[stg] = []
                    stage_samples[stg].append(lat)

            # scan_cycle이 개별 주문에 직접 포함되지 않았더라도 관제 통계에 있으면 함께 비교
            if "scan" not in stage_samples and "scan_cycle" not in stage_samples and self.stages["scan_cycle"].latency_samples:
                stage_samples["scan"] = list(self.stages["scan_cycle"].latency_samples)
        else:
            for stg_name, stg_obj in self.stages.items():
                if stg_obj.latency_samples:
                    alias = "scan" if stg_name == "scan_cycle" else stg_name
                    stage_samples[alias] = list(stg_obj.latency_samples)

        stage_totals: Dict[str, float] = {
            stg: sum(lats) for stg, lats in stage_samples.items()
        }
        grand_total = sum(stage_totals.values())

        stages_dict: Dict[str, Dict[str, Any]] = {}
        ranked_list = []

        for stg, lats in stage_samples.items():
            if not lats:
                continue
            s_sorted = sorted(lats)
            n = len(s_sorted)
            avg_val = sum(s_sorted) / float(n)
            p50_val = s_sorted[int(n * 0.50)]
            p95_val = s_sorted[min(int(n * 0.95), n - 1)]
            p99_val = s_sorted[min(int(n * 0.99), n - 1)]
            tot_elapsed = stage_totals.get(stg, 0.0)

            contrib_pct = round((tot_elapsed / grand_total) * 100.0, 1) if grand_total > 0 else 0.0

            stages_dict[stg] = {
                "contribution_pct": contrib_pct,
                "p50": round(p50_val, 1),
                "p95": round(p95_val, 1),
                "p99": round(p99_val, 1),
                "avg": round(avg_val, 1),
                "total_elapsed_ms": round(tot_elapsed, 1),
                "sample_count": n
            }
            ranked_list.append({
                "stage": stg.upper(),
                "stage_key": stg,
                "contribution_pct": contrib_pct,
                "p95_ms": round(p95_val, 1),
                "avg_ms": round(avg_val, 1),
                "sample_count": n
            })

        ranked_list.sort(key=lambda x: x["contribution_pct"], reverse=True)
        top_bottleneck = ranked_list[0]["stage"] if ranked_list and ranked_list[0]["contribution_pct"] > 0 else "NONE"
        top_contrib_pct = ranked_list[0]["contribution_pct"] if ranked_list else 0.0

        return {
            "overall_status": overall_status,
            "sample_count": sample_count,
            "top_bottleneck": top_bottleneck,
            "top_contribution_pct": top_contrib_pct,
            "stages": stages_dict,
            "ranked_stages": ranked_list,
            "recent_orders": [
                {
                    "order_id": r.order_id,
                    "symbol": r.symbol,
                    "strategy": r.strategy,
                    "total_pipeline_elapsed_ms": r.total_pipeline_elapsed_ms,
                    "primary_bottleneck": r.primary_bottleneck,
                    "primary_contribution_pct": r.primary_contribution_pct,
                    "secondary_bottleneck": r.secondary_bottleneck,
                    "secondary_contribution_pct": r.secondary_contribution_pct
                }
                for r in list(self.order_bottleneck_records)[-10:]
            ]
        }

    def format_bottleneck_contribution_summary(self) -> str:
        """터미널 대시보드 표출용 상위 병목 및 표본 신뢰도 요약 포맷"""
        analysis = self.get_bottleneck_contribution_analysis()
        lines = ["[TOP BOTTLENECKS]"]
        ranked = analysis.get("ranked_stages", [])
        if ranked:
            for idx, r in enumerate(ranked[:5], start=1):
                stg_display = r["stage"]
                pct = r["contribution_pct"]
                p95 = r["p95_ms"]
                lines.append(f"{idx}. {stg_display:<16s} {pct:>5.1f}%  p95={p95:.0f}ms")
        else:
            lines.append("  (기록된 파이프라인 지연 표본 없음)")

        lines.append("")
        lines.append("[DATA SUFFICIENCY]")
        lines.append(f"Samples: {analysis['sample_count']}")
        lines.append(f"Diagnostic Confidence: {analysis['overall_status']}")
        return "\n".join(lines)

    # =========================================================================
    # 병목 후보 자동 식별 엔진 (Bottleneck Detector)
    # =========================================================================
    def detect_bottlenecks(self) -> List[Dict[str, Any]]:
        """
        8대 핵심 조건을 기준으로 현재 파이프라인 병목 후보 자동 식별
        1) p95 latency > 기준치 초과
        2) p99 latency 급증 (p99 > 2*p95 or p99 > 3000ms)
        3) 특정 단계 reject rate 급증 (> 40%)
        4) timeout 발생
        5) 동일 reject_reason 반복 증가 (>= 3회)
        6) scan cycle time이 quote freshness threshold(3.0s) 지속 초과
        7) broker API latency 급증 (> 1000ms)
        8) 부분체결/미체결률 급증 (> 30%)
        """
        alerts = []
        now_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

        # 1. 단계별 Latency & Reject 검사
        for stg_name, stg in self.stages.items():
            if stg.input_count < 1:
                continue

            sla_limit = self.sla_thresholds.get(stg_name, 1000.0)
            pcts = stg.compute_percentiles()
            p95 = pcts["p95"]
            p99 = pcts["p99"]

            # 1-1. p95 latency 기준치 초과
            if p95 > sla_limit:
                alerts.append({
                    "category": "SLA_BREACH",
                    "stage": stg_name,
                    "severity": "CRITICAL" if p95 > (sla_limit * 1.5) else "WARNING",
                    "message": f"[{stg_name}] p95 지연시간({p95:.1f}ms)이 SLA 기준치({sla_limit:.1f}ms)를 초과했습니다.",
                    "metric_value": p95,
                    "threshold": sla_limit,
                    "timestamp": now_str
                })

            # 1-2. p99 latency 급증 (Tail Latency Spike)
            if p99 > (p95 * 2.0) and p99 > 300.0:
                alerts.append({
                    "category": "TAIL_LATENCY_SPIKE",
                    "stage": stg_name,
                    "severity": "WARNING",
                    "message": f"[{stg_name}] p99 꼬리 지연({p99:.1f}ms)이 p95({p95:.1f}ms) 대비 2배 이상 급증했습니다.",
                    "metric_value": p99,
                    "threshold": round(p95 * 2.0, 1),
                    "timestamp": now_str
                })

            # 1-3. 특정 단계 Reject Rate 급증
            if stg.input_count >= 3:
                rej_rate = (stg.reject_count / stg.input_count) * 100.0
                if rej_rate >= 40.0:
                    top_r = stg.rejection_reasons.most_common(1)
                    top_str = f" (주요사유: {top_r[0][0]})" if top_r else ""
                    alerts.append({
                        "category": "REJECT_RATE_SPIKE",
                        "stage": stg_name,
                        "severity": "CRITICAL" if rej_rate >= 70.0 else "WARNING",
                        "message": f"[{stg_name}] 거절률이 {rej_rate:.1f}%로 급증했습니다{top_str}.",
                        "metric_value": round(rej_rate, 1),
                        "threshold": 40.0,
                        "timestamp": now_str
                    })

            # 1-4. Timeout 발생
            if stg.timeout_count > 0:
                alerts.append({
                    "category": "TIMEOUT_DETECTED",
                    "stage": stg_name,
                    "severity": "CRITICAL",
                    "message": f"[{stg_name}] 타임아웃이 {stg.timeout_count}건 발생했습니다.",
                    "metric_value": stg.timeout_count,
                    "threshold": 0,
                    "timestamp": now_str
                })

            # 1-5. 동일 reject_reason 반복 증가
            for reason, cnt in stg.rejection_reasons.items():
                if cnt >= 3:
                    alerts.append({
                        "category": "REPEATED_REJECTION",
                        "stage": stg_name,
                        "severity": "WARNING",
                        "message": f"[{stg_name}] 동일 거절 사유 반복: '{reason}' ({cnt}회 누적)",
                        "metric_value": cnt,
                        "threshold": 3,
                        "timestamp": now_str
                    })

        # 2. 전체 종목 스캔 시간 및 호가 신선도(3.0s) 기준 초과 검사
        if self.scan_cycle_times_sec:
            recent_scans = list(self.scan_cycle_times_sec)[-10:]
            avg_scan = sum(recent_scans) / len(recent_scans)
            if avg_scan > 3.0:
                alerts.append({
                    "category": "SCAN_CYCLE_EXCEEDS_FRESHNESS",
                    "stage": "scan_cycle",
                    "severity": "CRITICAL",
                    "message": f"[전체 시장 스캔] 평균 스캔 시간({avg_scan:.2f}초)이 호가 신선도 한도(3.0초)를 초과하여 Stale Quote를 유발 중입니다.",
                    "metric_value": round(avg_scan, 2),
                    "threshold": 3.0,
                    "timestamp": now_str
                })

        # 3. 브로커 API 지연시간 급증 검사
        broker_pcts = self.stages["broker_api"].compute_percentiles()
        if broker_pcts["p95"] > 1000.0:
            alerts.append({
                "category": "BROKER_API_LATENCY_SPIKE",
                "stage": "broker_api",
                "severity": "CRITICAL",
                "message": f"[브로커 API] p95 API 응답 지연({broker_pcts['p95']:.1f}ms)이 1,000ms를 초과했습니다.",
                "metric_value": broker_pcts["p95"],
                "threshold": 1000.0,
                "timestamp": now_str
            })

        # 4. 부분체결/미체결률 급증 검사
        if self.orders_sent_count >= 2:
            unfilled = self.orders_sent_count - self.orders_filled_count
            unfilled_rate = (unfilled / self.orders_sent_count) * 100.0
            if unfilled_rate >= 30.0:
                alerts.append({
                    "category": "UNFILLED_ORDER_SPIKE",
                    "stage": "fill_health",
                    "severity": "WARNING",
                    "message": f"[체결 품질] 미체결률이 {unfilled_rate:.1f}% ({unfilled}/{self.orders_sent_count}건)에 도달했습니다.",
                    "metric_value": round(unfilled_rate, 1),
                    "threshold": 30.0,
                    "timestamp": now_str
                })

        # 5. 지연시간 기여도 기반 병목 감지 (HIGH_CONTRIBUTION_BOTTLENECK & 표본 충분성 검증)
        contrib_analysis = self.get_bottleneck_contribution_analysis()
        sample_count = contrib_analysis.get("sample_count", 0)
        ranked_stages = contrib_analysis.get("ranked_stages", [])

        if ranked_stages:
            top_stage_info = ranked_stages[0]
            top_stg_name = top_stage_info.get("stage_key", "")
            top_stg_display = top_stage_info.get("stage", "")
            top_pct = top_stage_info.get("contribution_pct", 0.0)
            top_p95 = top_stage_info.get("p95_ms", 0.0)

            stage_sla_key_map = {
                "scan": "scan_cycle",
                "decision_approved": "signal_to_decision",
                "broker_submit": "order_submit",
                "quote_check": "quote_check",
                "quote_refetch": "quote_refetch",
                "risk_recheck": "risk_recheck",
                "cash_check": "cash_check",
                "broker_ack": "broker_ack",
                "fill_received": "fill_received",
                "signal_to_router": "signal_to_router",
            }
            sla_key = stage_sla_key_map.get(top_stg_name, top_stg_name)
            sla_limit = self.sla_thresholds.get(sla_key, 1000.0)

            if top_pct >= 50.0:
                if sample_count < MIN_SAMPLES_PROVISIONAL:
                    alerts.append({
                        "category": "HIGH_CONTRIBUTION_BOTTLENECK",
                        "stage": top_stg_name,
                        "severity": "INSUFFICIENT_SAMPLE",
                        "status": "INSUFFICIENT_SAMPLE",
                        "message": f"[{top_stg_display}] 기여율 {top_pct:.1f}%가 감지되었으나 표본수 부족({sample_count}/{MIN_SAMPLES_PROVISIONAL}건)으로 병목을 확정하지 않고 INSUFFICIENT_SAMPLE로 플래그했습니다 (진단신뢰도: {CONFIDENCE_DIAGNOSTIC_ONLY}).",
                        "metric_value": sample_count,
                        "threshold": MIN_SAMPLES_PROVISIONAL,
                        "timestamp": now_str
                    })
                elif top_pct >= 70.0 and top_p95 > sla_limit:
                    alerts.append({
                        "category": "HIGH_CONTRIBUTION_BOTTLENECK",
                        "stage": top_stg_name,
                        "severity": "CRITICAL",
                        "status": "CONFIRMED",
                        "message": f"[{top_stg_display}] 파이프라인 지연 기여율 {top_pct:.1f}% 및 p95({top_p95:.1f}ms > SLA {sla_limit:.1f}ms) 동시 초과로 심각한 핵심 병목으로 확정되었습니다.",
                        "metric_value": top_pct,
                        "threshold": 70.0,
                        "timestamp": now_str
                    })
                else:
                    alerts.append({
                        "category": "HIGH_CONTRIBUTION_BOTTLENECK",
                        "stage": top_stg_name,
                        "severity": "WARNING",
                        "status": "CONFIRMED",
                        "message": f"[{top_stg_display}] 파이프라인 지연 기여율이 {top_pct:.1f}%(기준 50% 이상)로 주요 병목 후보로 식별되었습니다.",
                        "metric_value": top_pct,
                        "threshold": 50.0,
                        "timestamp": now_str
                    })

        self.active_bottleneck_alerts = alerts
        return alerts

    # =========================================================================
    # 6대 실시간 Health Snapshot 생성
    # =========================================================================
    def get_health_snapshot(self) -> Dict[str, Any]:
        """
        live_telemetry.json 전용 6대 건강도 스냅샷 생성
        1. execution_latency
        2. order_funnel
        3. quote_health
        4. broker_health
        5. fill_health
        6. bottleneck_alerts
        """
        bottleneck_alerts = self.detect_bottlenecks()

        # 1. execution_latency
        exec_latency = {}
        for stg_name, stg in self.stages.items():
            exec_latency[stg_name] = stg.compute_percentiles()

        # 2. order_funnel
        order_funnel = {}
        for stg_name, stg in self.stages.items():
            order_funnel[stg_name] = stg.get_summary()

        # 3. quote_health (오늘 집중 항목 포함)
        quote_age_pct = self._compute_deque_percentiles(self.quote_ages_ms)
        refetch_trigger_rate = (
            (self.refetch_triggered_count / max(1, self.stages["order_created_to_quote_check"].input_count)) * 100.0
        )
        refetch_success_rate = (
            (self.refetch_success_count / max(1, self.refetch_triggered_count)) * 100.0
        )
        refetch_to_send_rate = (
            (self.refetch_to_order_sent_count / max(1, self.refetch_triggered_count)) * 100.0
        )
        quote_health = {
            "quote_age_ms": quote_age_pct,
            "stale_quotes_detected": self.stale_quote_detected_count,
            "refetch_triggered_count": self.refetch_triggered_count,
            "refetch_success_count": self.refetch_success_count,
            "refetch_failed_count": self.refetch_failed_count,
            "stale_refetch_trigger_rate_pct": round(refetch_trigger_rate, 2),
            "refetch_success_rate_pct": round(refetch_success_rate, 2),
            "refetch_to_order_send_rate_pct": round(refetch_to_send_rate, 2),
            "refetch_latencies_ms": self.stages["quote_refetch"].compute_percentiles()
        }

        # 4. broker_health
        broker_health = {
            "submit_to_ack_latencies_ms": self.stages["broker_ack"].compute_percentiles(),
            "broker_api_latencies_ms": self.stages["broker_api"].compute_percentiles(),
            "total_api_calls": self.broker_api_calls_count,
            "api_errors_or_timeouts": self.broker_api_errors_count,
            "broker_ack_timeouts": self.stages["broker_ack"].timeout_count
        }

        # 5. fill_health
        ack_to_fill_pct = self.stages["fill_received"].compute_percentiles()
        fill_rate = (self.orders_filled_count / max(1, self.orders_sent_count)) * 100.0
        fill_health = {
            "ack_to_fill_latencies_ms": ack_to_fill_pct,
            "orders_sent": self.orders_sent_count,
            "orders_filled": self.orders_filled_count,
            "orders_partial_filled": self.orders_partial_filled_count,
            "orders_unfilled": self.orders_unfilled_count,
            "fill_rate_pct": round(fill_rate, 2)
        }

        # 6. today_special_metrics (사용자 요청 핵심 지표 요약)
        scan_times_ms = deque([s * 1000.0 for s in self.scan_cycle_times_sec], maxlen=200)
        today_metrics = {
            "scan_cycle_time_ms": self._compute_deque_percentiles(scan_times_ms),
            "signal_to_router_latency_ms": self.stages["signal_to_router"].compute_percentiles(),
            "quote_age_ms": quote_age_pct,
            "stale_to_refetch_trigger_rate_pct": round(refetch_trigger_rate, 2),
            "refetch_success_rate_pct": round(refetch_success_rate, 2),
            "refetch_to_order_send_rate_pct": round(refetch_to_send_rate, 2),
            "order_submit_to_broker_ack_ms": self.stages["broker_ack"].compute_percentiles(),
            "broker_ack_to_fill_ms": ack_to_fill_pct
        }

        return {
            "execution_latency": exec_latency,
            "order_funnel": order_funnel,
            "quote_health": quote_health,
            "broker_health": broker_health,
            "fill_health": fill_health,
            "today_special_metrics": today_metrics,
            "bottleneck_contribution": self.get_bottleneck_contribution_analysis(),
            "bottleneck_alerts": bottleneck_alerts,
            "recent_events": [
                {
                    "event": r.event_name,
                    "timestamp": r.timestamp,
                    "elapsed_ms": r.elapsed_ms,
                    "symbol": r.symbol,
                    "strategy": r.strategy,
                    "order_id": r.order_id,
                    "result": r.result,
                    "reject_reason": r.reject_reason,
                    "quote_age_ms": r.quote_age_ms
                }
                for r in list(self.event_records)[-20:]
            ]
        }

    @staticmethod
    def _compute_deque_percentiles(d: deque) -> Dict[str, float]:
        if not d:
            return {"avg": 0.0, "p50": 0.0, "p95": 0.0, "p99": 0.0, "max": 0.0}
        s = sorted(d)
        n = len(s)
        return {
            "avg": round(sum(s) / float(n), 2),
            "p50": round(s[int(n * 0.50)], 2),
            "p95": round(s[min(int(n * 0.95), n - 1)], 2),
            "p99": round(s[min(int(n * 0.99), n - 1)], 2),
            "max": round(s[-1], 2)
        }
