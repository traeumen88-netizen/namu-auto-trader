"""KRX 규격 호가단위(Tick Size) 정규화 모듈
- 가격 구간별 KRX Tick Size Table 기반 정확한 틱 단위 변환
- 매수 지정가, 매도 지정가, 손절 매도, 익절 매도 등 방향성(Rounding Direction) 완벽 보장
"""

import math
from typing import Literal
from config.krx_constants import KRX_TICK_TABLE


def get_tick_size(price: float) -> int:
    """주어진 가격에 해당하는 KRX 정규 호가단위(Tick Size) 반환"""
    price_int = int(round(price))
    for upper_limit, tick in KRX_TICK_TABLE:
        if price_int < upper_limit:
            return tick
    return 1000


def normalize_price(
    price: float,
    side: Literal["BUY", "SELL"] = "BUY",
    purpose: Literal["LIMIT", "STOP", "PROFIT", "TRAILING"] = "LIMIT"
) -> int:
    """
    거래소 호가단위에 맞춰 가격을 정규화(Normalize)한다.

    반올림 방향 원칙:
    1. 매수 지정가 (BUY LIMIT):
       - 가격조건을 훼손하지 않는 방향 (이하로 매수해야 하므로 내림 Floor)
    2. 매도 지정가 (SELL LIMIT / PROFIT):
       - 가격조건을 훼손하지 않는 방향 (이상으로 매도해야 하므로 올림 Ceil)
    3. 손절 매도 (SELL STOP):
       - 체결 우선 방향 (더 낮은 가격으로라도 신속 체결되어야 하므로 내림 Floor)
    4. 손절 매수 (BUY STOP):
       - 체결 우선 방향 (올림 Ceil)
    5. 트레일링 스탑 (SELL TRAILING):
       - 체결 우선 방향 (내림 Floor)
    """
    if price <= 0:
        return 0

    tick = get_tick_size(price)

    if side == "BUY":
        if purpose in ("LIMIT", "PROFIT"):
            # 가격 훼손 방지: 내림
            normalized = math.floor(price / tick) * tick
        else:  # STOP (체결 우선)
            normalized = math.ceil(price / tick) * tick
    else:  # SELL
        if purpose in ("STOP", "TRAILING"):
            # 체결 우선: 내림 (즉시 체결 유도)
            normalized = math.floor(price / tick) * tick
        else:  # LIMIT, PROFIT (가격 훼손 방지: 올림)
            normalized = math.ceil(price / tick) * tick

    # 틱 변환 후 가격대의 틱이 달라질 수 있는 경계값 재검증
    new_tick = get_tick_size(normalized)
    if new_tick != tick:
        normalized = int(round(normalized / new_tick)) * new_tick

    return int(max(normalized, tick))
