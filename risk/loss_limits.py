"""일일 및 주간 손실 한도 & 재진입 제한 엔진
- 일일 손실 한도: -1% (위험 75%), -2% (위험 50%), -2.5% (단타 금지), -3% (당일 매매 종료)
- 주간 손실 한도: -4% (위험 50%), -6% (포지션 최소화), -8% (전면 중단)
- 종목별 재진입 제한: 1회 손절(재진입 허용), 2회 손절(30분 쿨다운), 3회 손절(당일 영구 제외)
"""

from datetime import datetime, timedelta
from typing import Dict, Tuple, Any
from config.settings import (
    DAILY_LOSS_STEP1, DAILY_LOSS_STEP2, DAILY_LOSS_STEP3, DAILY_LOSS_STEP4,
    WEEKLY_LOSS_STEP1, WEEKLY_LOSS_STEP2, WEEKLY_LOSS_STEP3,
    MAX_LOSS_COUNT_PER_STOCK, LOSS_COOLDOWN_SECONDS
)


class LossLimitManager:
    def __init__(self):
        # 종목별 손절 이력: {iem_cd: {"count": int, "last_loss_time": datetime}}
        self.stock_loss_history: Dict[str, Dict[str, Any]] = {}

    def evaluate_loss_limits(
        self,
        daily_pnl_ratio: float,
        weekly_pnl_ratio: float
    ) -> Dict[str, Any]:
        """
        일일 및 주간 누적 손익률을 바탕으로 매매 허용 여부 및 리스크 감축 계수 산출
        :param daily_pnl_ratio: 당일 실현+평가 손익률 (예: -0.015 = -1.5%)
        :param weekly_pnl_ratio: 주간 누적 손익률
        :return: {"can_trade_intraday": bool, "can_trade_swing": bool, "risk_multiplier": float, "status": str}
        """
        risk_multiplier = 1.0
        can_intraday = True
        can_swing = True
        messages = []

        # 1. 일일 손실 점검
        if daily_pnl_ratio <= DAILY_LOSS_STEP4:  # -3.0%
            return {
                "can_trade_intraday": False,
                "can_trade_swing": False,
                "risk_multiplier": 0.0,
                "status": f"🚨 [일일 손실 차단] -3% 초과 ({daily_pnl_ratio*100:.2f}%) -> 당일 자동매매 전면 종료"
            }
        elif daily_pnl_ratio <= DAILY_LOSS_STEP3:  # -2.5%
            can_intraday = False
            risk_multiplier = min(risk_multiplier, 0.50)
            messages.append(f"일일 손실 -2.5% 초과 ({daily_pnl_ratio*100:.2f}%) -> 신규 단타 금지")
        elif daily_pnl_ratio <= DAILY_LOSS_STEP2:  # -2.0%
            risk_multiplier = min(risk_multiplier, 0.50)
            messages.append(f"일일 손실 -2.0% 초과 ({daily_pnl_ratio*100:.2f}%) -> 리스크 50% 감축")
        elif daily_pnl_ratio <= DAILY_LOSS_STEP1:  # -1.0%
            risk_multiplier = min(risk_multiplier, 0.75)
            messages.append(f"일일 손실 -1.0% 초과 ({daily_pnl_ratio*100:.2f}%) -> 리스크 75% 감축")

        # 2. 주간 손실 점검
        if weekly_pnl_ratio <= WEEKLY_LOSS_STEP3:  # -8.0%
            return {
                "can_trade_intraday": False,
                "can_trade_swing": False,
                "risk_multiplier": 0.0,
                "status": f"🚨 [주간 손실 차단] -8% 초과 ({weekly_pnl_ratio*100:.2f}%) -> 자동매매 전면 중단"
            }
        elif weekly_pnl_ratio <= WEEKLY_LOSS_STEP2:  # -6.0%
            risk_multiplier = min(risk_multiplier, 0.25)
            messages.append(f"주간 손실 -6.0% 초과 ({weekly_pnl_ratio*100:.2f}%) -> 신규 포지션 최소화(25%)")
        elif weekly_pnl_ratio <= WEEKLY_LOSS_STEP1:  # -4.0%
            risk_multiplier = min(risk_multiplier, 0.50)
            messages.append(f"주간 손실 -4.0% 초과 ({weekly_pnl_ratio*100:.2f}%) -> 리스크 50% 감축")

        status = " / ".join(messages) if messages else "정상 (손실 한도 범위 내)"
        return {
            "can_trade_intraday": can_intraday,
            "can_trade_swing": can_swing,
            "risk_multiplier": risk_multiplier,
            "status": status
        }

    def record_loss(self, iem_cd: str, loss_time: datetime):
        """손절 발생 시 이력 기록"""
        entry = self.stock_loss_history.setdefault(iem_cd, {"count": 0, "last_loss_time": loss_time})
        entry["count"] += 1
        entry["last_loss_time"] = loss_time

    def can_reenter_stock(self, iem_cd: str, current_time: datetime) -> Tuple[bool, str]:
        """
        종목별 재진입 허용 여부 판별
        - 1회 손절: 새 셋업 발생 시 즉시 허용
        - 2회 손절: 30분(1800초) 쿨다운
        - 3회 손절: 당일 해당 종목 매매 영구 제외
        """
        entry = self.stock_loss_history.get(iem_cd)
        if not entry:
            return True, "손절 이력 없음 (정상 진입 가능)"

        count = entry["count"]
        last_time = entry["last_loss_time"]

        if count >= MAX_LOSS_COUNT_PER_STOCK:
            return False, f"당일 {count}회 손절로 거래 차단 (최대 {MAX_LOSS_COUNT_PER_STOCK}회)"

        if count == 2:
            cooldown_end = last_time + timedelta(seconds=LOSS_COOLDOWN_SECONDS)
            if current_time < cooldown_end:
                remaining_mins = int((cooldown_end - current_time).total_seconds() / 60)
                return False, f"2회 손절 후 쿨다운 진행 중 (잔여: {remaining_mins}분)"

        return True, f"{count}회 손절 후 재진입 조건 충족"
