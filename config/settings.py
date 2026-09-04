"""통합 퀀트 트레이딩 시스템 글로벌 설정 (INTRADAY + SWING v5.0)
- 모든 파라미터는 수치 조건으로 관리되며 하드코딩을 배제함.
"""

import os
from pathlib import Path
from dotenv import load_dotenv

BASE_DIR = Path(__file__).resolve().parent.parent
load_dotenv(BASE_DIR / ".env", override=True)

# 1. API 자격 증명 및 도메인
APP_KEY = os.getenv("NHPLUG_APP_KEY", "")
APP_SECRET = os.getenv("NHPLUG_APP_SECRET", "")
TRADING_MODE = os.getenv("TRADING_MODE", "mock").lower().strip()

ACCOUNT_MOCK = os.getenv("ACCOUNT_MOCK", "50001003032")
ACCOUNT_LIVE = os.getenv("ACCOUNT_LIVE", "20201549311")
ACCOUNT_NO = ACCOUNT_LIVE if TRADING_MODE == "live" else ACCOUNT_MOCK

QUOTE_BASE_URL = "https://api.nhplug.com:8443"
TRADE_BASE_URL = "https://api.nhplug.com:8443" if TRADING_MODE == "live" else "https://moapi.nhplug.com:8443"
BASE_URL = TRADE_BASE_URL
MODE_NAME = "실전투자 (LIVE)" if TRADING_MODE == "live" else "모의투자 (MOCK)"
DRY_RUN = os.getenv("DRY_RUN", "false").lower() == "true"

TARGET_STOCKS = {
    "005930": "삼성전자",
    "000660": "SK하이닉스",
    "373220": "LG에너지솔루션",
    "207940": "삼성바이오로직스",
    "005380": "현대차",
    "035420": "NAVER",
    "000270": "기아",
    "068270": "셀트리온",
}

# 기본 손익절 및 투자한도 (main_trader 호환)
STOP_LOSS_RATE = float(os.getenv("STOP_LOSS_RATE", -0.02))
TAKE_PROFIT_RATE = float(os.getenv("TAKE_PROFIT_RATE", 0.04))
MAX_INVEST_PER_STOCK = int(os.getenv("MAX_INVEST_PER_STOCK", 1000000))

# 2. API 호출 제어 (Rate Limiter)
API_MAX_REQUESTS_PER_SECOND = 4.0  # 초당 최대 요청수
API_TIMEOUT_SECONDS = 10
API_MAX_RETRIES = 3
API_BACKOFF_FACTOR = 0.5

# 3. 데이터 감시 및 서킷 브레이커 한도
DATA_STALL_THRESHOLD_SECONDS = 3.0   # 3초 이상 실시간 데이터 지연 시 주문 중단
RECOVERY_CONFIRMATION_SECONDS = 10.0 # 복구 후 최소 10초간 안정 확인 후 재개
MAX_CONSECUTIVE_ORDER_ERRORS = 3     # 연속 주문 에러 허용 횟수

# 4. 유동성 필터 기준
LIQUIDITY_MIN_20D_TURNOVER = 5_000_000_000  # 최근 20일 평균 거래대금 >= 50억
LIQUIDITY_MIN_TODAY_TURNOVER = 2_000_000_000 # 당일 누적 거래대금 >= 20억
LIQUIDITY_MAX_SPREAD_RATIO = 0.0020          # 최우선 호가 스프레드 <= 0.20%

# 5. 자금 배분 및 리스크 관리 (Fixed Fractional Risk)
INTRADAY_RISK_PER_TRADE = 0.005  # 단타 1회 최대 Risk: 자산의 0.5%
SWING_RISK_PER_TRADE = 0.010     # 스윙 1회 최대 Risk: 자산의 1.0%

# 포트폴리오 총 리스크 한도
TOTAL_RISK_NORMAL_LIMIT = 0.03   # <= 3% : 정상 진입
TOTAL_RISK_REDUCED_LIMIT = 0.04  # 3~4%  : 신규 진입 50% 축소
TOTAL_RISK_CEILING = 0.04        # > 4%  : 신규 진입 전면 금지

# 테마 집중도 제한
MAX_STOCKS_PER_THEME = 3         # 동일 테마 최대 3종목
MAX_RISK_PER_THEME = 0.015       # 동일 테마 총 리스크 <= 1.5%

# 6. 시장국면별 자산 배분 (단타 / 스윙 / 현금)
REGIME_ALLOCATION = {
    "STRONG_BULL": {"intraday": 0.40, "swing": 0.50, "cash": 0.10},
    "BULL":        {"intraday": 0.30, "swing": 0.50, "cash": 0.20},
    "NEUTRAL":     {"intraday": 0.20, "swing": 0.30, "cash": 0.50},
    "BEAR":        {"intraday": 0.10, "swing": 0.00, "cash": 0.90},
    "PANIC":       {"intraday": 0.00, "swing": 0.00, "cash": 1.00},
}

# 7. 일일 / 주간 누적 손실 제한
DAILY_LOSS_STEP1 = -0.010  # -1.0% 도달 시 -> Risk 75%로 축소
DAILY_LOSS_STEP2 = -0.020  # -2.0% 도달 시 -> Risk 50%로 축소
DAILY_LOSS_STEP3 = -0.025  # -2.5% 도달 시 -> 신규 단타 전면 금지
DAILY_LOSS_STEP4 = -0.030  # -3.0% 도달 시 -> 당일 모든 매매 전면 종료

WEEKLY_LOSS_STEP1 = -0.040 # -4.0% 도달 시 -> Risk 50% 축소
WEEKLY_LOSS_STEP2 = -0.060 # -6.0% 도달 시 -> 신규 포지션 최소화
WEEKLY_LOSS_STEP3 = -0.080 # -8.0% 도달 시 -> 자동매매 전면 중단

# 8. 단타 매매 시간 규정
TIME_ORB_START = "09:00:00"
TIME_ORB_END = "09:05:00"
TIME_INTRADAY_ENTRY_END = "10:30:00"
TIME_INTRADAY_UNWIND_START = "15:10:00"
TIME_INTRADAY_FORCE_CLOSE = "15:20:00"

# 9. 재진입 및 쿨다운 규칙
MAX_LOSS_COUNT_PER_STOCK = 3  # 동일 종목 3회 손절 시 당일 해당 종목 퇴출
LOSS_COOLDOWN_SECONDS = 1800  # 2회 손절 시 30분 거래 중단

# 10. 초기 Watchlist 후보군
WATCHLIST_DEFAULTS = [
    "005930",  # 삼성전자
    "000660",  # SK하이닉스
    "373220",  # LG에너지솔루션
    "207940",  # 삼성바이오로직스
    "005380",  # 현대차
    "035420",  # NAVER
    "000270",  # 기아
    "068270",  # 셀트리온
    "035720",  # 카카오
    "105560",  # KB금융
]
