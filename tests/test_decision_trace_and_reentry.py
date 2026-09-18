"""Tests for BUY_DECISION_TRACE, NO_TRADE_TRACE, Re-entry Quality Telemetry,
Counterfactual Tracking, Micro-Trade Telemetry, and Section 11 Live Reporting.
"""

import os
import json
import pytest
from datetime import datetime, timedelta
from unittest.mock import MagicMock

from core.models import Position, TimeHorizon, Candle
from core.decision_trace import (
    DecisionTraceRecord,
    DecisionTraceRegistry,
    format_buy_trace,
    format_no_trade_trace,
    format_trace
)
from strategies.profit_opportunity_gate import (
    ProfitOpportunityTelemetry,
    CounterfactualTracker
)
from execution.position_manager import PositionManager
from ml.trade_db import TradeDatabase, TradeRecord


@pytest.fixture(autouse=True)
def reset_singletons():
    """Ensure clean singleton registries before each test."""
    DecisionTraceRegistry.get_instance().reset()
    ProfitOpportunityTelemetry.get_instance().reset()
    CounterfactualTracker.get_instance().reset()
    yield
    DecisionTraceRegistry.get_instance().reset()
    ProfitOpportunityTelemetry.get_instance().reset()
    CounterfactualTracker.get_instance().reset()


class TestDecisionTraceAndReentry:

    def test_buy_decision_trace_emitted_with_all_fields(self):
        """
        Group 1: BUY_APPROVED 발생 시 최종 BUY Decision Trace에 18개 핵심 필드가
        모두 기록되고 format_buy_trace 및 registry에 정상 저장되는지 검증.
        """
        registry = DecisionTraceRegistry.get_instance()
        now_iso = datetime.now().isoformat()

        record = DecisionTraceRecord(
            timestamp=now_iso,
            trading_mode="LIVE",
            account_no="55021234-01",
            symbol="005930",
            strategy="INT_BREAKOUT_VOL_EXPANSION",
            entry_price=80000.0,
            current_price=80000.0,
            bid=79900.0,
            ask=80100.0,
            spread_bps=25.0,
            target_1r=82000.0,
            stop_price=78800.0,
            gross_move_pct=0.025,
            expected_cost_pct=0.0033,
            net_move_pct=0.0217,
            cost_to_opportunity_ratio=0.132,
            rebound_strength=0.65,
            decision="BUY_APPROVED",
            rvol=2.1,
            momentum_3m=0.85,
            vwap_gap=0.012,
            signal_age_ms=120.0,
            price_drift_pct=0.001,
            shares=10,
            order_type="LIMIT"
        )

        formatted = format_buy_trace(record)
        assert "[BUY_DECISION_TRACE]" in formatted
        assert "005930" in formatted
        assert "INT_BREAKOUT_VOL_EXPANSION" in formatted
        assert "spread=25.0bps" in formatted
        assert "net_move=+2.17%" in formatted
        assert "cost/opp=0.13" in formatted
        assert "rebound_strength=0.65" in formatted
        assert "DECISION=BUY_APPROVED" in formatted

        # Record into registry
        registry.record_buy(record)
        assert len(registry.traces) == 1
        assert len(registry.buy_traces) == 1
        assert len(registry.no_trade_traces) == 0

        summary = registry.get_summary(trading_mode="LIVE")
        assert summary["total_decisions"] == 1
        assert summary["buy_approved_count"] == 1
        assert summary["no_trade_count"] == 0

        # Verify all 18 fields are present in dict
        d = record.to_dict()
        required_keys = [
            "timestamp", "trading_mode", "account_no", "symbol", "strategy",
            "entry_price", "current_price", "bid", "ask", "spread_bps",
            "target_1r", "stop_price", "gross_move_pct", "expected_cost_pct",
            "net_move_pct", "cost_to_opportunity_ratio", "rebound_strength",
            "decision"
        ]
        for k in required_keys:
            assert k in d, f"Missing required field: {k}"

    def test_no_trade_trace_distinguishes_initial_vs_final_reject(self):
        """
        Group 2: 초기 필터 탈락(예: SPREAD_TOO_WIDE, CASH_RESERVATION, REENTRY_COOLDOWN)과
        최종 Gate 탈락(MIN_PROFIT_OPPORTUNITY_INSUFFICIENT, LOW_REBOUND_STRENGTH)이
        reject_gate와 reason으로 정확히 구분 기록되는지 검증.
        """
        registry = DecisionTraceRegistry.get_instance()
        now_iso = datetime.now().isoformat()

        # Initial filter rejection: SPREAD_TOO_WIDE
        rec_initial = DecisionTraceRecord(
            timestamp=now_iso,
            trading_mode="LIVE",
            account_no="55021234-01",
            symbol="000660",
            strategy="INT_MOMENTUM_IGNITION",
            entry_price=150000.0,
            current_price=150000.0,
            bid=148000.0,
            ask=152000.0,
            spread_bps=266.6,
            target_1r=154000.0,
            stop_price=147000.0,
            gross_move_pct=0.0267,
            expected_cost_pct=0.027,
            net_move_pct=-0.0003,
            cost_to_opportunity_ratio=1.01,
            rebound_strength=0.0,
            decision="REJECTED",
            reject_gate="SPREAD_GUARD",
            reject_reason="SPREAD_TOO_WIDE (266.6bps > 35bps)"
        )
        registry.record_no_trade(rec_initial)

        # Final hard gate rejection: MIN_PROFIT_OPPORTUNITY_INSUFFICIENT
        rec_final = DecisionTraceRecord(
            timestamp=now_iso,
            trading_mode="LIVE",
            account_no="55021234-01",
            symbol="035720",
            strategy="INT_VWAP_PULLBACK",
            entry_price=50000.0,
            current_price=50000.0,
            bid=49950.0,
            ask=50050.0,
            spread_bps=20.0,
            target_1r=50300.0,
            stop_price=49700.0,
            gross_move_pct=0.006,
            expected_cost_pct=0.0033,
            net_move_pct=0.0027,
            cost_to_opportunity_ratio=0.55,
            rebound_strength=0.008,
            decision="REJECTED",
            reject_gate="MIN_PROFIT_OPPORTUNITY_GATE",
            reject_reason="MIN_PROFIT_OPPORTUNITY_INSUFFICIENT: net_move=0.27% < 0.80%"
        )
        registry.record_no_trade(rec_final)

        # Another initial filter: REENTRY_COOLDOWN
        rec_cooldown = DecisionTraceRecord(
            timestamp=now_iso,
            trading_mode="LIVE",
            account_no="55021234-01",
            symbol="005930",
            strategy="INT_BREAKOUT_VOL_EXPANSION",
            entry_price=80000.0,
            current_price=80000.0,
            bid=79950.0,
            ask=80050.0,
            spread_bps=12.5,
            target_1r=82000.0,
            stop_price=79000.0,
            gross_move_pct=0.025,
            expected_cost_pct=0.0033,
            net_move_pct=0.0217,
            cost_to_opportunity_ratio=0.13,
            rebound_strength=0.015,
            decision="REJECTED",
            reject_gate="REENTRY_GUARD",
            reject_reason="REENTRY_COOLDOWN_ACTIVE (300s remaining)"
        )
        registry.record_no_trade(rec_cooldown)

        # Verify formatting
        formatted_initial = format_no_trade_trace(rec_initial)
        assert "[NO_TRADE_TRACE]" in formatted_initial
        assert "GATE=SPREAD_GUARD" in formatted_initial
        assert "SPREAD_TOO_WIDE" in formatted_initial

        formatted_final = format_no_trade_trace(rec_final)
        assert "[NO_TRADE_TRACE]" in formatted_final
        assert "GATE=MIN_PROFIT_OPPORTUNITY_GATE" in formatted_final
        assert "MIN_PROFIT_OPPORTUNITY_INSUFFICIENT" in formatted_final

        # Verify registry separation
        assert len(registry.no_trade_traces) == 3
        summary = registry.get_summary(trading_mode="LIVE")
        assert summary["no_trade_count"] == 3
        reasons = summary["reasons"]
        assert "SPREAD_TOO_WIDE (266.6bps > 35bps)" in reasons
        assert "MIN_PROFIT_OPPORTUNITY_INSUFFICIENT: net_move=0.27% < 0.80%" in reasons
        assert "REENTRY_COOLDOWN_ACTIVE (300s remaining)" in reasons

    def test_reentry_quality_telemetry_tracking(self):
        """
        Group 3:
        - 1차 손절 후 15분 경과 전: REENTRY_COOLDOWN 탈락 및 reason 기록
        - 15분 경과 후: 재진입 허용 시 telemetry에 same_symbol_entry_count_today=2,
          same_symbol_stop_count_today=1, previous_trade_pnl, time_since_last_exit 채워짐
        - 3회 손절 후: REENTRY_DAILY_LIMIT_EXCEEDED 차단
        - 동일 종목의 다른 전략과 분리 추적 검증
        """
        pm = PositionManager(trading_mode="LIVE")
        sym = "005930"
        t0 = datetime(2026, 9, 17, 9, 30, 0)

        # 1. Trade 1: Entry with INT_BREAKOUT
        pm.open_position(
            iem_cd=sym,
            name="삼성전자",
            entry_price=80000,
            quantity=10,
            time_horizon=TimeHorizon.INTRADAY,
            target_1r=81600,
            target_2r=83200,
            stop_price=78400,
            strategy_id="INT_BREAKOUT",
            entry_time=t0,
            entry_reason="BREAKOUT_SIGNAL",
            entry_rvol=2.0
        )
        assert pm.daily_entry_counts_by_symbol[sym] == 1
        assert pm.daily_entry_counts_by_strategy[(sym, "INT_BREAKOUT")] == 1

        # Simulate price excursion and stop loss at t0 + 5m
        pos1 = pm.get_position(sym)
        assert pos1 is not None
        t1 = t0 + timedelta(minutes=5)
        pm.update_price_and_manage(sym, 79000, t0 + timedelta(minutes=2))
        pm.update_price_and_manage(sym, 78300, t1)  # Triggers stop

        # Check telemetry immediately after exit
        telem_sym = pm.get_reentry_telemetry(sym, now=t1)
        assert telem_sym["same_symbol_entry_count_today"] == 1
        assert telem_sym["same_symbol_stop_count_today"] == 1
        assert telem_sym["previous_trade_pnl"] < 0
        assert "손절" in telem_sym["last_exit_reason"] or "스톱" in telem_sym["last_exit_reason"]
        assert telem_sym["time_since_last_exit"] is not None
        assert telem_sym["time_since_last_exit"] < 10.0  # Just exited

        # 2. Re-entry at t1 + 10m (15m has NOT passed since exit t1)
        # PositionManager's is_reentry_allowed check
        t_reentry_early = t1 + timedelta(minutes=10)
        allowed_early, reason_early = pm.is_reentry_allowed(sym, "INT_BREAKOUT", current_time=t_reentry_early)
        assert not allowed_early
        assert "손절 후 쿨다운" in reason_early or "재진입 쿨다운" in reason_early

        # 3. Re-entry at t1 + 16m (15m elapsed)
        t_reentry_ok = t1 + timedelta(minutes=16)
        allowed_ok, reason_ok = pm.is_reentry_allowed(sym, "INT_BREAKOUT", current_time=t_reentry_ok)
        assert allowed_ok

        # Open Trade 2: Re-entry allowed
        pm.open_position(
            iem_cd=sym,
            name="삼성전자",
            entry_price=79500,
            quantity=10,
            time_horizon=TimeHorizon.INTRADAY,
            target_1r=81000,
            target_2r=82500,
            stop_price=78000,
            strategy_id="INT_BREAKOUT",
            entry_time=t_reentry_ok
        )
        assert pm.daily_entry_counts_by_symbol[sym] == 2
        assert pm.daily_entry_counts_by_strategy[(sym, "INT_BREAKOUT")] == 2

        # Close Trade 2 with stop loss at t_reentry_ok + 5m
        t2_exit = t_reentry_ok + timedelta(minutes=5)
        pm.update_price_and_manage(sym, 77900, t2_exit)
        assert pm.daily_stop_counts_by_symbol[sym] == 2
        assert pm.daily_stop_counts_by_strategy[(sym, "INT_BREAKOUT")] == 2

        # Open Trade 3 at t2_exit + 16m and close with stop loss
        t3_entry = t2_exit + timedelta(minutes=16)
        pm.open_position(
            iem_cd=sym,
            name="삼성전자",
            entry_price=78000,
            quantity=10,
            time_horizon=TimeHorizon.INTRADAY,
            target_1r=79500,
            target_2r=81000,
            stop_price=76500,
            strategy_id="INT_BREAKOUT",
            entry_time=t3_entry
        )
        t3_exit = t3_entry + timedelta(minutes=5)
        pm.update_price_and_manage(sym, 76400, t3_exit)
        assert pm.daily_stop_counts_by_symbol[sym] == 3
        assert pm.daily_stop_counts_by_strategy[(sym, "INT_BREAKOUT")] == 3

        # Now 3 stops accumulated for today: 4th attempt should be blocked permanently today
        t4_attempt = t3_exit + timedelta(minutes=30)
        allowed_4th, reason_4th = pm.is_reentry_allowed(sym, "INT_BREAKOUT", current_time=t4_attempt)
        assert not allowed_4th
        assert "손절 제한 초과" in reason_4th or "3회" in reason_4th

        # 4. Verify separation of another strategy on the same symbol
        # For strategy INT_VWAP_PULLBACK, entry count is 0 and stop count is 0
        strat_telem = pm.get_reentry_telemetry(sym, strategy="INT_VWAP_PULLBACK")
        assert strat_telem["same_symbol_entry_count_today"] == 0
        assert strat_telem["same_symbol_stop_count_today"] == 0

    def test_counterfactual_tracker_records_blocked_trades(self):
        """
        Group 4: Gate에서 차단된 거래가 CounterfactualTracker에 등록되고,
        이후 가격 수신 시 5분/15분/30분 가격 및 MFE/MAE가 갱신되며,
        BUY 체결된 거래 통계와 완벽히 분리되어 리포트되는지 검증.
        """
        tracker = CounterfactualTracker.get_instance()
        tracker.reset()

        t0 = datetime(2026, 9, 17, 10, 0, 0)
        # Register a blocked trade
        tracker.record_blocked_trade(
            iem_cd="035420",
            name="NAVER",
            strategy="INT_MOMENTUM_IGNITION",
            blocked_reason="MIN_PROFIT_OPPORTUNITY_INSUFFICIENT (net_move=0.55% < 0.80%)",
            price=200000.0,
            timestamp=t0,
            target_price=204000.0,
            stop_price=197000.0
        )
        assert len(tracker.blocked_trades) == 1
        item = tracker.blocked_trades[0]
        assert item["status"] == "TRACKING"
        assert item["entry_price"] == 200000.0

        # Feed price at t0 + 2m: price rises to 203,000 (MFE=1.5%)
        tracker.update_price("035420", 203000.0, t0 + timedelta(minutes=2))
        assert item["highest_price"] == 203000.0
        assert item["mfe_pct"] == pytest.approx(0.015, abs=1e-4)

        # Feed price at t0 + 5m (300s): price drops to 201,000
        tracker.update_price("035420", 201000.0, t0 + timedelta(minutes=5))
        assert item["price_5m"] == 201000.0
        assert item["return_5m"] == pytest.approx(0.005, abs=1e-4)

        # Feed price at t0 + 15m (900s): price reaches 206,000 (MFE=3.0%)
        tracker.update_price("035420", 206000.0, t0 + timedelta(minutes=15))
        assert item["price_15m"] == 206000.0
        assert item["return_15m"] == pytest.approx(0.03, abs=1e-4)
        assert item["highest_price"] == 206000.0
        assert item["mfe_pct"] == pytest.approx(0.03, abs=1e-4)

        # Feed price at t0 + 30m (1800s): price ends at 202,000
        tracker.update_price("035420", 202000.0, t0 + timedelta(minutes=30))
        assert item["price_30m"] == 202000.0
        assert item["return_30m"] == pytest.approx(0.01, abs=1e-4)
        assert item["status"] == "COMPLETED"

        # Summary statistics
        stats = tracker.get_summary_stats()
        assert stats["total_blocked"] == 1
        assert stats["completed_count"] == 1
        assert stats["avg_mfe_pct"] == pytest.approx(0.03, abs=1e-4)
        # Because return_15m is 3% >= 1.0%, this counterfactual was a "good move missed"
        assert stats["good_move_missed_count"] == 1
        assert stats["good_move_missed_ratio"] == 1.0

    def test_micro_trade_telemetry_and_classification(self):
        """
        Group 5: 익절/손절/타임스톱 완료 거래 중 gross_move < 0.0050 AND holding_sec < 300인 거래가
        micro_trade로 분류되고, 전략별 micro trade 수, 손익, 비중(micro_trade_ratio)이 정확히 집계되는지 검증.
        """
        telem = ProfitOpportunityTelemetry.get_instance()
        telem.reset()

        # Trade 1: Normal profitable move (gross move = 2.0%, holding = 600s) -> Not micro
        telem.record_trade_completion(
            symbol="005930",
            strategy="INT_BREAKOUT",
            entry_price=80000,
            exit_price=81600,
            pnl=16000,
            holding_seconds=600,
            exit_reason="목표가 익절 (+2.0%)"
        )

        # Trade 2: Micro trade (gross move = 0.25% < 0.50%, holding = 120s < 300s) -> Micro trade
        telem.record_trade_completion(
            symbol="000660",
            strategy="INT_MOMENTUM_IGNITION",
            entry_price=160000,
            exit_price=160400,
            pnl=4000,
            holding_seconds=120,
            exit_reason="1차 목표 조기청산"
        )

        # Trade 3: Micro loss trade (gross move = 0.30% < 0.50%, holding = 180s < 300s) -> Micro trade
        telem.record_trade_completion(
            symbol="035420",
            strategy="INT_BREAKOUT",
            entry_price=200000,
            exit_price=199400,
            pnl=-6000,
            holding_seconds=180,
            exit_reason="조기 미세 손절"
        )

        # Trade 4: Low move but long holding (gross move = 0.20%, holding = 1800s > 300s) -> Not micro (Time stop)
        telem.record_trade_completion(
            symbol="035720",
            strategy="INT_VWAP_PULLBACK",
            entry_price=50000,
            exit_price=50100,
            pnl=1000,
            holding_seconds=1800,
            exit_reason="TIME_STOP 정체 청산"
        )

        assert telem.total_completed_trades == 4
        assert telem.micro_trade_count == 2
        assert telem.micro_trade_ratio == pytest.approx(2 / 4, abs=1e-4)

        # Verify strategy breakdown
        assert telem.strategy_micro_counts["INT_MOMENTUM_IGNITION"] == 1
        assert telem.strategy_micro_counts["INT_BREAKOUT"] == 1
        assert "INT_VWAP_PULLBACK" not in telem.strategy_micro_counts

        # Verify PnL of micro trades
        assert len(telem.micro_trade_pnls) == 2
        assert sum(telem.micro_trade_pnls) == -2000

    def test_round_trip_entry_quality_to_exit_linkage(self):
        """
        Group 6: 진입 시점의 entry quality (RVOL, momentum, rebound_strength, expected_move)가
        Position 객체 및 청산 시 TradeRecord / trade_history에 보존되어
        손익과 연계 분석 가능한지 검증.
        """
        pm = PositionManager(trading_mode="LIVE")
        sym = "068270"
        t_entry = datetime(2026, 9, 17, 9, 45, 0)

        pm.open_position(
            iem_cd=sym,
            name="셀트리온",
            entry_price=200000,
            quantity=5,
            time_horizon=TimeHorizon.INTRADAY,
            target_1r=206000,
            target_2r=212000,
            stop_price=196000,
            strategy_id="INT_BREAKOUT_VOL_EXPANSION",
            entry_time=t_entry,
            entry_reason="RVOL_EXPANSION_ENTRY",
            entry_rvol=2.8,
            entry_momentum_3m=1.2,
            entry_vwap_gap=0.018,
            entry_rebound_strength=0.022,
            expected_move_pct=0.03,
            expected_net_r=1.8
        )

        pos = pm.get_position(sym)
        assert pos is not None
        assert pos.entry_rvol == 2.8
        assert pos.entry_momentum_3m == 1.2
        assert pos.entry_vwap_gap == 0.018
        assert pos.entry_rebound_strength == 0.022
        assert pos.expected_move_pct == 0.03
        assert pos.expected_net_r == 1.8

        # Manage price: goes up to 207,000, then exits at 206,000 (1R target)
        t_peak = t_entry + timedelta(minutes=10)
        pm.update_price_and_manage(sym, 207000, t_peak)
        assert pos.highest_price == 207000
        assert pos.mfe_pct > 0.03

        t_exit = t_entry + timedelta(minutes=15)
        pm._close_position(pos, 206000, t_exit, reason="목표가 익절 (+3.0%)")

        # Position should be closed
        assert not pm.has_position(sym)
        assert len(pm.trade_history) >= 1

        last_trade = pm.trade_history[-1]
        assert last_trade["symbol"] == sym
        assert last_trade["strategy"] == "INT_BREAKOUT_VOL_EXPANSION"
        assert last_trade["entry_rvol"] == 2.8
        assert last_trade["entry_momentum_3m"] == 1.2
        assert last_trade["entry_vwap_gap"] == 0.018
        assert last_trade["entry_rebound_strength"] == 0.022
        assert last_trade["expected_move_pct"] == 0.03
        assert last_trade["expected_net_r"] == 1.8
        assert last_trade["pnl"] > 0
        assert last_trade["holding_seconds"] == pytest.approx(900.0, abs=1.0)
        assert last_trade["mfe_pct"] == pytest.approx((207000 - 200000) / 200000, abs=1e-4)

    def test_section11_live_report_generation(self):
        """
        Group 7: generate_report_block()이 [ENTRY QUALITY], [PROFIT OPPORTUNITY],
        [CANCELLED / NO TRADE], [REENTRY], [MICRO TRADE], [COUNTERFACTUAL], [COMPLETED TRADE]
        7개 섹션을 모두 규격대로 정상 출력하는지 검증.
        """
        telem = ProfitOpportunityTelemetry.get_instance()
        telem.reset()

        # Feed some sample data to populate sections
        telem.record_gate_reject("MIN_PROFIT_OPPORTUNITY_INSUFFICIENT", "005930", 0.005, 0.0033, 0.0017, 0.008)
        telem.record_trade_completion("000660", "INT_MOMENTUM_IGNITION", 150000, 150300, 3000, 120, "익절")

        tracker = CounterfactualTracker.get_instance()
        tracker.reset()
        tracker.record_blocked_trade("035420", "NAVER", "INT_BREAKOUT", "REJECT_OPPORTUNITY", 200000, datetime.now())

        report = telem.generate_report_block(trading_mode="LIVE")

        required_sections = [
            "[ENTRY QUALITY]",
            "[PROFIT OPPORTUNITY]",
            "[CANCELLED / NO TRADE]",
            "[REENTRY]",
            "[MICRO TRADE]",
            "[COUNTERFACTUAL]",
            "[COMPLETED TRADE]"
        ]

        for sec in required_sections:
            assert sec in report, f"Section missing in report block: {sec}"

        assert "LIVE" in report
        assert "Micro Trade Ratio" in report
        assert "Blocked Trades" in report
        assert "STOP → Reentry < 15m:" in report
        assert "STOP → Reentry 15~30m:" in report
        assert "STOP → Reentry 30m+:" in report

    def test_reentry_disjoint_buckets_sum_conservation(self):
        """
        Group 8: 재진입 버킷의 상호 배타성 및 합계 보존성 검증:
        STOP → Reentry < 15m, 15~30m, 30m+ 세 버킷의 합이
        전체 STOP 후 재진입 횟수(total_stop_reentries)와 정확히 일치하는지 검증.
        """
        pm = PositionManager(trading_mode="LIVE")
        sym = "005930"
        t0 = datetime(2026, 9, 17, 9, 0, 0)

        # 1. Trade 1: Stop exit at 09:10
        pos1 = pm.open_position(
            iem_cd=sym, name="삼성전자", entry_price=80000, quantity=10,
            strategy_id="INT_BREAKOUT", entry_time=t0, stop_price=78000
        )
        t1_exit = t0 + timedelta(minutes=10)
        pm._close_position(pos1, 77800, t1_exit, "스톱로스 도달")

        # 2. Trade 2: Re-entry at 09:20 (gap = 10m < 15m) -> Bucket < 15m
        t2_entry = t1_exit + timedelta(minutes=10)
        pos2 = pm.open_position(
            iem_cd=sym, name="삼성전자", entry_price=78500, quantity=10,
            strategy_id="INT_BREAKOUT", entry_time=t2_entry, stop_price=77000
        )
        t2_exit = t2_entry + timedelta(minutes=5)
        pm._close_position(pos2, 76800, t2_exit, "스톱로스 도달")

        # 3. Trade 3: Re-entry at 09:45 (gap = 20m, 15~30m) -> Bucket 15~30m
        t3_entry = t2_exit + timedelta(minutes=20)
        pos3 = pm.open_position(
            iem_cd=sym, name="삼성전자", entry_price=77500, quantity=10,
            strategy_id="INT_BREAKOUT", entry_time=t3_entry, stop_price=76000
        )
        t3_exit = t3_entry + timedelta(minutes=5)
        pm._close_position(pos3, 75800, t3_exit, "스톱로스 도달")

        # 4. Trade 4: Re-entry at 10:30 (gap = 40m >= 30m) -> Bucket 30m+
        t4_entry = t3_exit + timedelta(minutes=40)
        pos4 = pm.open_position(
            iem_cd=sym, name="삼성전자", entry_price=76000, quantity=10,
            strategy_id="INT_BREAKOUT", entry_time=t4_entry, stop_price=74500
        )
        t4_exit = t4_entry + timedelta(minutes=10)
        pm._close_position(pos4, 78000, t4_exit, "목표가 익절")

        stats = pm.get_reentry_stats_summary()
        assert stats["stop_reentry_lt_15m"] == 1
        assert stats["stop_reentry_15_30m"] == 1
        assert stats["stop_reentry_gte_30m"] == 1
        assert stats["total_stop_reentries"] == 3
        # Mathematical conservation assertion:
        assert stats["stop_reentry_lt_15m"] + stats["stop_reentry_15_30m"] + stats["stop_reentry_gte_30m"] == stats["total_stop_reentries"]

    def test_restart_recovery_and_scale_out_exclusion(self):
        """
        Group 9: RESTART_RECOVERY와 SCALE_OUT이 당일 진입 횟수, 손절 횟수,
        telemetry 및 trade_history에서 철저히 배제되는지 검증.
        """
        pm = PositionManager(trading_mode="LIVE")
        telem = ProfitOpportunityTelemetry.get_instance()
        telem.reset()

        sym = "035420"
        t0 = datetime(2026, 9, 17, 9, 0, 0)

        # 1. Broker sync restores a position -> RESTART_RECOVERY
        holdings = [{
            "iem_cd": sym,
            "iem_nm": "NAVER",
            "qty": 20,
            "buy_price": 200000,
            "eval_price": 195000
        }]
        pm.sync_from_broker(holdings)
        assert pm.has_position(sym)
        # Entry count must be 0 for strategy entries
        assert pm.daily_entry_counts_by_symbol.get(sym, 0) == 0

        # Close the recovery position with stop loss
        rec_pos = pm.get_position(sym)
        pm._close_position(rec_pos, 190000, t0 + timedelta(minutes=15), "스톱로스 도달")

        # Telemetry & counts should NOT count recovery stop loss
        assert pm.daily_stop_counts_by_symbol.get(sym, 0) == 0
        assert len(pm.trade_history) == 0
        assert telem.total_completed_trades == 0

        # 2. Normal strategy trade with partial exit (SCALE_OUT)
        pos_strat = pm.open_position(
            iem_cd=sym, name="NAVER", entry_price=200000, quantity=10,
            strategy_id="INT_BREAKOUT", entry_time=t0 + timedelta(minutes=30),
            stop_price=196000, target_1r=204000
        )
        assert pm.daily_entry_counts_by_symbol[sym] == 1

        # Partial exit 3 shares at +1R
        pm._partial_exit(pos_strat, 3, 204000, "+1R 분할매도")
        # Partial exit must NOT append to trade_history or increment completed trades count
        assert len(pm.trade_history) == 0
        assert telem.total_completed_trades == 0

        # Full close remaining 7 shares
        pm._close_position(pos_strat, 205000, t0 + timedelta(minutes=45), "전량 익절")
        assert len(pm.trade_history) == 1
        assert telem.total_completed_trades == 1

    def test_validate_telemetry_consistency(self):
        """
        Group 10: Section 11 수학적 정합성 교차 검증 (Pipeline Monotonicity & Buckets)
        """
        telem = ProfitOpportunityTelemetry.get_instance()
        telem.reset()

        # Clean state: all 0
        res0 = telem.validate_telemetry_consistency()
        assert res0["is_valid"]

        # Valid pipeline: approved (3) >= sent (2) >= filled (2)
        telem.buy_approved_count = 3
        telem.orders_sent_count = 2
        telem.filled_count = 2
        telem.record_reentry_stats(same_symbol=2, stop_lt_15m=1, stop_15_30m=1, stop_gte_30m=0)
        res_valid = telem.validate_telemetry_consistency()
        assert res_valid["is_valid"]
        assert len(res_valid["errors"]) == 0

        # Invalid pipeline: sent > approved
        telem.buy_approved_count = 1
        telem.orders_sent_count = 2
        res_err_pipeline = telem.validate_telemetry_consistency()
        assert not res_err_pipeline["is_valid"]
        assert any("Pipeline order violation" in e for e in res_err_pipeline["errors"])
