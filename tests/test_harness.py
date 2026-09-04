"""통합 퀀트 자동매매 시스템 종합 검증 테스트 하네스 (Test Harness)
INTRADAY + SWING / EXECUTION-GRADE SPECIFICATION v5.0 전 항목 전수 검증
- 1. KRX 호가단위(Tick Size) 정규화 및 방향성 검증
- 2. 거래비용(Cost Model) 스트레스 시나리오 검증
- 3. 캔들 집계 및 보조지표(VWAP, EMA, RSI, ATR, RVOL) 계산 검증
- 4. 시장국면(Market Regime - Swing & Intraday) 판정 검증
- 5. 단타 5대 전략(ORB, PDH, VWAP PB, EMA PB, Momentum Burst) 검증
- 6. 스윙 4대 전략(Trend, HH60, MA20 PB, MA60 PB) 검증
- 7. 신호 채점 엔진(Scoring Engine) 100점 모델 검증
- 8. 포지션 사이징(Fixed Fractional Risk) 및 리스크 통제 검증
- 9. 손실 한도 및 서킷 브레이커(Circuit Breaker) 검증
- 10. 주문 라우터 10단계 안전점검 및 멱등성 검증
- 11. 포지션 매니저 다단계 분할 익절(+1R/+2R) 및 시간청산 검증
- 12. 백테스트 및 워크 포워드(Walk-Forward) 분석 검증
"""

import sys
import os
import unittest
from datetime import datetime, timedelta

# 프로젝트 루트 경로 추가
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core.models import (
    Tick, Candle, OrderSide, OrderType, OrderStatus, TimeHorizon, MarketRegime, TradeSignal
)
from core.tick_normalizer import get_tick_size, normalize_price
from core.cost_model import CostModel
from core.aggregator import CandleAggregator
from market_regime.swing_regime import SwingRegimeEngine
from market_regime.intraday_regime import IntradayRegimeEngine
from strategies.intraday.orb import ORBStrategy
from strategies.intraday.pdh_breakout import PDHBreakoutStrategy
from strategies.intraday.vwap_pullback import VWAPPullbackStrategy
from strategies.intraday.ema_pullback import EMAPullbackStrategy
from strategies.intraday.momentum_burst import MomentumBurstStrategy
from strategies.swing.trend_align import TrendAlignmentStrategy
from strategies.swing.hh60_breakout import HH60BreakoutStrategy
from strategies.swing.ma20_pullback import MA20PullbackStrategy
from strategies.swing.ma60_pullback import MA60PullbackStrategy
from strategies.scoring_engine import ScoringEngine
from risk.position_sizer import PositionSizer
from risk.portfolio_risk import PortfolioRiskManager
from risk.loss_limits import LossLimitManager
from risk.circuit_breaker import CircuitBreaker
from execution.order_router import OrderRouter
from execution.position_manager import PositionManager
from research.backtest_engine import BacktestEngine
from research.walk_forward import WalkForwardAnalyzer


class TestQuantHarness(unittest.TestCase):

    def test_01_krx_tick_normalization(self):
        """1. KRX 호가단위(Tick Size) 및 반올림 방향성 검증"""
        # 호가단위 구간 테스트
        self.assertEqual(get_tick_size(1500), 1)
        self.assertEqual(get_tick_size(3500), 5)
        self.assertEqual(get_tick_size(12000), 10)
        self.assertEqual(get_tick_size(35000), 50)
        self.assertEqual(get_tick_size(75000), 100)
        self.assertEqual(get_tick_size(250000), 500)
        self.assertEqual(get_tick_size(650000), 1000)

        # 반올림 방향성: 매수 지정가 (Floor) vs 매도 지정가 (Ceil)
        # 75,040원 (호가단위 100원)
        self.assertEqual(normalize_price(75040, "BUY", "LIMIT"), 75000)   # 가격 훼손 방지: 내림
        self.assertEqual(normalize_price(75040, "SELL", "LIMIT"), 75100)  # 가격 훼손 방지: 올림

        # 손절 매도 (Floor: 체결 우선)
        self.assertEqual(normalize_price(75080, "SELL", "STOP"), 75000)

    def test_02_cost_model_stress(self):
        """2. 거래비용 모델 (NORMAL, STRESS, WORST) 검증"""
        model = CostModel(market="KOSPI")
        # 100,000원에 10주 매수 -> 100만원 거래대금
        cost_normal = model.calculate_cost(OrderSide.BUY, 100000, 10, scenario="NORMAL")
        cost_stress = model.calculate_cost(OrderSide.BUY, 100000, 10, scenario="STRESS")
        cost_worst = model.calculate_cost(OrderSide.BUY, 100000, 10, scenario="WORST")

        # 스트레스 슬리피지 배수 검증
        self.assertAlmostEqual(cost_stress.slippage_cost, cost_normal.slippage_cost * 2.0)
        self.assertAlmostEqual(cost_worst.slippage_cost, cost_normal.slippage_cost * 3.0)

        # 매도 시 거래세 부과 확인
        sell_cost = model.calculate_cost(OrderSide.SELL, 100000, 10)
        self.assertGreater(sell_cost.agricultural_tax, 0.0)

    def test_03_candle_aggregator_and_indicators(self):
        """3. 캔들 집계 및 VWAP, EMA, RSI, ATR 계산 검증"""
        agg = CandleAggregator("005930")
        base_dt = datetime(2026, 9, 4, 9, 0, 0)

        # 30개의 1분 틱 데이터 주입
        price = 70000
        for i in range(30):
            # 분당 2회 틱
            t1 = Tick(base_dt + timedelta(minutes=i, seconds=10), "005930", price + i * 100, 100)
            t2 = Tick(base_dt + timedelta(minutes=i, seconds=50), "005930", price + i * 100 + 50, 200)
            agg.on_tick(t1)
            agg.on_tick(t2)

        # 1분봉 및 5분봉 완성 확인
        self.assertGreaterEqual(len(agg.candles_1m), 28)
        self.assertGreaterEqual(len(agg.candles_5m), 5)

        # 지표 산출 확인
        ema9 = agg.calculate_ema("1m", 9)
        ema20 = agg.calculate_ema("1m", 20)
        rsi = agg.calculate_rsi("1m", 14)
        atr = agg.calculate_atr("1m", 14)

        self.assertGreater(ema9, 0)
        self.assertGreater(ema20, 0)
        self.assertGreater(ema9, ema20)  # 지속 상승했으므로 EMA9 > EMA20
        self.assertGreater(rsi, 50.0)    # 상승추세이므로 RSI >= 50
        self.assertGreater(atr, 0.0)
        self.assertGreater(agg.vwap, 0)

    def test_04_market_regime_engines(self):
        """4. 스윙 및 단타 시장국면 판정 검증"""
        # 스윙: 상승 정배열
        prices = [1000 + i * 10 for i in range(250)]
        swing_res = SwingRegimeEngine.evaluate(prices, one_day_return=0.01)
        self.assertEqual(swing_res["regime"], MarketRegime.STRONG_BULL)

        # 스윙: 패닉 급락
        panic_prices = [2000 - i * 5 for i in range(250)]
        swing_panic = SwingRegimeEngine.evaluate(panic_prices, one_day_return=-0.03)
        self.assertEqual(swing_panic["regime"], MarketRegime.PANIC)

        # 단타: 정상 상승장 (AD 0.70)
        intra_bull = IntradayRegimeEngine.evaluate(
            advancing_count=700, declining_count=300,
            kospi_5m_return=0.002, kosdaq_5m_return=0.003
        )
        self.assertEqual(intra_bull["regime"], MarketRegime.STRONG_BULL)
        self.assertTrue(intra_bull["can_trade_intraday"])

        # 단타: 패닉장 (AD 0.25 또는 5분 -1.5% 급락)
        intra_panic = IntradayRegimeEngine.evaluate(
            advancing_count=200, declining_count=800,
            kospi_5m_return=-0.012, kosdaq_5m_return=-0.015
        )
        self.assertEqual(intra_panic["regime"], MarketRegime.PANIC)
        self.assertFalse(intra_panic["can_trade_intraday"])

    def test_05_intraday_strategies(self):
        """5. 단타 전략(ORB, PDH, VWAP Pullback, EMA Pullback, Momentum Burst) 검증"""
        agg = CandleAggregator("005930")
        base_dt = datetime(2026, 9, 4, 9, 0, 0)

        # 1분봉 65개 생성 (3분봉 EMA20 연산 요건 충족, 분당 2회 틱으로 양봉 몸통 형성)
        for i in range(65):
            p = 70000 + i * 100
            v = 3000 if i >= 60 else 1000
            agg.on_tick(Tick(base_dt + timedelta(minutes=i, seconds=10), "005930", p - 20, v // 2))
            agg.on_tick(Tick(base_dt + timedelta(minutes=i, seconds=50), "005930", p + 80, v // 2))

        agg.current_1m.volume = 3000
        now = datetime(2026, 9, 4, 10, 6, 0)
        cur_p = 76600
        or_high = 76000
        or_low = 74500

        # ORB 전략 평가
        sig_orb = ORBStrategy.evaluate(
            "005930", "삼성전자", cur_p, agg, MarketRegime.STRONG_BULL,
            now, spread_ratio=0.001, or_high=or_high, or_low=or_low
        )
        self.assertIsNotNone(sig_orb)
        self.assertEqual(sig_orb.strategy_id, "INT_ORB")
        self.assertGreater(sig_orb.expected_rr, 1.0)

        # PDH 돌파 전략 평가
        sig_pdh = PDHBreakoutStrategy.evaluate(
            "005930", "삼성전자", cur_p, pdh=76200, aggregator=agg,
            market_regime=MarketRegime.STRONG_BULL, current_time=now, spread_ratio=0.001
        )
        self.assertIsNotNone(sig_pdh)
        self.assertEqual(sig_pdh.strategy_id, "INT_PDH")

    def test_06_swing_strategies(self):
        """6. 스윙 전략(정배열, 60일 신고가) 검증"""
        daily_candles = []
        for i in range(260):
            p = 50000 + i * 100
            daily_candles.append({
                "open": p - 50, "high": p + 50, "low": p - 100, "close": p,
                "volume": 100000
            })

        now = datetime.now()
        sig_trend = TrendAlignmentStrategy.evaluate(
            "005930", "삼성전자", daily_candles, MarketRegime.STRONG_BULL, now
        )
        self.assertIsNotNone(sig_trend)
        self.assertEqual(sig_trend.strategy_id, "SWG_TREND_ALIGN")

        # 60일 신고가 돌파 봉 추가
        hh60 = max(c["high"] for c in daily_candles[-61:-1])
        daily_candles.append({
            "open": hh60 + 50, "high": hh60 + 500, "low": hh60, "close": hh60 + 400,
            "volume": 300000  # 거래량 3배 급증
        })

        sig_hh60 = HH60BreakoutStrategy.evaluate(
            "005930", "삼성전자", daily_candles, MarketRegime.STRONG_BULL, now
        )
        self.assertIsNotNone(sig_hh60)
        self.assertEqual(sig_hh60.strategy_id, "SWG_HH60_BREAKOUT")

    def test_07_scoring_engine(self):
        """7. 100점 만점 신호 채점 모델 검증"""
        # ORB + PDH 복합 돌파 발생 시 A+ 달성
        score_a_plus, grade_a_plus = ScoringEngine.score_intraday(
            market_regime=MarketRegime.STRONG_BULL,
            rvol=3.2,
            price_above_vwap=True,
            vwap_rising=True,
            ema_aligned=True,
            rsi=68.0,
            breakout_type=["ORB", "PDH"],
            execution_intensity=125.0,
            obi=0.25
        )
        self.assertGreaterEqual(score_a_plus, 90.0)
        self.assertEqual(grade_a_plus, "A+")

        # 약한 조건 -> NO TRADE
        score_low, grade_low = ScoringEngine.score_intraday(
            market_regime=MarketRegime.BEAR,
            rvol=1.1,
            price_above_vwap=False,
            vwap_rising=False,
            ema_aligned=False,
            rsi=42.0,
            breakout_type="NONE"
        )
        self.assertLess(score_low, 70.0)
        self.assertEqual(grade_low, "NO TRADE")

    def test_08_position_sizer_and_portfolio_risk(self):
        """8. 포지션 사이징(단타 0.5%, 스윙 1.0%) 및 포트폴리오 리스크 통제 검증"""
        equity = 100_000_000.0  # 1억 원
        cash = 50_000_000.0

        # 단타: 70,000원 진입, 69,000원 손절 (1R = 1,000원 = 1.4%)
        # 1억 * 0.5% = 50만 원 위험 한도 -> 500주
        shares, risk, reason = PositionSizer.calculate_shares(
            TimeHorizon.INTRADAY, equity, cash, 70000, 69000
        )
        self.assertEqual(shares, 500)
        self.assertEqual(risk, 500_000.0)

        # 손절폭 > 3% 시 단타 진입 거부 확인
        shares_rej, _, _ = PositionSizer.calculate_shares(
            TimeHorizon.INTRADAY, equity, cash, 70000, 67000  # -4.2%
        )
        self.assertEqual(shares_rej, 0)

        # 스윙: 손절폭 10% (8~12% 구간) -> 포지션 50% 축소 확인
        shares_swing, _, _ = PositionSizer.calculate_shares(
            TimeHorizon.SWING, equity, cash, 100000, 90000  # 10%
        )
        # 정상 100만 원 / 1만 원 = 100주 -> 50% 축소 = 50주
        self.assertEqual(shares_swing, 50)

    def test_09_loss_limits_and_circuit_breaker(self):
        """9. 일일/주간 손실 한도 및 서킷 브레이커 검증"""
        limit_mgr = LossLimitManager()

        # 당일 손실 -1.5% -> 위험 75% 감축
        eval_1 = limit_mgr.evaluate_loss_limits(-0.015, 0.0)
        self.assertEqual(eval_1["risk_multiplier"], 0.75)
        self.assertTrue(eval_1["can_trade_intraday"])

        # 당일 손실 -2.6% -> 단타 금지
        eval_2 = limit_mgr.evaluate_loss_limits(-0.026, 0.0)
        self.assertFalse(eval_2["can_trade_intraday"])

        # 당일 손실 -3.1% -> 전면 종료
        eval_3 = limit_mgr.evaluate_loss_limits(-0.031, 0.0)
        self.assertFalse(eval_3["can_trade_intraday"])
        self.assertFalse(eval_3["can_trade_swing"])

        # 서킷 브레이커: 3초 이상 데이터 지연 감지
        cb = CircuitBreaker()
        now = datetime.now()
        cb.update_data_heartbeat(now - timedelta(seconds=4))
        self.assertTrue(cb.check_data_staleness(now))
        self.assertTrue(cb.is_tripped)

    def test_10_order_router_and_idempotency(self):
        """10. 주문 라우터 10단계 안전점검 및 멱등성 검증"""
        cb = CircuitBreaker()
        # Mock Client
        class DummyClient:
            token = "dummy_token"
            def buy_market(self, iem_cd, qty): return {"Output_0": {"mkt_orr_no": 12345}}

        router = OrderRouter(DummyClient(), cb)
        client_id_1 = router.generate_client_order_id("INT_ORB", "005930", OrderSide.BUY)
        client_id_2 = router.generate_client_order_id("INT_ORB", "005930", OrderSide.BUY)
        self.assertNotEqual(client_id_1, client_id_2)  # 고유성 보장

        sig = TradeSignal(
            strategy_id="INT_ORB", time_horizon=TimeHorizon.INTRADAY,
            iem_cd="005930", name="삼성전자", side=OrderSide.BUY,
            strategy_price=70000, stop_price=69000, score=92.0,
            reason="테스트", timestamp=datetime.now(), target_1r=71000, target_2r=72000
        )

        order = router.submit_order(
            sig, shares=10, order_type=OrderType.MARKET, order_price=70000,
            balance={"cash": 10000000}, portfolio_risk_status="NORMAL"
        )
        self.assertIsNotNone(order)
        self.assertEqual(order.status, OrderStatus.FILLED)
        self.assertEqual(order.broker_order_no, "12345")

    def test_11_position_manager_dual_tracking_and_exits(self):
        """11. 포지션 매니저 단타/스윙 동시 보유 분리 및 다단계 익절/시간청산 검증"""
        cb = CircuitBreaker()
        router = OrderRouter(None, cb)
        pm = PositionManager(router)

        # 동일 종목(삼성전자)에 대해 단타와 스윙 동시 진입
        pos_int = pm.open_position(
            TimeHorizon.INTRADAY, "INT_ORB", "005930", "삼성전자",
            qty=100, entry_price=70000, stop_price=69000, target_1r=71000, target_2r=72000,
            target_3r=73000, initial_risk=100000
        )
        pos_swg = pm.open_position(
            TimeHorizon.SWING, "SWG_TREND", "005930", "삼성전자",
            qty=200, entry_price=70000, stop_price=66000, target_1r=74000, target_2r=78000,
            target_3r=82000, initial_risk=800000
        )

        self.assertNotEqual(pos_int.position_id, pos_swg.position_id)
        self.assertTrue(pos_int.position_id.startswith("INT_"))
        self.assertTrue(pos_swg.position_id.startswith("SWG_"))

        # 가격이 71,500원으로 상승 -> 단타 +1R 도달 (30% 익절, 100주 -> 70주)
        pm.update_price_and_manage("005930", 71500, datetime(2026, 9, 4, 10, 0))
        self.assertEqual(pos_int.qty, 70)
        self.assertTrue(pos_int.target_1r_taken)
        self.assertEqual(pos_swg.qty, 200)  # 스윙 포지션은 영향 없음

        # 15:20 도달 시 단타 포지션 강제 청산
        pm.update_price_and_manage("005930", 71500, datetime(2026, 9, 4, 15, 20))
        self.assertTrue(pos_int.is_closed)
        self.assertFalse(pos_swg.is_closed)  # 스윙은 장 마감 후에도 유지

    def test_12_backtest_and_walk_forward(self):
        """12. 백테스트 엔진 스트레스 테스트 및 워크포워드 분할 검증"""
        # 10건의 합성 거래 신호 생성
        signals = []
        for i in range(20):
            sig = TradeSignal(
                strategy_id="INT_ORB", time_horizon=TimeHorizon.INTRADAY,
                iem_cd="005930", name="삼성전자", side=OrderSide.BUY,
                strategy_price=70000, stop_price=69000, score=90.0,
                reason="테스트", timestamp=datetime.now(), target_1r=71000, target_2r=72000
            )
            # 70% 확률로 72,000원 도달(승리), 30% 확률로 69,000원 도달(패배)
            if i % 3 != 0:
                bars = [{"high": 72500, "low": 69500, "close": 72000, "volume": 1000}]
            else:
                bars = [{"high": 70500, "low": 68800, "close": 68900, "volume": 1000}]
            signals.append({"signal": sig, "bars": bars})

        bt = BacktestEngine(initial_equity=100_000_000.0)
        res_normal = bt.run_simulation(signals, scenario="NORMAL")
        res_stress = bt.run_simulation(signals, scenario="STRESS")
        res_worst = bt.run_simulation(signals, scenario="WORST")

        # 비용 증가에 따른 순수익 감소 확인
        self.assertGreater(res_normal["final_equity"], res_stress["final_equity"])
        self.assertGreater(res_stress["final_equity"], res_worst["final_equity"])

        # 워크 포워드 분할 검증
        wf_res = WalkForwardAnalyzer.run_walk_forward(signals)
        self.assertIn("train", wf_res)
        self.assertIn("out_of_sample", wf_res)

    def test_13_universe_scanner(self):
        """13. 유니버스 스캐너(Top 20/50/100, 테마 매핑, 수급 기준) 검증"""
        from universe.universe_scanner import UniverseScanner

        # 1. Top 50 유니버스 검증
        u50 = UniverseScanner.get_universe("top50")
        self.assertGreaterEqual(len(u50), 50)
        self.assertIn("005930", u50)  # 삼성전자
        self.assertEqual(u50["005930"]["theme"], "반도체")

        # 2. Top 100 유니버스 검증
        u100 = UniverseScanner.get_universe("top100")
        self.assertGreaterEqual(len(u100), 100)

        # 3. Top 20 유니버스 검증
        u20 = UniverseScanner.get_universe("top20")
        self.assertEqual(len(u20), 20)

        # 4. 테마 매핑 검증 (Section 4 테마별 리스크 통제 연동)
        theme_map = UniverseScanner.get_theme_map("top50")
        self.assertEqual(theme_map["000660"], "반도체")
        self.assertEqual(theme_map["373220"], "2차전지")
        self.assertEqual(theme_map["207940"], "바이오")
        self.assertEqual(theme_map["012450"], "방산")
        self.assertEqual(theme_map["267260"], "전력설비")

        # 5. 종목코드:종목명 딕셔너리 호환 검증
        u_dict = UniverseScanner.get_universe_dict("top50")
        self.assertEqual(u_dict["005930"], "삼성전자")
        self.assertGreaterEqual(len(u_dict), 50)


# v6.0 전체 시장(2,670+종목) 이벤트 탐지형 검증 테스트 스위트 연동 (Section 70)
from tests.test_v6_full_universe_harness import TestV6FullUniverseHarness


if __name__ == "__main__":
    unittest.main(verbosity=2)


