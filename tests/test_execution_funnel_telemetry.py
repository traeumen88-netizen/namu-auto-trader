"""
tests/test_execution_funnel_telemetry.py
실행 파이프라인 전 구간 밀리초(ms) 지연시간, 거절 텔레메트리 및 병목 자동 탐지 검증 테스트
"""

import unittest
from datetime import datetime, timedelta
import json
import os
import time

from execution.execution_funnel_telemetry import (
    ExecutionFunnelTelemetry,
    StageStats,
    FunnelEventRecord,
    OrderBottleneckRecord,
    DEFAULT_STAGE_SLA_MS,
    CONFIDENCE_DIAGNOSTIC_ONLY,
    CONFIDENCE_PROVISIONAL,
    CONFIDENCE_RELIABLE,
    MIN_SAMPLES_PROVISIONAL,
    MIN_SAMPLES_RELIABLE,
    EVENT_SIGNAL_DETECTED,
    EVENT_DECISION_APPROVED,
    EVENT_ORDER_CREATED,
    EVENT_QUOTE_CHECK_START,
    EVENT_QUOTE_REFETCH_START,
    EVENT_QUOTE_REFETCH_END,
    EVENT_RISK_RECHECK,
    EVENT_CASH_CHECK,
    EVENT_ORDER_SUBMIT_START,
    EVENT_BROKER_ACK,
    EVENT_FILL_RECEIVED,
)
from execution.order_router import OrderRouter
from execution.order_quote_manager import OrderQuoteSnapshot
from core.models import TradeSignal, TimeHorizon, OrderSide, OrderType, OrderStatus


class MockBrokerClient:
    """테스트용 가상 브로커 클라이언트"""
    def __init__(self, dry_run=True, delay_sec=0.0):
        self.dry_run = dry_run
        self.delay_sec = delay_sec

    def get_current_price(self, symbol):
        if self.delay_sec > 0:
            time.sleep(self.delay_sec)
        return {
            "is_valid": True,
            "price": 10000,
            "bid": 9990,
            "ask": 10000,
            "volume": 500000,
            "timestamp": datetime.now()
        }

    def buy_market(self, symbol, qty):
        if self.delay_sec > 0:
            time.sleep(self.delay_sec)
        return {"Output_0": {"mkt_orr_no": "MKT_ACK_123"}}


class TestExecutionFunnelTelemetry(unittest.TestCase):
    def setUp(self):
        self.telemetry = ExecutionFunnelTelemetry()

    def test_milestone_event_recording(self):
        """10대 이정표 이벤트가 빠짐없이 기록되고 모든 필수 필드를 보유하는지 검증"""
        now = datetime.now()
        events = [
            (EVENT_SIGNAL_DETECTED, "SUCCESS", "", 0.0, 0.0),
            (EVENT_DECISION_APPROVED, "SUCCESS", "", 1.2, 0.0),
            (EVENT_ORDER_CREATED, "SUCCESS", "", 0.5, 0.0),
            (EVENT_QUOTE_CHECK_START, "SUCCESS", "", 0.2, 500.0),
            (EVENT_QUOTE_REFETCH_START, "SUCCESS", "", 0.3, 3500.0),
            (EVENT_QUOTE_REFETCH_END, "SUCCESS", "", 45.0, 100.0),
            (EVENT_RISK_RECHECK, "SUCCESS", "", 0.8, 0.0),
            (EVENT_CASH_CHECK, "SUCCESS", "", 0.4, 0.0),
            (EVENT_ORDER_SUBMIT_START, "SUCCESS", "", 0.1, 0.0),
            (EVENT_BROKER_ACK, "SUCCESS", "", 120.0, 0.0),
            (EVENT_FILL_RECEIVED, "SUCCESS", "", 5.0, 0.0),
        ]

        for ev_name, res, rej, elap, q_age in events:
            self.telemetry.record_funnel_event(
                event_name=ev_name,
                symbol="005930",
                strategy="INT_ORB",
                order_id="ORD_001",
                result=res,
                reject_reason=rej,
                elapsed_ms=elap,
                quote_age_ms=q_age,
                now_dt=now
            )

        self.assertEqual(len(self.telemetry.event_records), len(events))
        first_rec = self.telemetry.event_records[0]
        self.assertEqual(first_rec.event_name, EVENT_SIGNAL_DETECTED)
        self.assertEqual(first_rec.symbol, "005930")
        self.assertEqual(first_rec.strategy, "INT_ORB")
        self.assertEqual(first_rec.order_id, "ORD_001")
        self.assertEqual(first_rec.result, "SUCCESS")

        refetch_rec = [r for r in self.telemetry.event_records if r.event_name == EVENT_QUOTE_REFETCH_START][0]
        self.assertEqual(refetch_rec.quote_age_ms, 3500.0)

    def test_percentile_calculations(self):
        """StageStats의 p50, p95, p99, max, avg 백분위수 산출 정밀도 검증"""
        stats = StageStats(stage_name="test_stage")
        # 빈 데이터 검증
        p_empty = stats.compute_percentiles()
        self.assertEqual(p_empty["avg"], 0.0)
        self.assertEqual(p_empty["p50"], 0.0)
        self.assertEqual(p_empty["max"], 0.0)

        # 100개 데이터 입력 (1ms ~ 100ms)
        for i in range(1, 101):
            stats.record_entry()
            stats.record_success(float(i))

        p = stats.compute_percentiles()
        self.assertAlmostEqual(p["avg"], 50.5, delta=0.1)
        self.assertAlmostEqual(p["p50"], 50.0, delta=1.0)
        self.assertAlmostEqual(p["p95"], 95.0, delta=1.0)
        self.assertAlmostEqual(p["p99"], 99.0, delta=1.0)
        self.assertEqual(p["max"], 100.0)

    def test_bottleneck_detection_sla_breach(self):
        """p95 지연시간이 SLA 기준치를 초과할 때 CRITICAL 또는 WARNING 병목 후보 감지 검증"""
        # broker_ack 기준 SLA는 1,500ms
        stg = self.telemetry.stages["broker_ack"]
        for _ in range(20):
            stg.record_entry()
            stg.record_success(2000.0)  # 2000ms > 1500ms

        alerts = self.telemetry.detect_bottlenecks()
        sla_alerts = [a for a in alerts if a["category"] == "SLA_BREACH"]
        self.assertTrue(len(sla_alerts) > 0)
        alert = sla_alerts[0]
        self.assertEqual(alert["stage"], "broker_ack")
        self.assertIn(alert["severity"], ("WARNING", "CRITICAL"))
        self.assertIn("1500.0ms", alert["message"])

    def test_bottleneck_detection_tail_latency_spike(self):
        """p99 tail latency 급증 (p99 > 2 * p95) 감지 검증"""
        stg = self.telemetry.stages["signal_to_router"]
        # 98개는 10ms, 상위 2개는 400ms (p95 = 10ms, p99 = 400ms > 2 * 10ms and >= 300ms)
        for _ in range(98):
            stg.record_entry()
            stg.record_success(10.0)
        for _ in range(2):
            stg.record_entry()
            stg.record_success(400.0)

        alerts = self.telemetry.detect_bottlenecks()
        tail_alerts = [a for a in alerts if a["category"] == "TAIL_LATENCY_SPIKE"]
        self.assertTrue(len(tail_alerts) > 0)
        self.assertEqual(tail_alerts[0]["stage"], "signal_to_router")

    def test_bottleneck_detection_reject_rate_spike(self):
        """특정 단계의 reject rate가 40%를 초과할 때 병목 후보 감지 검증"""
        stg = self.telemetry.stages["risk_recheck"]
        stg.record_entry()
        stg.record_success(1.0)
        stg.record_entry()
        stg.record_reject("RISK_LIMIT", 1.0)
        stg.record_entry()
        stg.record_reject("RISK_LIMIT", 1.0)  # 2/3 = 66.7% > 40%

        alerts = self.telemetry.detect_bottlenecks()
        rej_alerts = [a for a in alerts if a["category"] == "REJECT_RATE_SPIKE"]
        self.assertTrue(len(rej_alerts) > 0)
        self.assertEqual(rej_alerts[0]["stage"], "risk_recheck")
        self.assertIn("66.7%", rej_alerts[0]["message"])

    def test_bottleneck_detection_scan_cycle_threshold(self):
        """전체 종목 스캔 시간이 3.0초(quote freshness 한도)를 초과할 때 병목 후보 감지 검증"""
        self.telemetry.record_scan_cycle(4.2)
        self.telemetry.record_scan_cycle(3.8)

        alerts = self.telemetry.detect_bottlenecks()
        scan_alerts = [a for a in alerts if a["category"] == "SCAN_CYCLE_EXCEEDS_FRESHNESS"]
        self.assertTrue(len(scan_alerts) > 0)
        self.assertEqual(scan_alerts[0]["severity"], "CRITICAL")
        self.assertIn("3.0초", scan_alerts[0]["message"])

    def test_bottleneck_detection_repeated_rejects(self):
        """동일 거절 사유가 3회 이상 반복 누적될 때 병목 후보 감지 검증"""
        stg = self.telemetry.stages["quote_refetch"]
        for _ in range(4):
            stg.record_entry()
            stg.record_reject("RE_FETCH_STALE (3500ms > 3000ms)", 50.0)

        alerts = self.telemetry.detect_bottlenecks()
        rep_alerts = [a for a in alerts if a["category"] == "REPEATED_REJECTION"]
        self.assertTrue(len(rep_alerts) > 0)
        self.assertIn("RE_FETCH_STALE", rep_alerts[0]["message"])

    def test_health_snapshot_structure(self):
        """get_health_snapshot()이 사용자 요구사항 6대 섹션 및 오늘 집중 지표를 정확히 반환하는지 검증"""
        # 이벤트 및 지표 몇 개 주입
        self.telemetry.record_scan_cycle(1.5)
        self.telemetry.record_signal_to_router_latency(12.0)
        self.telemetry.record_quote_refetch_attempt(is_success=True, is_stale_before=True, latency_ms=45.0)
        self.telemetry.record_refetched_order_sent()
        self.telemetry.record_broker_api_call(latency_ms=150.0, is_success=True)
        self.telemetry.record_order_lifecycle(is_sent=True, is_filled=True)

        snapshot = self.telemetry.get_health_snapshot()

        # 6대 필수 섹션 확인
        required_sections = [
            "execution_latency",
            "order_funnel",
            "quote_health",
            "broker_health",
            "fill_health",
            "bottleneck_alerts"
        ]
        for sec in required_sections:
            self.assertIn(sec, snapshot, f"Missing required section: {sec}")

        # 오늘 집중 지표 확인
        self.assertIn("today_special_metrics", snapshot)
        today_m = snapshot["today_special_metrics"]
        self.assertIn("scan_cycle_time_ms", today_m)
        self.assertIn("signal_to_router_latency_ms", today_m)
        self.assertIn("quote_age_ms", today_m)
        self.assertIn("stale_to_refetch_trigger_rate_pct", today_m)
        self.assertIn("refetch_success_rate_pct", today_m)
        self.assertIn("refetch_to_order_send_rate_pct", today_m)
        self.assertIn("order_submit_to_broker_ack_ms", today_m)
        self.assertIn("broker_ack_to_fill_ms", today_m)

        # Re-fetch 성공률 및 주문 전송률 정량 검증
        self.assertEqual(today_m["refetch_success_rate_pct"], 100.0)
        self.assertEqual(today_m["refetch_to_order_send_rate_pct"], 100.0)
        self.assertEqual(snapshot["fill_health"]["orders_filled"], 1)

    def test_order_router_funnel_integration(self):
        """OrderRouter와 ExecutionFunnelTelemetry가 통합 연동되어 이벤트가 정상 기록되는지 검증"""
        client = MockBrokerClient(dry_run=True)
        router = OrderRouter(namu_client=client, circuit_breaker=None, funnel_telemetry=self.telemetry)

        now = datetime.now()
        sig = TradeSignal(
            strategy_id="INT_ORB",
            time_horizon=TimeHorizon.INTRADAY,
            iem_cd="005930",
            name="삼성전자",
            side=OrderSide.BUY,
            strategy_price=10000,
            stop_price=9800,
            score=85.0,
            reason="ORB 돌파",
            timestamp=now,
            target_1r=10150,
            target_2r=10300,
            target_3r=10500,
            order_type=OrderType.LIMIT
        )
        # 1초 전 수신된 Fresh Quote 부여
        sig.quote_snapshot = OrderQuoteSnapshot(
            symbol="005930",
            current_price=10000,
            bid=9990,
            ask=10000,
            quote_time="10:00:00",
            quote_timestamp=now - timedelta(seconds=1.0),
            received_at=now - timedelta(seconds=1.0),
            api_latency_ms=50.0,
            quote_data_age_ms=800.0,
            order_quote_age_ms=1000.0,
            is_fresh=True,
            source="REST_FIRST"
        )

        order = router.submit_order(
            signal=sig,
            shares=10,
            order_type=OrderType.LIMIT,
            order_price=10000,
            balance={"cash": 10_000_000, "total_asset": 10_000_000},
            portfolio_risk_status="NORMAL",
            now=now
        )

        self.assertIsNotNone(order)
        self.assertEqual(order.status, OrderStatus.FILLED)

        # 텔레메트리에 남은 이벤트 확인
        ev_names = [r.event_name for r in self.telemetry.event_records]
        self.assertIn(EVENT_RISK_RECHECK, ev_names)
        self.assertIn(EVENT_ORDER_CREATED, ev_names)
        self.assertIn(EVENT_ORDER_SUBMIT_START, ev_names)
        self.assertIn(EVENT_BROKER_ACK, ev_names)
        self.assertIn(EVENT_FILL_RECEIVED, ev_names)

        # Paper 체결 카운트 반영 확인
        self.assertEqual(self.telemetry.orders_sent_count, 1)
        self.assertEqual(self.telemetry.orders_filled_count, 1)

    def test_order_pipeline_contribution_calculation(self):
        """단일 주문의 전 구간 지연시간 등록 및 1/2위 병목 및 기여율 산출 정밀도 검증"""
        stage_lats = {
            "decision_approved": 2.0,
            "signal_to_router": 10.0,
            "quote_refetch": 150.0,
            "risk_recheck": 1.0,
            "cash_check": 1.0,
            "broker_submit": 2.0,
            "broker_ack": 800.0,
            "fill_received": 34.0,
        }
        rec = self.telemetry.record_order_pipeline_latency(
            order_id="ORD_1001",
            symbol="005930",
            strategy="INT_ORB",
            stage_latencies_ms=stage_lats
        )
        self.assertEqual(rec.total_pipeline_elapsed_ms, 1000.0)
        self.assertEqual(rec.primary_bottleneck, "BROKER_ACK")
        self.assertEqual(rec.primary_contribution_pct, 80.0)
        self.assertEqual(rec.secondary_bottleneck, "QUOTE_REFETCH")
        self.assertEqual(rec.secondary_contribution_pct, 15.0)
        self.assertAlmostEqual(sum(rec.stage_contributions_pct.values()), 100.0, delta=0.5)

    def test_cumulative_bottleneck_analysis(self):
        """누적 표본의 백분위수 및 단계별 기여율 집계 검증"""
        for i in range(5):
            self.telemetry.record_order_pipeline_latency(
                order_id=f"ORD_{i}",
                symbol="005930",
                strategy="INT_ORB",
                stage_latencies_ms={
                    "signal_to_router": 10.0,
                    "quote_refetch": 200.0,
                    "broker_ack": 790.0,
                }
            )
        analysis = self.telemetry.get_bottleneck_contribution_analysis()
        self.assertEqual(analysis["sample_count"], 5)
        self.assertEqual(analysis["overall_status"], CONFIDENCE_DIAGNOSTIC_ONLY)
        self.assertEqual(analysis["top_bottleneck"], "BROKER_ACK")
        self.assertGreaterEqual(analysis["top_contribution_pct"], 75.0)
        self.assertIn("broker_ack", analysis["stages"])
        b_ack = analysis["stages"]["broker_ack"]
        self.assertEqual(b_ack["sample_count"], 5)
        self.assertEqual(b_ack["p50"], 790.0)

    def test_sample_sufficiency_status_transitions(self):
        """표본수 기준에 따른 DIAGNOSTIC_ONLY -> PROVISIONAL -> RELIABLE 신뢰도 전이 검증"""
        # 1. 표본 < 10 -> DIAGNOSTIC_ONLY
        for i in range(3):
            self.telemetry.record_order_pipeline_latency(f"ORD_{i}", "005930", "INT_ORB", {"broker_ack": 100.0})
        analysis = self.telemetry.get_bottleneck_contribution_analysis()
        self.assertEqual(analysis["overall_status"], CONFIDENCE_DIAGNOSTIC_ONLY)

        # 2. 10 <= 표본 < 30 -> PROVISIONAL
        for i in range(3, 15):
            self.telemetry.record_order_pipeline_latency(f"ORD_{i}", "005930", "INT_ORB", {"broker_ack": 100.0})
        analysis = self.telemetry.get_bottleneck_contribution_analysis()
        self.assertEqual(analysis["overall_status"], CONFIDENCE_PROVISIONAL)

        # 3. 표본 >= 30 -> RELIABLE
        for i in range(15, 35):
            self.telemetry.record_order_pipeline_latency(f"ORD_{i}", "005930", "INT_ORB", {"broker_ack": 100.0})
        analysis = self.telemetry.get_bottleneck_contribution_analysis()
        self.assertEqual(analysis["overall_status"], CONFIDENCE_RELIABLE)

    def test_high_contribution_alert_insufficient_sample(self):
        """표본 < 10 일 때 기여율이 80%이더라도 병목으로 확정하지 않고 INSUFFICIENT_SAMPLE 플래그 검증"""
        for i in range(3):
            self.telemetry.record_order_pipeline_latency(
                order_id=f"ORD_{i}",
                symbol="005930",
                strategy="INT_ORB",
                stage_latencies_ms={"signal_to_router": 20.0, "broker_ack": 80.0}
            )
        alerts = self.telemetry.detect_bottlenecks()
        high_alerts = [a for a in alerts if a["category"] == "HIGH_CONTRIBUTION_BOTTLENECK"]
        self.assertTrue(len(high_alerts) > 0)
        alert = high_alerts[0]
        self.assertEqual(alert["severity"], "INSUFFICIENT_SAMPLE")
        self.assertEqual(alert["status"], "INSUFFICIENT_SAMPLE")
        self.assertIn("INSUFFICIENT_SAMPLE", alert["message"])

    def test_high_contribution_alert_warning(self):
        """표본 >= 10, 기여율 >= 50%이나 SLA 이내일 때 WARNING 병목 후보 식별 검증"""
        for i in range(12):
            self.telemetry.record_order_pipeline_latency(
                order_id=f"ORD_{i}",
                symbol="005930",
                strategy="INT_ORB",
                stage_latencies_ms={"signal_to_router": 40.0, "broker_ack": 60.0}
            )
        alerts = self.telemetry.detect_bottlenecks()
        high_alerts = [a for a in alerts if a["category"] == "HIGH_CONTRIBUTION_BOTTLENECK"]
        self.assertTrue(len(high_alerts) > 0)
        alert = high_alerts[0]
        self.assertEqual(alert["severity"], "WARNING")
        self.assertEqual(alert["status"], "CONFIRMED")
        self.assertEqual(alert["stage"], "broker_ack")

    def test_high_contribution_alert_critical(self):
        """표본 >= 10, 기여율 >= 70% 및 p95 SLA 초과 시 CRITICAL 병목 확정 검증"""
        # broker_ack SLA는 1500.0ms
        for i in range(12):
            self.telemetry.record_order_pipeline_latency(
                order_id=f"ORD_{i}",
                symbol="005930",
                strategy="INT_ORB",
                stage_latencies_ms={"signal_to_router": 100.0, "broker_ack": 2000.0}
            )
        alerts = self.telemetry.detect_bottlenecks()
        crit_alerts = [a for a in alerts if a["category"] == "HIGH_CONTRIBUTION_BOTTLENECK" and a["severity"] == "CRITICAL"]
        self.assertTrue(len(crit_alerts) > 0)
        alert = crit_alerts[0]
        self.assertEqual(alert["status"], "CONFIRMED")
        self.assertIn("핵심 병목으로 확정", alert["message"])

    def test_format_bottleneck_contribution_summary(self):
        """format_bottleneck_contribution_summary 텍스트 포맷 검증"""
        self.telemetry.record_order_pipeline_latency(
            order_id="ORD_1",
            symbol="005930",
            strategy="INT_ORB",
            stage_latencies_ms={"signal_to_router": 10.0, "broker_ack": 90.0}
        )
        summary = self.telemetry.format_bottleneck_contribution_summary()
        self.assertIn("[TOP BOTTLENECKS]", summary)
        self.assertIn("BROKER_ACK", summary)
        self.assertIn("[DATA SUFFICIENCY]", summary)
        self.assertIn("Samples: 1", summary)
        self.assertIn("Diagnostic Confidence: DIAGNOSTIC_ONLY", summary)


if __name__ == "__main__":
    unittest.main()
