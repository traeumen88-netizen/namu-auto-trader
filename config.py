import os
from pathlib import Path
from dotenv import load_dotenv

# .env 로드
BASE_DIR = Path(__file__).resolve().parent
ENV_PATH = BASE_DIR / ".env"
load_dotenv(ENV_PATH, override=True)

# 1. API 키 설정
APP_KEY = os.getenv("NHPLUG_APP_KEY", "")
APP_SECRET = os.getenv("NHPLUG_APP_SECRET", "")

# 2. 투자 모드 ('mock' 또는 'live')
TRADING_MODE = os.getenv("TRADING_MODE", "mock").lower().strip()

# 3. 모드별 계좌번호 및 베이스 도메인 자동 매핑
if TRADING_MODE == "live":
    BASE_URL = "https://api.nhplug.com:8443"
    ACCOUNT_NO = os.getenv("ACCOUNT_LIVE", "20201549311")
    MODE_NAME = "실전투자 (LIVE)"
else:
    BASE_URL = "https://moapi.nhplug.com:8443"
    ACCOUNT_NO = os.getenv("ACCOUNT_MOCK", "50071003032")
    MODE_NAME = "모의투자 (MOCK)"

# nhplug 패키지 연동을 위해 환경변수 동기화
os.environ["NHPLUG_APP_KEY"] = APP_KEY
os.environ["NHPLUG_APP_SECRET"] = APP_SECRET
os.environ["NHPLUG_BASE_URL"] = BASE_URL

# 4. 리스크 관리 설정
STOP_LOSS_RATE = float(os.getenv("STOP_LOSS_RATE", -0.02))      # 손절선 (예: -2%)
TAKE_PROFIT_RATE = float(os.getenv("TAKE_PROFIT_RATE", 0.04))   # 익절선 (예: +4%)
MAX_INVEST_PER_STOCK = int(os.getenv("MAX_INVEST_PER_STOCK", 500000)) # 종목당 최대 매수금
DRY_RUN = os.getenv("DRY_RUN", "false").lower() == "true"

# 5. 기본 감시/매매 대상 종목 (KOSPI 대형주 및 주도주 기본 세팅)
# 종목코드: 종목명 (원하는 종목으로 자유롭게 변경 가능)
TARGET_STOCKS = {
    "005930": "삼성전자",
    "000660": "SK하이닉스",
    "373220": "LG에너지솔루션",
    "207940": "삼성바이오로직스",
    "005380": "현대차",
    "035420": "NAVER",
}
