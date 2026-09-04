"""KRX 거래소 규격 상수 및 비용 테이블
- KRX 호가단위(Tick Size) 테이블
- 거래세/수수료 테이블 (시장/상품/일자별 유연 구성)
- 슬리피지 예산 및 스트레스 시나리오 배수
"""

from dataclasses import dataclass
from typing import List, Tuple

# 1. KRX 호가단위(Tick Size) 테이블: (상한가 미만, 호가단위)
# 기준: KRX 업무규정 시행세칙
KRX_TICK_TABLE: List[Tuple[int, int]] = [
    (2000, 1),         # 2,000원 미만 -> 1원
    (5000, 5),         # 2,000원 이상 ~ 5,000원 미만 -> 5원
    (20000, 10),       # 5,000원 이상 ~ 20,000원 미만 -> 10원
    (50000, 50),       # 20,000원 이상 ~ 50,000원 미만 -> 50원
    (200000, 100),     # 50,000원 이상 ~ 200,000원 미만 -> 100원
    (500000, 500),     # 200,000원 이상 ~ 500,000원 미만 -> 500원
    (float("inf"), 1000) # 500,000원 이상 -> 1,000원
]

# 2. 거래비용 세부 설정 테이블
@dataclass(frozen=True)
class CostSchedule:
    market: str          # KOSPI / KOSDAQ / ETF
    broker_fee_rate: float       # 위탁수수료율 (예: 0.0001 = 0.01%)
    transaction_tax_rate: float  # 증권거래세율
    agricultural_tax_rate: float # 농어촌특별세율
    base_slippage_rate: float    # 기본 슬리피지 (0.00075 = 0.075%)

# 2026년 기준 법령 및 증권사 표준 수수료 테이블
COST_SCHEDULES = {
    "KOSPI": CostSchedule(
        market="KOSPI",
        broker_fee_rate=0.0001,       # 0.01%
        transaction_tax_rate=0.0000,  # 0.00%
        agricultural_tax_rate=0.0015, # 0.15% (농특세)
        base_slippage_rate=0.00075    # 0.075%
    ),
    "KOSDAQ": CostSchedule(
        market="KOSDAQ",
        broker_fee_rate=0.0001,       # 0.01%
        transaction_tax_rate=0.0015,  # 0.15%
        agricultural_tax_rate=0.0000, # 0.00%
        base_slippage_rate=0.0010     # 0.10%
    ),
    "ETF": CostSchedule(
        market="ETF",
        broker_fee_rate=0.0001,       # 0.01%
        transaction_tax_rate=0.0000,  # 거래세 면제
        agricultural_tax_rate=0.0000,
        base_slippage_rate=0.0005     # 0.05%
    )
}

# 3. 비용 스트레스 시나리오 배수
STRESS_SCENARIOS = {
    "NORMAL": 1.0,   # 기본 슬리피지
    "STRESS": 2.0,   # 슬리피지 2배
    "WORST": 3.0     # 슬리피지 3배
}

# 4. 슬리피지 허용 예산 (Slippage Budget)
MAX_SLIPPAGE_BUDGET = 0.0015  # 0.15% 초과 시 주문 거부
