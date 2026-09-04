"""자동매매 전략 및 시그널 판별 엔진
1. 래리 윌리엄스 변동성 돌파 전략 (Volatility Breakout)
2. 이동평균선(5일/20일) 골든크로스 및 RSI 보조지표 분석
3. 스마트 손익절(Stop-Loss / Take-Profit) 리스크 관리 엔진
"""

import logging
from typing import Dict, Any, List
import config

logger = logging.getLogger("Strategy")


class StrategyEngine:
    def __init__(self, client):
        self.client = client
        self.bought_today = set()  # 당일 이미 매수한 종목 (중복 매수 방지)
        self.target_cache = {}     # {iem_cd: target_dict} 당일 변동성 돌파 목표가 캐시 (API 절약)

    def check_stop_loss_and_take_profit(self, holdings: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        """
        보유 종목의 손익률을 점검하여 익절 또는 손절 매도 시그널 생성
        :return: 매도 실행해야 할 종목 리스트
        """
        sell_orders = []
        for h in holdings:
            iem_cd = h["iem_cd"]
            iem_nm = h["iem_nm"]
            profit_rate = h["profit_rate"] / 100.0  # % 단위를 소수로 변환
            qty = h["qty"]

            # 1. 손절 조건 (Stop-Loss)
            if profit_rate <= config.STOP_LOSS_RATE:
                logger.warning(f"🚨 [손절 신호 감지] {iem_nm}({iem_cd}) 수익률 {profit_rate*100:.2f}% (손절선 {config.STOP_LOSS_RATE*100:.1f}%)")
                sell_orders.append({
                    "iem_cd": iem_cd,
                    "name": iem_nm,
                    "qty": qty,
                    "reason": f"손절 (-{abs(profit_rate*100):.2f}%)",
                    "profit_rate": profit_rate
                })

            # 2. 익절 조건 (Take-Profit)
            elif profit_rate >= config.TAKE_PROFIT_RATE:
                logger.info(f"🎯 [익절 신호 감지] {iem_nm}({iem_cd}) 수익률 +{profit_rate*100:.2f}% (익절선 +{config.TAKE_PROFIT_RATE*100:.1f}%)")
                sell_orders.append({
                    "iem_cd": iem_cd,
                    "name": iem_nm,
                    "qty": qty,
                    "reason": f"익절 (+{profit_rate*100:.2f}%)",
                    "profit_rate": profit_rate
                })

        return sell_orders

    def calculate_volatility_target(self, iem_cd: str, k: float = 0.5) -> Dict[str, Any]:
        """
        변동성 돌파 전략 목표 매수가 산출:
        목표가 = 당일 시가 + (전일 고가 - 전일 저가) * K
        (장중 당일 목표가는 변하지 않으므로 캐시하여 API 과도 호출 방지)
        """
        if iem_cd in self.target_cache:
            return self.target_cache[iem_cd]

        candles = self.client.get_daily_candles(iem_cd, count=5)
        if len(candles) < 2:
            return None
        
        # candles[0] = 당일(오늘), candles[1] = 전일(어제)
        today = candles[0]
        yesterday = candles[1]

        prev_range = yesterday["high"] - yesterday["low"]
        target_price = int(today["open"] + (prev_range * k))

        res = {
            "iem_cd": iem_cd,
            "today_open": today["open"],
            "prev_high": yesterday["high"],
            "prev_low": yesterday["low"],
            "prev_range": prev_range,
            "target_price": target_price,
            "k": k
        }
        if today["open"] > 0 and target_price > 0:
            self.target_cache[iem_cd] = res
        return res

    def check_buy_signal(self, iem_cd: str, curr_price_info: Dict[str, Any], v_info: Dict[str, Any] = None) -> Dict[str, Any]:
        """
        매수 진입 조건 판별 (변동성 돌파 전략 기준)
        """
        if iem_cd in self.bought_today:
            return None  # 오늘 이미 매수 완료된 종목 건너뜀

        if v_info is None:
            v_info = self.calculate_volatility_target(iem_cd, k=0.5)

        if not v_info:
            return None

        current_price = curr_price_info["price"]
        target_price = v_info["target_price"]

        # 현재가가 돌파 목표가 이상이고, 당일 시가보다 상승 중일 때 매수
        if current_price >= target_price and current_price > curr_price_info["open"]:
            # 매수 수량 산출 (1종목당 최대 설정금액 기준)
            invest_limit = config.MAX_INVEST_PER_STOCK
            buy_qty = int(invest_limit // current_price)

            if buy_qty > 0:
                return {
                    "iem_cd": iem_cd,
                    "name": curr_price_info["name"],
                    "current_price": current_price,
                    "target_price": target_price,
                    "qty": buy_qty,
                    "reason": f"변동성 돌파 (현재가 {current_price:,}원 >= 목표가 {target_price:,}원)"
                }

        return None
