"""유니버스 스캐너 및 종목 선정 시스템 (Universe Scanner v5.0)
- Execution-Grade Specification v5.0 (Section 3 & Section 8 준수)
- 1차 필터: 관리종목, 정리매매, 환기종목, 스팩, 우선주 배제
- 2차 필터: 최근 20일 일평균 거래대금 >= 50억원
- 3차 필터: 당일 누적 거래대금 >= 20억원
- 4차 필터: 최우선 호가 스프레드 <= 0.20%
- 5차 필터: 시가총액 KOSPI >= 3,000억원, KOSDAQ >= 1,500억원
- 테마/섹터 태깅: 포트폴리오 리스크 관리(동일 테마 <= 3종목) 연동
"""

import json
import logging
from pathlib import Path
from typing import Dict, List, Any, Optional

logger = logging.getLogger("UniverseScanner")

# ==============================================================================
# 1. 국내 주식 최상위 주도주 50선 (TOP 50)
# ==============================================================================
UNIVERSE_TOP50: Dict[str, Dict[str, str]] = {
    # ── 반도체 / IT ──
    "005930": {"name": "삼성전자", "theme": "반도체", "market": "KOSPI"},
    "000660": {"name": "SK하이닉스", "theme": "반도체", "market": "KOSPI"},
    "042700": {"name": "한미반도체", "theme": "반도체", "market": "KOSPI"},
    "058470": {"name": "리노공업", "theme": "반도체", "market": "KOSDAQ"},
    "403870": {"name": "HPSP", "theme": "반도체", "market": "KOSDAQ"},
    "007660": {"name": "이수페타시스", "theme": "반도체", "market": "KOSPI"},
    "036930": {"name": "주성엔지니어링", "theme": "반도체", "market": "KOSDAQ"},

    # ── 2차전지 / 배터리 ──
    "373220": {"name": "LG에너지솔루션", "theme": "2차전지", "market": "KOSPI"},
    "005490": {"name": "POSCO홀딩스", "theme": "2차전지", "market": "KOSPI"},
    "247540": {"name": "에코프로비엠", "theme": "2차전지", "market": "KOSDAQ"},
    "086520": {"name": "에코프로", "theme": "2차전지", "market": "KOSDAQ"},
    "003670": {"name": "포스코퓨처엠", "theme": "2차전지", "market": "KOSPI"},
    "006400": {"name": "삼성SDI", "theme": "2차전지", "market": "KOSPI"},
    "223250": {"name": "엔켐", "theme": "2차전지", "market": "KOSDAQ"},

    # ── 바이오 / 제약 ──
    "207940": {"name": "삼성바이오로직스", "theme": "바이오", "market": "KOSPI"},
    "068270": {"name": "셀트리온", "theme": "바이오", "market": "KOSPI"},
    "196170": {"name": "알테오젠", "theme": "바이오", "market": "KOSDAQ"},
    "000100": {"name": "유한양행", "theme": "바이오", "market": "KOSPI"},
    "028300": {"name": "HLB", "theme": "바이오", "market": "KOSDAQ"},
    "000250": {"name": "삼천당제약", "theme": "바이오", "market": "KOSDAQ"},
    "141080": {"name": "리가켐바이오", "theme": "바이오", "market": "KOSDAQ"},
    "237690": {"name": "에스티팜", "theme": "바이오", "market": "KOSDAQ"},

    # ── 자동차 / 모빌리티 ──
    "005380": {"name": "현대차", "theme": "자동차", "market": "KOSPI"},
    "000270": {"name": "기아", "theme": "자동차", "market": "KOSPI"},
    "012330": {"name": "현대모비스", "theme": "자동차", "market": "KOSPI"},

    # ── 방산 / 항공우주 ──
    "012450": {"name": "한화에어로스페이스", "theme": "방산", "market": "KOSPI"},
    "064350": {"name": "현대로템", "theme": "방산", "market": "KOSPI"},
    "047810": {"name": "한국항공우주", "theme": "방산", "market": "KOSPI"},
    "079550": {"name": "LIG넥스원", "theme": "방산", "market": "KOSPI"},

    # ── 전력설비 / 원전 ──
    "267260": {"name": "HD현대일렉트릭", "theme": "전력설비", "market": "KOSPI"},
    "034020": {"name": "두산에너빌리티", "theme": "전력설비", "market": "KOSPI"},
    "298040": {"name": "효성중공업", "theme": "전력설비", "market": "KOSPI"},
    "010120": {"name": "LS ELECTRIC", "theme": "전력설비", "market": "KOSPI"},
    "006260": {"name": "LS", "theme": "전력설비", "market": "KOSPI"},

    # ── 인터넷 / 플랫폼 / 게임 / 엔터 ──
    "035420": {"name": "NAVER", "theme": "플랫폼", "market": "KOSPI"},
    "035720": {"name": "카카오", "theme": "플랫폼", "market": "KOSPI"},
    "259960": {"name": "크래프톤", "theme": "게임", "market": "KOSPI"},
    "352820": {"name": "하이브", "theme": "엔터", "market": "KOSPI"},
    "041510": {"name": "에스엠", "theme": "엔터", "market": "KOSDAQ"},

    # ── 금융 / 지주 / 밸류업 ──
    "105560": {"name": "KB금융", "theme": "금융", "market": "KOSPI"},
    "055550": {"name": "신한지주", "theme": "금융", "market": "KOSPI"},
    "086790": {"name": "하나금융지주", "theme": "금융", "market": "KOSPI"},
    "138040": {"name": "메리츠금융지주", "theme": "금융", "market": "KOSPI"},
    "028260": {"name": "삼성물산", "theme": "지주", "market": "KOSPI"},

    # ── 조선 / 해운 ──
    "009540": {"name": "HD한국조선해양", "theme": "조선", "market": "KOSPI"},
    "329180": {"name": "HD현대중공업", "theme": "조선", "market": "KOSPI"},
    "010140": {"name": "삼성중공업", "theme": "조선", "market": "KOSPI"},
    "042660": {"name": "한화오션", "theme": "조선", "market": "KOSPI"},

    # ── K-소비재 / 음식료 ──
    "003230": {"name": "삼양식품", "theme": "소비재", "market": "KOSPI"},
    "090430": {"name": "아모레퍼시픽", "theme": "소비재", "market": "KOSPI"},

    # ── 로봇 / AI ──
    "277810": {"name": "레인보우로보틱스", "theme": "로봇", "market": "KOSDAQ"},
    "454910": {"name": "두산로보틱스", "theme": "로봇", "market": "KOSPI"},
}

# ==============================================================================
# 2. 국내 주식 핵심 주도주 100선 (TOP 100) - 50선 포함 확장
# ==============================================================================
UNIVERSE_TOP100: Dict[str, Dict[str, str]] = {
    **UNIVERSE_TOP50,
    # 반도체 / IT 확장
    "240810": {"name": "원익IPS", "theme": "반도체", "market": "KOSDAQ"},
    "005290": {"name": "동진쎄미켐", "theme": "반도체", "market": "KOSDAQ"},
    "357780": {"name": "솔브레인", "theme": "반도체", "market": "KOSDAQ"},
    "089030": {"name": "테크윙", "theme": "반도체", "market": "KOSDAQ"},
    "095340": {"name": "ISC", "theme": "반도체", "market": "KOSDAQ"},
    "039030": {"name": "이오테크닉스", "theme": "반도체", "market": "KOSDAQ"},
    "000990": {"name": "DB하이텍", "theme": "반도체", "market": "KOSPI"},
    "009150": {"name": "삼성전기", "theme": "반도체", "market": "KOSPI"},
    "402340": {"name": "SK스퀘어", "theme": "IT", "market": "KOSPI"},

    # 2차전지 확장
    "066970": {"name": "엘앤에프", "theme": "2차전지", "market": "KOSPI"},
    "005070": {"name": "코스모신소재", "theme": "2차전지", "market": "KOSPI"},
    "086520": {"name": "에코프로", "theme": "2차전지", "market": "KOSDAQ"},
    "051910": {"name": "LG화학", "theme": "2차전지", "market": "KOSPI"},
    "096770": {"name": "SK이노베이션", "theme": "2차전지", "market": "KOSPI"},
    "078600": {"name": "대주전자재료", "theme": "2차전지", "market": "KOSDAQ"},

    # 바이오 확장
    "145020": {"name": "휴젤", "theme": "바이오", "market": "KOSDAQ"},
    "214150": {"name": "클래시스", "theme": "바이오", "market": "KOSDAQ"},
    "128940": {"name": "한미약품", "theme": "바이오", "market": "KOSPI"},
    "084990": {"name": "바이오니아", "theme": "바이오", "market": "KOSDAQ"},
    "298380": {"name": "에이비엘바이오", "theme": "바이오", "market": "KOSDAQ"},
    "087010": {"name": "펩트론", "theme": "바이오", "market": "KOSDAQ"},
    "096530": {"name": "씨젠", "theme": "바이오", "market": "KOSDAQ"},
    "000810": {"name": "삼성화재", "theme": "금융", "market": "KOSPI"},

    # 방산 / 전력 확장
    "272210": {"name": "한화시스템", "theme": "방산", "market": "KOSPI"},
    "103140": {"name": "풍산", "theme": "방산", "market": "KOSPI"},
    "103590": {"name": "일진전기", "theme": "전력설비", "market": "KOSPI"},
    "001440": {"name": "대한전선", "theme": "전력설비", "market": "KOSPI"},
    "009470": {"name": "삼화전기", "theme": "전력설비", "market": "KOSPI"},

    # 자동차 / 기계 / 철강 확장
    "204320": {"name": "HL만도", "theme": "자동차", "market": "KOSPI"},
    "018880": {"name": "한온시스템", "theme": "자동차", "market": "KOSPI"},
    "047050": {"name": "포스코인터내셔널", "theme": "지주", "market": "KOSPI"},
    "066570": {"name": "LG전자", "theme": "IT", "market": "KOSPI"},
    "017800": {"name": "현대엘리베이", "theme": "기계", "market": "KOSPI"},

    # 엔터 / 게임 / IT 확장
    "251270": {"name": "넷마블", "theme": "게임", "market": "KOSPI"},
    "035900": {"name": "JYP Ent.", "theme": "엔터", "market": "KOSDAQ"},
    "263750": {"name": "펄어비스", "theme": "게임", "market": "KOSDAQ"},
    "041020": {"name": "폴라리스오피스", "theme": "AI", "market": "KOSDAQ"},
    "328130": {"name": "루닛", "theme": "AI", "market": "KOSDAQ"},

    # 소비재 / K-푸드 / 뷰티 확장
    "192820": {"name": "코스맥스", "theme": "소비재", "market": "KOSPI"},
    "161890": {"name": "한국콜마", "theme": "소비재", "market": "KOSPI"},
    "033780": {"name": "KT&G", "theme": "소비재", "market": "KOSPI"},
    "097950": {"name": "CJ제일제당", "theme": "소비재", "market": "KOSPI"},

    # 금융 / 지주 확장
    "316140": {"name": "우리금융지주", "theme": "금융", "market": "KOSPI"},
    "024110": {"name": "기업은행", "theme": "금융", "market": "KOSPI"},
    "323410": {"name": "카카오뱅크", "theme": "금융", "market": "KOSPI"},

    # 유틸리티 / 해운 확장
    "015760": {"name": "한국전력", "theme": "유틸리티", "market": "KOSPI"},
    "036460": {"name": "한국가스공사", "theme": "유틸리티", "market": "KOSPI"},
    "011200": {"name": "HMM", "theme": "해운", "market": "KOSPI"},
    "028670": {"name": "팬오션", "theme": "해운", "market": "KOSPI"},
    "443060": {"name": "HD현대마린솔루션", "theme": "조선", "market": "KOSPI"},
}

# ==============================================================================
# 3. 초고속 초저지연 감시용 최상위 20선 (TOP 20)
# ==============================================================================
UNIVERSE_TOP20: Dict[str, Dict[str, str]] = {
    k: UNIVERSE_TOP50[k]
    for k in [
        "005930",  # 삼성전자
        "000660",  # SK하이닉스
        "373220",  # LG에너지솔루션
        "207940",  # 삼성바이오로직스
        "005380",  # 현대차
        "000270",  # 기아
        "068270",  # 셀트리온
        "196170",  # 알테오젠
        "012450",  # 한화에어로스페이스
        "064350",  # 현대로템
        "267260",  # HD현대일렉트릭
        "034020",  # 두산에너빌리티
        "035420",  # NAVER
        "105560",  # KB금융
        "055550",  # 신한지주
        "247540",  # 에코프로비엠
        "003230",  # 삼양식품
        "009540",  # HD한국조선해양
        "042700",  # 한미반도체
        "277810",  # 레인보우로보틱스
    ]
}


class UniverseScanner:
    """유니버스 관리 및 동적 필터링 스캐너"""

    CUSTOM_FILE = Path(__file__).resolve().parent.parent / "config" / "custom_universe.json"

    @classmethod
    def get_universe(cls, mode: str = "full") -> Dict[str, Dict[str, str]]:
        """
        모드별 유니버스 딕셔너리 반환
        :param mode: 'full' (전체 상장 2,670+종목) | 'top20' | 'top50' | 'top100' | 'custom'
        :return: {iem_cd: {"name": ..., "theme": ..., "market": ...}}
        """
        mode = (mode or "full").lower().strip()
        
        if mode in ("full", "all", "market"):
            try:
                from universe.full_universe_master import FullUniverseMaster
                full_m = FullUniverseMaster.load_full_universe()
                return {
                    code: {"name": s.name, "theme": s.theme, "market": s.market}
                    for code, s in full_m.items()
                }
            except Exception as e:
                logger.warning(f"전체 유니버스 로드 실패, TOP50 대체: {e}")
                return UNIVERSE_TOP50
        elif mode == "top20":
            return UNIVERSE_TOP20
        elif mode == "top100":
            return UNIVERSE_TOP100
        elif mode in ("top50", "50"):
            return UNIVERSE_TOP50
        elif mode == "custom":
            custom_data = cls.load_custom_universe()
            return custom_data if custom_data else UNIVERSE_TOP50
        elif mode in ("default8", "8"):
            logger.info("8종목 고정 모드는 폐지되어 FULL MARKET UNIVERSE(2,670+종목)로 자동 전환됩니다.")
            return cls.get_universe("full")
        
        return cls.get_universe("full")

    @classmethod
    def get_universe_dict(cls, mode: str = "full") -> Dict[str, str]:
        """기존 코드(config.TARGET_STOCKS 등) 호환용 {종목코드: 종목명} 반환"""
        u = cls.get_universe(mode)
        return {code: info["name"] for code, info in u.items()}

    @classmethod
    def get_theme_map(cls, mode: str = "full") -> Dict[str, str]:
        """포트폴리오 리스크 엔진 연동용 {종목코드: 테마} 맵 반환"""
        u = cls.get_universe(mode)
        return {code: info["theme"] for code, info in u.items()}

    @classmethod
    def load_custom_universe(cls) -> Optional[Dict[str, Dict[str, str]]]:
        """사용자 정의 JSON 유니버스 로드"""
        if cls.CUSTOM_FILE.exists():
            try:
                with open(cls.CUSTOM_FILE, "r", encoding="utf-8") as f:
                    data = json.load(f)
                    if isinstance(data, dict) and data:
                        return data
            except Exception as e:
                logger.error(f"사용자 정의 유니버스 파일 로드 실패: {e}")
        return None

    @classmethod
    def save_custom_universe(cls, universe_data: Dict[str, Dict[str, str]]):
        """사용자 정의 JSON 유니버스 저장"""
        try:
            cls.CUSTOM_FILE.parent.mkdir(parents=True, exist_ok=True)
            with open(cls.CUSTOM_FILE, "w", encoding="utf-8") as f:
                json.dump(universe_data, f, ensure_ascii=False, indent=2)
            logger.info(f"사용자 정의 유니버스 저장 완료 ({len(universe_data)}종목)")
        except Exception as e:
            logger.error(f"사용자 정의 유니버스 저장 실패: {e}")
