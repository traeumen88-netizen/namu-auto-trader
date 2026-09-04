"""Triple Barrier Labeling & Excursion Engine (v7.0)
- Execution-Grade Specification v7.0 (Section 6, 11, 12 준수)
- Upper Barrier (목표가), Lower Barrier (손절가), Time Barrier (보유시간 제한)
- MAE (최대 불리한 가격 역행), MFE (최대 유리한 가격 진행) 정밀 측정
- 거래비용(세금, 수수료, 슬리피지) 차감 Net Return 산출
"""

from enum import Enum
from dataclasses import dataclass
from typing import List, Dict, Any, Optional
import numpy as np


class BarrierOutcome(Enum):
    HIT_UPPER = 1     # 목표가 먼저 도달 (성공)
    HIT_LOWER = -1    # 손절가 먼저 도달 (손실)
    TIMEOUT = 0       # 시간 제한 만료 (청산)


@dataclass
class BarrierResult:
    outcome: BarrierOutcome
    entry_price: float
    exit_price: float
    upper_barrier: float
    lower_barrier: float
    time_limit_bars: int
    holding_bars: int
    gross_return: float
    net_return: float
    r_return: float          # R 단위 순익 (예: +1.5R, -1.0R)
    mae: float               # Maximum Adverse Excursion (%)
    mfe: float               # Maximum Favorable Excursion (%)
    mae_r: float             # MAE in R-units
    mfe_r: float             # MFE in R-units


class TripleBarrierEngine:
    @classmethod
    def evaluate_barriers(
        cls,
        entry_price: float,
        future_highs: List[float],
        future_lows: List[float],
        future_closes: List[float],
        target_r: float = 1.5,
        stop_r: float = 1.0,
        r_unit_price: float = 0.0,
        max_bars: int = 10,
        total_cost_rate: float = 0.0025  # 수수료 + 거래세 + 기본 슬리피지 합산 (0.25%)
    ) -> BarrierResult:
        """
        주어진 미래 가격 시퀀스에 대해 Triple Barrier 및 MAE/MFE 산출
        """
        if entry_price <= 0:
            entry_price = 10000.0

        if r_unit_price <= 0:
            r_unit_price = entry_price * 0.015  # 기본 1.5%

        upper_barrier = entry_price + (target_r * r_unit_price)
        lower_barrier = entry_price - (stop_r * r_unit_price)

        n_bars = min(len(future_closes), max_bars)
        if n_bars == 0:
            return BarrierResult(
                outcome=BarrierOutcome.TIMEOUT,
                entry_price=entry_price,
                exit_price=entry_price,
                upper_barrier=upper_barrier,
                lower_barrier=lower_barrier,
                time_limit_bars=max_bars,
                holding_bars=0,
                gross_return=0.0,
                net_return=-total_cost_rate,
                r_return=-total_cost_rate * entry_price / r_unit_price,
                mae=0.0,
                mfe=0.0,
                mae_r=0.0,
                mfe_r=0.0
            )

        highest_favorable = entry_price
        lowest_adverse = entry_price

        outcome = BarrierOutcome.TIMEOUT
        exit_price = future_closes[n_bars - 1]
        holding_bars = n_bars

        for i in range(n_bars):
            h = future_highs[i] if i < len(future_highs) else future_closes[i]
            l = future_lows[i] if i < len(future_lows) else future_closes[i]

            if h > highest_favorable:
                highest_favorable = h
            if l < lowest_adverse:
                lowest_adverse = l

            # 1. 상단 배리어 먼저 도달 검사
            hit_up = (h >= upper_barrier)
            hit_down = (l <= lower_barrier)

            if hit_up and not hit_down:
                outcome = BarrierOutcome.HIT_UPPER
                exit_price = upper_barrier
                holding_bars = i + 1
                break
            elif hit_down and not hit_up:
                outcome = BarrierOutcome.HIT_LOWER
                exit_price = lower_barrier
                holding_bars = i + 1
                break
            elif hit_up and hit_down:
                # 동시 도달 봉: 봉 시가 대비 종가 방향으로 판정 (보수적으로 손절 우선)
                outcome = BarrierOutcome.HIT_LOWER
                exit_price = lower_barrier
                holding_bars = i + 1
                break

        # 성과 및 MAE/MFE 계산
        gross_return = (exit_price - entry_price) / entry_price
        net_return = gross_return - total_cost_rate
        r_return = (exit_price - entry_price - (entry_price * total_cost_rate)) / r_unit_price

        mfe = (highest_favorable - entry_price) / entry_price
        mae = (entry_price - lowest_adverse) / entry_price
        mfe_r = (highest_favorable - entry_price) / r_unit_price
        mae_r = (entry_price - lowest_adverse) / r_unit_price

        return BarrierResult(
            outcome=outcome,
            entry_price=entry_price,
            exit_price=exit_price,
            upper_barrier=upper_barrier,
            lower_barrier=lower_barrier,
            time_limit_bars=max_bars,
            holding_bars=holding_bars,
            gross_return=gross_return,
            net_return=net_return,
            r_return=r_return,
            mae=mae,
            mfe=mfe,
            mae_r=mae_r,
            mfe_r=mfe_r
        )
