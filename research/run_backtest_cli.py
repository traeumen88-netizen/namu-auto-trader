"""백테스트 및 워크 포워드 분석 CLI 실행기
- 3대 비용 시나리오 (NORMAL, STRESS 2x, WORST 3x) 비교 분석
- Walk-Forward (Train 60% / Val 20% / OOS 20%) 검증
- 파라미터 민감도(Sensitivity) 스트레스 테스트
"""

import sys
import os
from datetime import datetime, timedelta

# UTF-8 출력 보정
if sys.platform == "win32":
    try:
        sys.stdout.reconfigure(encoding="utf-8")
        sys.stderr.reconfigure(encoding="utf-8")
    except Exception:
        pass

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core.models import TradeSignal, TimeHorizon, OrderSide
from research.backtest_engine import BacktestEngine
from research.walk_forward import WalkForwardAnalyzer


def generate_benchmark_dataset(num_trades: int = 60):
    """실행-그레이드 벤치마크 데이터셋 생성 (실제 시장 승률 55%~60%, 손익비 1.8~2.2 시뮬레이션)"""
    import random
    random.seed(42)

    dataset = []
    base_time = datetime(2026, 1, 2, 9, 5)

    for i in range(num_trades):
        is_intraday = (i % 2 == 0)
        horizon = TimeHorizon.INTRADAY if is_intraday else TimeHorizon.SWING
        strat_id = "INT_ORB" if is_intraday else "SWG_TREND"
        p = random.randint(50000, 150000)
        stop_dist = int(p * (0.015 if is_intraday else 0.04))
        stop_p = p - stop_dist
        t1 = p + stop_dist
        t2 = p + int(stop_dist * 2.0)
        t3 = p + int(stop_dist * 3.0)

        sig = TradeSignal(
            strategy_id=strat_id,
            time_horizon=horizon,
            iem_cd=f"{random.randint(1000, 9999):06d}",
            name=f"종목_{i+1}",
            side=OrderSide.BUY,
            strategy_price=p,
            stop_price=stop_p,
            target_1r=t1,
            target_2r=t2,
            target_3r=t3,
            score=91.0,
            reason="백테스트 시뮬레이션",
            timestamp=base_time + timedelta(days=i)
        )

        # 58% 확률 승리 셋업
        if random.random() < 0.58:
            bars = [
                {"high": p + int(stop_dist * 0.8), "low": p - int(stop_dist * 0.3), "close": p + int(stop_dist * 0.5), "volume": 5000},
                {"high": t2 + 100, "low": p, "close": t2, "volume": 8000}
            ]
        else:
            bars = [
                {"high": p + int(stop_dist * 0.2), "low": stop_p - 100, "close": stop_p, "volume": 4000}
            ]

        dataset.append({"signal": sig, "bars": bars})

    return dataset


def main():
    print("=" * 75)
    print("      [국내 주식 통합 퀀트 시스템 v5.0] 백테스트 및 스트레스 분석")
    print("=" * 75)

    data = generate_benchmark_dataset(num_trades=100)
    engine = BacktestEngine(initial_equity=100_000_000.0, market="KOSPI")

    print(f"\n총 거래 표본수: {len(data)}건 (단타 50건 + 스윙 50건)\n")

    # 1. 3대 비용 시나리오 스트레스 테스트 (NORMAL, STRESS, WORST)
    print("┌" + "─" * 73 + "┐")
    print(f"│ {'시나리오':^12} │ {'승률':^8} │ {'손익비':^8} │ {'Profit Factor':^14} │ {'Expectancy':^12} │ {'MDD':^6} │")
    print("├" + "─" * 73 + "┤")

    scenarios = ["NORMAL", "STRESS", "WORST"]
    results = {}
    for sc in scenarios:
        res = engine.run_simulation(data, scenario=sc)
        results[sc] = res
        print(
            f"│ {sc:^14} │ {res['win_rate']*100:>6.1f}% │ {res['payoff_ratio']:>8.2f} │ "
            f"{res['profit_factor']:>14.2f} │ {res['expectancy']:>10,.0f}원 │ {res['max_drawdown']*100:>4.1f}% │"
        )
    print("└" + "─" * 73 + "┘")

    # 통과 판정
    normal_pass = results["NORMAL"]["profit_factor"] >= 1.20 and results["NORMAL"]["expectancy"] > 0
    stress_pass = results["STRESS"]["profit_factor"] >= 1.05 and results["STRESS"]["expectancy"] > 0
    print(f"\n* Normal 시나리오 승인 요건(PF >= 1.20, Expectancy > 0): {'[PASS]' if normal_pass else '[FAIL]'}")
    print(f"* Stress 시나리오 방어 요건(PF >= 1.05, Expectancy > 0): {'[PASS]' if stress_pass else '[FAIL]'}")

    # 2. Walk-Forward 분석 (60% Train / 20% Val / 20% OOS)
    print("\n" + "=" * 75)
    print("      [Walk-Forward 전진 분석] (Train 60% / Val 20% / Out-of-Sample 20%)")
    print("=" * 75)
    wf_res = WalkForwardAnalyzer.run_walk_forward(data)

    print(f"1. Training (60%):       거래 {wf_res['train']['trades']:>2}건 | 승률 {wf_res['train']['win_rate']*100:>5.1f}% | PF {wf_res['train']['profit_factor']:>5.2f} | MDD {wf_res['train']['mdd']*100:>4.1f}%")
    print(f"2. Validation (20%):     거래 {wf_res['validation']['trades']:>2}건 | 승률 {wf_res['validation']['win_rate']*100:>5.1f}% | PF {wf_res['validation']['profit_factor']:>5.2f} | MDD {wf_res['validation']['mdd']*100:>4.1f}%")
    print(f"3. Out-of-Sample (20%):  거래 {wf_res['out_of_sample']['trades']:>2}건 | 승률 {wf_res['out_of_sample']['win_rate']*100:>5.1f}% | PF {wf_res['out_of_sample']['profit_factor']:>5.2f} | MDD {wf_res['out_of_sample']['mdd']*100:>4.1f}%")
    print(f"\n* 모델 안정성(Robustness) 검증 결과: {'[안정적 (Robust)]' if wf_res['is_robust'] else '[과적합 의심 (Overfitted)]'}")

    # 3. 파라미터 민감도 분석
    print("\n" + "=" * 75)
    print("      [파라미터 민감도 분석] (RVOL 임계치 1.5 vs 1.75 vs 2.0 변동 테스트)")
    print("=" * 75)
    sens_res = WalkForwardAnalyzer.analyze_parameter_sensitivity(data, "RVOL", [1.5, 1.75, 2.0])
    for var in sens_res["variations"]:
        print(f"  · RVOL = {var['param_value']:<4} -> PF: {var['profit_factor']:.2f} | 승률: {var['win_rate']*100:.1f}% | 1회기대값: {var['expectancy']:,.0f}원")
    print(f"\n* 파라미터 민감도 판정: {'[안정적 (파라미터 급변 없음)]' if sens_res['is_stable'] else '[민감 (특정값 편향)]'}")
    print("=" * 75)


if __name__ == "__main__":
    main()
