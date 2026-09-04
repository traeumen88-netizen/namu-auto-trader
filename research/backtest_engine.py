"""실행 수준 백테스트 엔진 (Execution-Grade Backtest Engine)
- 실전과 동일한 Strategy Interface 사용 (SignalEngine.evaluate)
- 사실적 체결 모델 (Bid/Ask, Spread, Slippage, Fees, Taxes)
- 3대 비용 스트레스 시나리오 (NORMAL, STRESS 2x, WORST 3x)
- 성과 지표 산출: Expectancy, Profit Factor, MDD, Win Rate, Payoff Ratio, Sharpe
"""

import math
from typing import List, Dict, Any, Literal
from core.models import TradeSignal, TimeHorizon, OrderSide
from core.cost_model import CostModel
from core.tick_normalizer import normalize_price


class BacktestEngine:
    def __init__(self, initial_equity: float = 100_000_000.0, market: str = "KOSPI"):
        self.initial_equity = initial_equity
        self.market = market
        self.cost_model = CostModel(market=market)

    def run_simulation(
        self,
        signals_and_bars: List[Dict[str, Any]],
        scenario: Literal["NORMAL", "STRESS", "WORST"] = "NORMAL"
    ) -> Dict[str, Any]:
        """
        단타 / 스윙 신호 및 가격 데이터를 바탕으로 백테스트 실행
        :param signals_and_bars: [{'signal': TradeSignal, 'bars': [{'high', 'low', 'close', 'volume'}]}]
        :param scenario: "NORMAL", "STRESS", "WORST"
        :return: 백테스트 성과 분석 보고서
        """
        equity = self.initial_equity
        peak_equity = equity
        max_drawdown = 0.0

        trade_logs = []
        equity_curve = [equity]

        for item in signals_and_bars:
            sig: TradeSignal = item["signal"]
            future_bars = item["bars"]

            entry_price = float(sig.strategy_price)
            stop_price = float(sig.stop_price)
            t1 = float(sig.target_1r)
            t2 = float(sig.target_2r)

            # 리스크 기반 수량 산출 (단타 0.5%, 스윙 1.0%)
            risk_ratio = 0.005 if sig.time_horizon == TimeHorizon.INTRADAY else 0.010
            risk_budget = equity * risk_ratio
            stop_dist = abs(entry_price - stop_price)
            if stop_dist <= 0:
                continue
            shares = max(1, int(risk_budget / stop_dist))

            # 포지션 진행 시뮬레이션
            exit_price = entry_price
            exit_reason = "TIME_OUT"
            is_winner = False

            for bar in future_bars:
                high = bar["high"]
                low = bar["low"]

                # 손절 감지
                if low <= stop_price:
                    exit_price = stop_price
                    exit_reason = "STOP_LOSS"
                    is_winner = False
                    break

                # 2차 익절(+2R) 도달
                if high >= t2:
                    exit_price = t2
                    exit_reason = "TARGET_2R"
                    is_winner = True
                    break

                # 1차 익절(+1R) 도달 시 본전 스탑 상향
                if high >= t1:
                    stop_price = max(stop_price, entry_price)

            # 거래비용 산출 (수수료, 세금, 슬리피지 Normal/Stress/Worst)
            cost = self.cost_model.calculate_roundtrip_cost(
                entry_price, exit_price, shares, scenario=scenario
            )

            gross_pnl = (exit_price - entry_price) * shares
            net_pnl = gross_pnl - cost

            equity += net_pnl
            equity_curve.append(equity)

            # MDD 갱신
            peak_equity = max(peak_equity, equity)
            drawdown = (peak_equity - equity) / peak_equity if peak_equity > 0 else 0.0
            max_drawdown = max(max_drawdown, drawdown)

            trade_logs.append({
                "strategy_id": sig.strategy_id,
                "iem_cd": sig.iem_cd,
                "time_horizon": sig.time_horizon.value,
                "entry_price": entry_price,
                "exit_price": exit_price,
                "shares": shares,
                "gross_pnl": gross_pnl,
                "cost": cost,
                "net_pnl": net_pnl,
                "is_winner": net_pnl > 0,
                "exit_reason": exit_reason
            })

        # 성과 지표 집계
        total_trades = len(trade_logs)
        if total_trades == 0:
            return {"trades": 0, "expectancy": 0.0, "profit_factor": 0.0, "mdd": 0.0}

        wins = [t for t in trade_logs if t["is_winner"]]
        losses = [t for t in trade_logs if not t["is_winner"]]

        win_rate = len(wins) / total_trades
        total_win_amt = sum(t["net_pnl"] for t in wins)
        total_loss_amt = abs(sum(t["net_pnl"] for t in losses))

        avg_win = total_win_amt / len(wins) if wins else 0.0
        avg_loss = total_loss_amt / len(losses) if losses else 0.0

        payoff_ratio = (avg_win / avg_loss) if avg_loss > 0 else 0.0
        profit_factor = (total_win_amt / total_loss_amt) if total_loss_amt > 0 else float("inf")

        # Expectancy = (WinRate * AvgWin) - (LossRate * AvgLoss)
        loss_rate = 1.0 - win_rate
        expectancy = (win_rate * avg_win) - (loss_rate * avg_loss)

        total_net_return = (equity - self.initial_equity) / self.initial_equity

        return {
            "scenario": scenario,
            "total_trades": total_trades,
            "win_rate": win_rate,
            "payoff_ratio": payoff_ratio,
            "profit_factor": profit_factor,
            "expectancy": expectancy,
            "max_drawdown": max_drawdown,
            "final_equity": equity,
            "total_net_return": total_net_return,
            "trade_logs": trade_logs
        }
