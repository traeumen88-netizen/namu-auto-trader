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
    ACCOUNT_NO = os.getenv("ACCOUNT_MOCK", "50001003032")
    MODE_NAME = "모의투자 (MOCK)"

# nhplug 패키지 연동을 위해 환경변수 동기화
os.environ["NHPLUG_APP_KEY"] = APP_KEY
os.environ["NHPLUG_APP_SECRET"] = APP_SECRET
os.environ["NHPLUG_BASE_URL"] = BASE_URL
os.environ["NHPLUG_SUCCESS_CODES"] = "00000,00166,00221,13578,XA109,00001,00167,00218,00219,00220,00168"

# 4. 리스크 관리 설정
STOP_LOSS_RATE = float(os.getenv("STOP_LOSS_RATE", -0.02))      # 손절선 (예: -2%)
TAKE_PROFIT_RATE = float(os.getenv("TAKE_PROFIT_RATE", 0.04))   # 익절선 (예: +4%)
MAX_INVEST_PER_STOCK = int(os.getenv("MAX_INVEST_PER_STOCK", 500000)) # 종목당 최대 매수금
DRY_RUN = os.getenv("DRY_RUN", "false").lower() == "true"

# 5. 대한민국 KOSPI + KOSDAQ 전체 상장 유니버스 (FULL MARKET UNIVERSE v6.0)
# 8개, 10개, 20개 등 특정 종목 고정/하드코딩 배제 -> 2,670+ 전 종목 로드
UNIVERSE_MODE = os.getenv("UNIVERSE_MODE", "full").lower().strip()

try:
    from universe.full_universe_master import FullUniverseMaster
    _full_universe = FullUniverseMaster.load_full_universe()
    TARGET_STOCKS = {code: sym.name for code, sym in _full_universe.items()}
except Exception:
    from universe.universe_scanner import UniverseScanner
    TARGET_STOCKS = UniverseScanner.get_universe_dict(UNIVERSE_MODE)

# 6. BREAKOUT 수급 안전 게이트
BREAKOUT_MIN_RVOL = float(os.getenv("BREAKOUT_MIN_RVOL", 1.5))


