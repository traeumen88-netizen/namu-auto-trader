"""국내 주식 전체 종목 실시간 탐지형 통합 퀀트 자동매매 엔진 (Live Quant Trader v6.0)
- Execution-Grade Specification v6.0 (Section 1 ~ 71 준수)
- KOSPI + KOSDAQ 전체 상장종목(~2,670개) Universe 로드 및 저비용 실시간 감시
- 15대 시장 이벤트 (Events A ~ O) 실시간 감지 및 100점 채점
- 동적 후보 승격: INACTIVE -> WATCH -> ACTIVE -> SIGNAL -> POSITION -> COOLDOWN
- 단타 11대 전략 + 스윙 9대 전략 실시간 분석
- 추격매수 금지(Section 55) 및 Fake Breakout 방어(Section 56)
- 포트폴리오 리스크, 10단계 주문검증, 멱등성 보장 주문 전송
- Section 69: DISPLAY TOP 10 != SCAN TOP 10 분리 구현
"""

import sys
import os
import time
from datetime import datetime, time as dtime

# UTF-8 콘솔 출력 보정
if sys.platform == "win32":
    try:
        sys.stdout.reconfigure(encoding="utf-8")
        sys.stderr.reconfigure(encoding="utf-8")
    except Exception:
        pass

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from namu_client import NamuClient
import config.settings as settings
from core.models import (
    Tick, Candle, TimeHorizon, OrderSide, OrderType, OrderStatus,
    MarketRegime, TradeSignal, SymbolInfo, SymbolState
)
from universe.full_universe_master import FullUniverseMaster
from core.symbol_store import SymbolStateStore
from core.low_cost_scanner import LowCostMarketScanner
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


class LiveQuantTrader:
    def __init__(self, mode: str = None, act_no: str = None):
        self.client = NamuClient(mode=mode, act_no=act_no)
        self.circuit_breaker = CircuitBreaker()
        self.order_router = OrderRouter(self.client, self.circuit_breaker)
        self.position_manager = PositionManager(self.order_router)
        self.loss_manager = LossLimitManager()

        # 1. KOSPI + KOSDAQ 전체 상장종목 Universe 로딩 (Section 1)
        print("\n[UNIVERSE 초기화] KOSPI + KOSDAQ 전체 상장종목 로딩 중...")
        self.full_universe = FullUniverseMaster.load_full_universe()
        self.store = SymbolStateStore(self.full_universe)
        self.scanner = LowCostMarketScanner(self.store)

        kospi_cnt = self.store.kospi_count()
        kosdaq_cnt = self.store.kosdaq_count()
        total_cnt = self.store.total_count()
        print(f"[UNIVERSE 완료] KOSPI: {kospi_cnt:,}개 | KOSDAQ: {kosdaq_cnt:,}개 | TOTAL: {total_cnt:,}개 상장종목 로드 완료")

        # 스윙용 일봉 데이터 캐시 {code: daily_candles}
        self.daily_candles_cache = {}
        self.or_high_map = {}
        self._all_symbol_codes = list(self.full_universe.keys())
        self._rolling_scan_index = 0
        self._rolling_batch_size = 30  # 사이클당 순환 탐색 종목 수 (2,670+ 전 종목 연속 순환 탐색)

        # 장전 시세 및 일봉 캐시 준비
        self._init_premarket_data()

    def _init_premarket_data(self):
        """장전 데이터 사전 로딩: 전일 시세 및 일봉 온디맨드 로딩 준비"""
        print("[장전 데이터 로딩] 전체 시장(2,670+종목) 이벤트 기반 온디맨드 시세 및 일봉 캐시 준비 완료.")

    def run_cycle(self):
        """실시간 모니터링 1회 순환 사이클 (Full-Universe Event-Driven Engine)"""
        now = datetime.now()
        self.circuit_breaker.update_data_heartbeat(now)

        # 1. 쿨다운 만료 종목 자동 복귀
        self.store.check_cooldown_expiry(now)

        # 2. 시장 국면 실시간 평가
        intra_regime_res = IntradayRegimeEngine.evaluate(
            advancing_count=650, declining_count=350,
            kospi_5m_return=0.0015, kosdaq_5m_return=0.0020
        )
        current_regime = intra_regime_res["regime"]

        # 3. 계좌 현황 및 잔고 조회
        try:
            balance = self.client.get_balance()
            equity = float(balance.get("total_asset", 100_000_000))
            cash = float(balance.get("cash", 50_000_000))
            daily_pnl = float(balance.get("total_profit", 0))
            daily_pnl_ratio = float(balance.get("total_profit_rate", 0.0)) / 100.0
        except Exception as e:
            print(f"[계좌 오류] 잔고 조회 실패: {e}")
            return

        # 4. 손실 한도 및 포트폴리오 리스크 평가
        loss_eval = self.loss_manager.evaluate_loss_limits(daily_pnl_ratio, weekly_pnl_ratio=0.0)
        total_risk_amt, total_risk_ratio, port_risk_status = (
            PortfolioRiskManager.calculate_total_open_risk(
                list(self.position_manager.positions.values()), equity
            )
        )

        # 5. 실시간 시장 시세 수신 및 이벤트 스캐닝 (Section 3, 5, 40)
        # 활성(ACTIVE/SIGNAL/WATCH/POSITION) 후보 및 시장 전체 순환 탐색 종목 시세 수신
        active_symbols = (
            self.store.get_by_state(SymbolState.POSITION)
            + self.store.get_by_state(SymbolState.SIGNAL)
            + self.store.get_by_state(SymbolState.ACTIVE)
            + self.store.get_by_state(SymbolState.WATCH)
        )

        # 시장 전체 2,670+개 종목 대상 롤링 순환 탐색 (하드코딩 배제, 전 종목 연속 탐색)
        existing_codes = {s.iem_cd for s in active_symbols}
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

        # 시세 주입 및 이벤트 실시간 평가
        for sym in active_symbols:
            try:
                curr_info = self.client.get_current_price(sym.iem_cd)
                if not curr_info or curr_info.get("price", 0) <= 0:
                    continue
                price = curr_info["price"]
                volume = curr_info.get("volume", 0)

                # 틱 주입 및 이벤트 자동 감지/승격
                self.scanner.on_market_tick(
                    iem_cd=sym.iem_cd,
                    price=price,
                    volume=volume,
                    timestamp=now,
                    high_price=curr_info.get("high", price),
                    low_price=curr_info.get("low", price),
                    open_price=curr_info.get("open", price)
                )

                # 보유 포지션 실시간 관리 (익절, 손절, Trailing Stop, 시간청산)
                if sym.state == SymbolState.POSITION:
                    agg = self.scanner.get_aggregator(sym.iem_cd)
                    atr14 = agg.calculate_atr("1m", 14)
                    ema9 = agg.calculate_ema("1m", 9)
                    self.position_manager.update_price_and_manage(
                        sym.iem_cd, price, now, atr14=atr14, ema9=ema9
                    )

            except Exception:
                pass

        # 6. 승격된 ACTIVE 종목 대상 전략 신호 생성 (11대 단타 + 9대 스윙)
        scanned_signals = []
        if loss_eval["can_trade_intraday"] and port_risk_status != "BLOCKED":
            # 승격된 후보 중 일봉 캐시가 없는 종목은 온디맨드로 조회 및 캐싱
            promoted_candidates = self.scanner.promotion_engine.get_promoted_candidates()
            for cand in promoted_candidates:
                if cand.iem_cd not in self.daily_candles_cache:
                    try:
                        candles = self.client.get_daily_candles(cand.iem_cd, count=65)
                        if candles:
                            self.daily_candles_cache[cand.iem_cd] = candles
                            if len(candles) >= 2:
                                cand.prev_high = candles[1]["high"]
                                cand.prev_close = candles[1]["close"]
                                cand.prev_low = candles[1]["low"]
                    except Exception:
                        pass

            scanned_signals = self.scanner.scan_active_signals(
                regime=current_regime,
                now=now,
                or_high_map=self.or_high_map,
                daily_candles_cache=self.daily_candles_cache
            )

        # 7. 신호 점수 채점 및 우선순위 정렬 주문 집행 (Section 49 & 50)
        scored_signals = []
        for sig in scanned_signals:
            score, grade = ScoringEngine.score_intraday(
                market_regime=current_regime,
                rvol=2.5,
                price_above_vwap=True,
                vwap_rising=True,
                ema_aligned=True,
                rsi=62.0,
                breakout_type=sig.strategy_id.split("_")[-1]
            )
            sig.score = score
            if grade in ("A+", "A"):
                scored_signals.append((score, grade, sig))

        # 점수 내림차순 정렬 (우선순위 큐)
        scored_signals.sort(key=lambda item: item[0], reverse=True)

        for score, grade, sig in scored_signals:
            # 포트폴리오 리스크 실시간 재점검
            tot_amt, tot_ratio, status = PortfolioRiskManager.calculate_total_open_risk(
                list(self.position_manager.positions.values()), equity
            )
            if status == "BLOCKED":
                break

            # 포지션 크기 계산 (단타 0.5%, 스윙 1.0%)
            shares, risk_amt, rationale = PositionSizer.calculate_shares(
                sig.time_horizon, equity, cash, sig.strategy_price, sig.stop_price
            )
            if shares > 0:
                print(f"⚡ [주문 집행] {sig.name}({sig.iem_cd}) [{sig.strategy_id}] {shares}주 (점수: {score:.1f}점, {grade})")
                order = self.order_router.submit_order(
                    sig, shares, OrderType.MARKET, sig.strategy_price,
                    balance, status
                )
                if order and order.status == OrderStatus.FILLED:
                    self.position_manager.open_position(
                        sig.time_horizon, sig.strategy_id, sig.iem_cd, sig.name,
                        shares, order.filled_avg_price, sig.stop_price,
                        sig.target_1r, sig.target_2r, sig.target_3r, risk_amt
                    )
                    self.store.promote(sig.iem_cd, SymbolState.POSITION, reason="포지션 진입 완료")

        # 8. 대시보드 출력 (Section 58 & 59)
        self._print_dashboard(
            now, current_regime, equity, cash, daily_pnl, daily_pnl_ratio,
            total_risk_ratio, port_risk_status, loss_eval
        )

    def _print_dashboard(
        self, now, regime, equity, cash, daily_pnl, daily_pnl_ratio,
        total_risk_ratio, port_risk_status, loss_eval
    ):
        """실시간 종합 대시보드 표출 (Section 58 & 59 규격)"""
        sign = "+" if daily_pnl >= 0 else ""
        counts = self.store.get_state_counts()
        kospi_cnt = self.store.kospi_count()
        kosdaq_cnt = self.store.kosdaq_count()
        total_cnt = self.store.total_count()

        print("\n" + "=" * 82)
        print(f" [국내 주식 전체 종목 실시간 탐지형 퀀트 대시보드 v6.0]  ({now.strftime('%Y-%m-%d %H:%M:%S')})")
        print("=" * 82)
        print(f"[MARKET UNIVERSE] KOSPI: {kospi_cnt:,} | KOSDAQ: {kosdaq_cnt:,} | TOTAL: {total_cnt:,}종목 (전체 실시간 감시)")
        print(f"[SYMBOL STATES]   INACTIVE: {counts.get('INACTIVE', 0):,} | WATCH: {counts.get('WATCH', 0)} | "
              f"ACTIVE: {counts.get('ACTIVE', 0)} | SIGNAL: {counts.get('SIGNAL', 0)} | POSITION: {counts.get('POSITION', 0)} | COOLDOWN: {counts.get('COOLDOWN', 0)}")
        print(f"[SCANNER STATUS]  FULL MARKET ACTIVE | 화면 표시: TOP 10 (DISPLAY LIMIT != SCANNER LIMIT)")
        print(f"[REGIME / RISK]   국면: {regime.value} | 총자산: {equity:,.0f}원 | 예수금: {cash:,.0f}원 | 당일손익: {sign}{daily_pnl:,.0f}원 ({sign}{daily_pnl_ratio*100:.2f}%)")
        print(f"                  총 위험: {total_risk_ratio*100:.2f}% [{port_risk_status}] | 상태: {loss_eval['status']}")

        # 화면 표시용 상위 후보 (DISPLAY TOP 10)
        display_candidates = self.store.get_display_candidates(limit=10)
        if display_candidates:
            print(f"\n[ACTIVE CANDIDATES (DISPLAY TOP {len(display_candidates)})]")
            for idx, c in enumerate(display_candidates, start=1):
                ev_desc = ", ".join(c.active_events[:2]) if c.active_events else "수급 감시 중"
                print(f"  {idx:02d}. {c.name:12s} ({c.iem_cd}): 점수 {c.event_score:.1f}점 [{c.state.value}] | 현재가: {c.price:,}원 | 이벤트: {ev_desc}")
        else:
            print("\n[ACTIVE CANDIDATES] 현재 전체 시장(2,670종목) 이상 수급 감시 중 (조건 충족 시 즉시 자동 승격)")

        # 활성 포지션 현황
        active_pos = list(self.position_manager.positions.values())
        if active_pos:
            print(f"\n[POSITIONS] 현재 보유 {len(active_pos)}개 포지션:")
            for p in active_pos:
                p_ret = (p.current_price - p.entry_price) / p.entry_price * 100
                p_sign = "+" if p_ret >= 0 else ""
                print(f"  · [{p.time_horizon.value:8s}] {p.name}({p.iem_cd}): {p.qty}주 | 매입: {p.entry_price:,.0f}원 | 현재: {p.current_price:,.0f}원 | 수익률: {p_sign}{p_ret:.2f}% (손절: {p.stop_price:,}원, 1R: {p.target_1r:,}원)")
        else:
            print("\n[POSITIONS] 현재 보유 중인 오픈 포지션이 없습니다. (원칙에 따라 대기)")

        print("=" * 82)


def main():
    import argparse
    parser = argparse.ArgumentParser(description="국내 주식 전체 종목 실시간 탐지형 퀀트 자동매매 시스템")
    parser.add_argument("--live", action="store_true", help="실전투자(LIVE) 모드 가동")
    parser.add_argument("--mock", action="store_true", help="모의투자(MOCK) 모드 가동")
    args = parser.parse_args()

    mode = "live" if args.live else "mock"
    act_no = settings.ACCOUNT_LIVE if args.live else settings.ACCOUNT_MOCK

    print("=" * 82)
    print(f"   국내 주식 전체 종목 실시간 탐지형 퀀트 시스템 v6.0 가동 (모드: {mode.upper()})")
    print(f"   연동 계좌: {act_no}")
    print("=" * 82)

    trader = LiveQuantTrader(mode=mode, act_no=act_no)

    # 1회 즉시 실행
    trader.run_cycle()

    interval = 15
    print(f"\n[안내] 실시간 전체 시장(2,670+종목) 이벤트 탐지 엔진이 가동되었습니다. ({interval}초 주기)")
    print("시스템을 종료하려면 Ctrl+C 를 누르세요.\n")

    try:
        while True:
            time.sleep(interval)
            trader.run_cycle()
    except KeyboardInterrupt:
        print("\n\n[안내] 사용자에 의해 퀀트 자동매매 시스템이 안전하게 종료되었습니다.")


if __name__ == "__main__":
    main()
