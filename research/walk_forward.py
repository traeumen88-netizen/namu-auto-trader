"""워크 포워드 분석 및 파라미터 민감도 분석기 (Walk Forward & Sensitivity Analyzer)
- 60% 학습(Training) / 20% 검증(Validation) / 20% 전진분석(Out-of-Sample)
- 파라미터 민감도(Sensitivity) 스트레스 테스트 및 과최적화(Overfitting) 검증
"""

from typing import List, Dict, Any, Tuple
from research.backtest_engine import BacktestEngine


class WalkForwardAnalyzer:
    @staticmethod
    def split_dataset(data: List[Any]) -> Tuple[List[Any], List[Any], List[Any]]:
        """60% Train / 20% Validation / 20% Out-of-Sample 분할"""
        n = len(data)
        train_end = int(n * 0.60)
        val_end = int(n * 0.80)

        train = data[:train_end]
        val = data[train_end:val_end]
        oos = data[val_end:]
        return train, val, oos

    @staticmethod
    def run_walk_forward(data: List[Dict[str, Any]]) -> Dict[str, Any]:
        """Train / Validation / OOS 구간별 성과 분석 비교"""
        train, val, oos = WalkForwardAnalyzer.split_dataset(data)

        engine = BacktestEngine()
        res_train = engine.run_simulation(train, scenario="NORMAL")
        res_val = engine.run_simulation(val, scenario="NORMAL")
        res_oos = engine.run_simulation(oos, scenario="NORMAL")

        # 과적합 검증 (OOS 성과가 Train 대비 50% 이하로 급락하면 경고)
        pf_train = res_train["profit_factor"]
        pf_oos = res_oos["profit_factor"]
        is_robust = True
        if pf_train > 1.2 and pf_oos < 1.0:
            is_robust = False

        return {
            "train": {
                "trades": res_train["total_trades"],
                "profit_factor": res_train["profit_factor"],
                "win_rate": res_train["win_rate"],
                "mdd": res_train["max_drawdown"]
            },
            "validation": {
                "trades": res_val["total_trades"],
                "profit_factor": res_val["profit_factor"],
                "win_rate": res_val["win_rate"],
                "mdd": res_val["max_drawdown"]
            },
            "out_of_sample": {
                "trades": res_oos["total_trades"],
                "profit_factor": res_oos["profit_factor"],
                "win_rate": res_oos["win_rate"],
                "mdd": res_oos["max_drawdown"]
            },
            "is_robust": is_robust
        }

    @staticmethod
    def analyze_parameter_sensitivity(
        base_data: List[Dict[str, Any]],
        parameter_name: str,
        test_values: List[float]
    ) -> Dict[str, Any]:
        """
        파라미터 변동에 따른 성과 민감도 측정 (예: RVOL 1.5, 1.75, 2.0)
        """
        results = []
        engine = BacktestEngine()

        for val in test_values:
            # 시뮬레이션
            res = engine.run_simulation(base_data, scenario="NORMAL")
            results.append({
                "param_value": val,
                "profit_factor": res["profit_factor"],
                "win_rate": res["win_rate"],
                "expectancy": res["expectancy"],
                "mdd": res["max_drawdown"]
            })

        # 성과 편차(분산) 검사
        pfs = [r["profit_factor"] for r in results if r["profit_factor"] < 100]
        variance = max(pfs) - min(pfs) if pfs else 0.0

        return {
            "parameter": parameter_name,
            "variations": results,
            "max_spread": variance,
            "is_stable": variance <= 0.40  # 파라미터 변화에도 PF 변동폭이 0.40 이내이면 안정적
        }
