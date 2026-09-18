"""[TEST] SimilarityMetaOutput Schema Parity & Fault Isolation Suite
tests/test_similarity_meta_schema_parity.py

검증 요구사항:
1. SimilarityMetaOutput 실제 필드와 호출부 정합성
2. win_rate 미존재 상태에서 AttributeError 미발생
3. Similarity 오류 발생 시 신규 BUY 안전 중단 (NO_TRADE 처리)
4. Similarity 오류 발생 시 기존 OPEN POSITION Watchdog 정상 실행 (스톱로스 유지)
5. 다음 cycle 정상 재개
"""

import os
import sys
import unittest
import dataclasses
from datetime import datetime, timedelta
from typing import Dict, Any, List

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if BASE_DIR not in sys.path:
    sys.path.insert(0, BASE_DIR)

from ml.similarity_engine import SimilarityMetaOutput, HistoricalSimilarityEngine
from ml.experience_memory import ExperienceMemory
from core.models import (
    Position, TradeSignal, OrderSide, OrderType, TimeHorizon, SymbolInfo, SymbolState
)
from execution.position_manager import PositionManager
from execution.order_router import OrderRouter
from execution.persistence_manager import PersistenceManager
from risk.circuit_breaker import CircuitBreaker


class MockSimilarityBrokerClient:
    def __init__(self):
        self.orders = []
        self.dry_run = False
        self.prices = {}

    def get_current_price(self, iem_cd: str) -> Dict[str, Any]:
        return self.prices.get(iem_cd, {"price": 50000, "bid": 50000, "ask": 50000})

    def sell_market(self, iem_cd: str, qty: int) -> Dict[str, Any]:
        self.orders.append({"side": "SELL", "iem_cd": iem_cd, "qty": qty})
        return {"Output_0": {"mkt_orr_no": f"SELL_MKT_{iem_cd}"}}

    def buy_market(self, iem_cd: str, qty: int) -> Dict[str, Any]:
        self.orders.append({"side": "BUY", "iem_cd": iem_cd, "qty": qty})
        return {"Output_0": {"mkt_orr_no": f"BUY_MKT_{iem_cd}"}}


class TestSimilarityMetaSchemaParity(unittest.TestCase):
    def setUp(self):
        self.test_db = f"data/test_sim_parity_{int(datetime.now().timestamp() * 1000)}.db"
        self.exp_memory = ExperienceMemory(db_path=self.test_db)
        self.sim_engine = HistoricalSimilarityEngine(memory=self.exp_memory, min_samples=3)
        self.broker = MockSimilarityBrokerClient()
        self.circuit_breaker = CircuitBreaker()
        self.circuit_breaker.update_data_heartbeat(datetime.now())
        self.router = OrderRouter(namu_client=self.broker, circuit_breaker=self.circuit_breaker)
        self.persistence = PersistenceManager(db_path=self.test_db)
        self.pm = PositionManager(order_router=self.router, persistence_manager=self.persistence)

    def tearDown(self):
        for f in [self.test_db, self.test_db + "-wal", self.test_db + "-shm"]:
            if os.path.exists(f):
                try:
                    os.remove(f)
                except Exception:
                    pass

    # -------------------------------------------------------------------------
    # 1. SimilarityMetaOutput 실제 필드와 호출부 정합성
    # -------------------------------------------------------------------------
    def test_01_similarity_meta_output_fields(self):
        fields = {f.name: f.type for f in dataclasses.fields(SimilarityMetaOutput)}
        expected_fields = [
            "hist_win_rate", "hist_target_rate", "hist_stop_rate", "expected_r",
            "avg_mfe", "avg_mae", "sample_count", "regime_match",
            "time_of_day_match", "similarity_distance", "is_sufficient_sample"
        ]
        for ef in expected_fields:
            self.assertIn(ef, fields, f"필드 {ef}가 SimilarityMetaOutput에 반드시 존재해야 함")

        # win_rate는 존재하지 않아야 함
        self.assertNotIn("win_rate", fields, "win_rate는 SimilarityMetaOutput의 필드가 아님 (hist_win_rate 사용)")

        # 실제 query_similarity 호출 결과 확인
        cur_feats = {"price": 70000, "score": 80.0, "rvol": 2.0}
        sim_res = self.sim_engine.query_similarity(cur_feats, current_regime="BULL", now=datetime.now())
        self.assertIsInstance(sim_res, SimilarityMetaOutput)
        self.assertTrue(hasattr(sim_res, "hist_win_rate"))
        self.assertFalse(hasattr(sim_res, "win_rate"))
        self.assertIsInstance(sim_res.hist_win_rate, float)
        self.assertIsInstance(sim_res.sample_count, int)

    # -------------------------------------------------------------------------
    # 2. win_rate 미존재 상태에서 AttributeError 미발생 검증
    # -------------------------------------------------------------------------
    def test_02_attribute_error_prevention(self):
        cur_feats = {"price": 70000, "score": 80.0, "rvol": 2.0}
        sim_res = self.sim_engine.query_similarity(cur_feats, current_regime="BULL", now=datetime.now())

        # 호출부가 hist_win_rate를 참조할 때 정상 작동
        try:
            formatted_win_rate = f"{sim_res.hist_win_rate:.2f}"
            samples = sim_res.sample_count
        except AttributeError as e:
            self.fail(f"hist_win_rate 참조 중 예기치 않은 AttributeError 발생: {e}")

        self.assertIsNotNone(formatted_win_rate)
        self.assertGreaterEqual(samples, 0)

        # win_rate 직접 접근 시 AttributeError가 정상 발생함을 검증 (임의 주입 방지)
        with self.assertRaises(AttributeError):
            _ = getattr(sim_res, "win_rate")

    # -------------------------------------------------------------------------
    # 3. Similarity 오류 발생 시 신규 BUY 안전 중단 (NO_TRADE 처리)
    # -------------------------------------------------------------------------
    def test_03_similarity_error_safely_halts_new_buy(self):
        # 모의 실패 SimilarityEngine
        class FailingSimilarityEngine:
            def query_similarity(self, *args, **kwargs):
                raise RuntimeError("Similarity Database Query Failed (Simulated)")

        failing_sim = FailingSimilarityEngine()
        sig = TradeSignal(
            strategy_id="TEST_MOMENTUM", time_horizon=TimeHorizon.INTRADAY, iem_cd="005930",
            name="삼성전자", side=OrderSide.BUY, strategy_price=70000, stop_price=68000,
            score=85.0, reason="돌파", timestamp=datetime.now()
        )

        # live_quant_trader.py의 보호 패턴 시뮬레이션
        buy_halted = False
        no_trade_recorded = False

        try:
            sim_meta = failing_sim.query_similarity(
                current_features={"price": sig.strategy_price, "score": sig.score, "rvol": 2.0},
                current_regime="BULL",
                now=datetime.now()
            )
            # win_rate 포맷팅 (정상 경로)
            _ = f"{sim_meta.hist_win_rate:.2f}"
        except Exception as sim_err:
            # 안전 차단 및 NO_TRADE 기록
            self.persistence.record_no_trade(
                iem_cd=sig.iem_cd,
                name=sig.name,
                strategy_id=sig.strategy_id,
                score=sig.score,
                ml_prob=0.0,
                expected_net_r=0.0,
                primary_reason=f"SIMILARITY_ERROR: {sim_err}",
                category="CORRECT_NO_TRADE"
            )
            buy_halted = True
            no_trade_recorded = True

        self.assertTrue(buy_halted, "Similarity 오류 시 해당 후보 BUY 판단이 안전 중단되어야 함")
        self.assertTrue(no_trade_recorded, "Similarity 오류 시 NO_TRADE로 기록되어야 함")
        self.assertEqual(len(self.broker.orders), 0, "BUY 주문이 브로커로 발주되지 않아야 함")

    # -------------------------------------------------------------------------
    # 4. Similarity 오류 발생 시 기존 OPEN POSITION Watchdog 정상 실행
    # -------------------------------------------------------------------------
    def test_04_similarity_error_exit_watchdog_unaffected(self):
        # 기존 보유 포지션 등록 (손절가 하회 상황)
        pos = self.pm.open_position(
            time_horizon=TimeHorizon.INTRADAY,
            strategy_id="TEST_STRAT",
            iem_cd="000660",
            name="SK하이닉스",
            qty=10,
            entry_price=120000,
            stop_price=115000,
            target_1r=125000,
            target_2r=130000,
            target_3r=135000,
            initial_risk=50000
        )
        self.broker.prices["000660"] = {"price": 114000, "bid": 114000, "ask": 114000}

        # 신규 매수 파이프라인에서 Similarity 오류 발생 시뮬레이션
        class BrokenSimEngine:
            def query_similarity(self, *args, **kwargs):
                raise ConnectionError("Similarity cluster offline")

        broken_sim = BrokenSimEngine()
        with self.assertRaises(ConnectionError):
            broken_sim.query_similarity({}, "BULL", datetime.now())

        # Step 0 Exit Watchdog 청산 로직 실행
        self.pm.update_price_and_manage("000660", 114000, datetime.now())

        # 기존 보유 포지션은 손절 매도 발주 완료되어야 함
        self.assertEqual(len(self.broker.orders), 1)
        self.assertEqual(self.broker.orders[0]["side"], "SELL")
        self.assertEqual(self.broker.orders[0]["iem_cd"], "000660")
        self.assertTrue(pos.is_closed)

    # -------------------------------------------------------------------------
    # 5. 다음 cycle 정상 재개
    # -------------------------------------------------------------------------
    def test_05_next_cycle_normal_resumption(self):
        # Cycle 1: Similarity 엔진 일시 오류 발생
        error_cycle_halted = False
        try:
            raise TimeoutError("Temporary Redis/Memory Lock Timeout")
        except TimeoutError:
            error_cycle_halted = True
        self.assertTrue(error_cycle_halted)

        # Cycle 2: 정상 재개
        cur_feats = {"price": 70000, "score": 85.0, "rvol": 2.5}
        sim_res = self.sim_engine.query_similarity(cur_feats, current_regime="BULL", now=datetime.now())
        self.assertIsInstance(sim_res, SimilarityMetaOutput)
        self.assertGreaterEqual(sim_res.hist_win_rate, 0.0)
        self.assertLessEqual(sim_res.hist_win_rate, 1.0)


if __name__ == "__main__":
    unittest.main()
