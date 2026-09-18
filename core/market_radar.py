"""[FINAL MASTER v16.0] 저비용 시장 전체 감시 및 수급 레이더 (core/market_radar.py)
Section 7: 전체 시장 감시 Architecture
- 전체 KOSPI/KOSDAQ (3,136개) Universe 대상 저비용 실시간 수급 모니터링
- 모든 종목에 개별 REST 1초 Polling 금지 (API Rate Limit 및 지연 방지)
- 실시간 거래량/거래대금 상위, 상승률 상위, 수급 급증 종목 실시간 감지
- 이벤트 발생 종목을 Candidate -> ACTIVE로 고속 승격하여 고해상도 집중 모니터링으로 전환
"""

import time
import re
import urllib.request
import logging
from datetime import datetime
from typing import Dict, List, Tuple, Optional, Any
from dataclasses import dataclass

logger = logging.getLogger("MarketRadar")


@dataclass
class RadarMover:
    iem_cd: str
    name: str
    price: int
    change_rate: float
    volume: int
    turnover: float
    source: str  # "VOLUME_TOP", "GAIN_TOP", "ROLLING_SCAN"


class MarketRadar:
    """전체 KOSPI/KOSDAQ 저비용 초고속 수급 감시 레이더"""

    def __init__(self, request_timeout: float = 3.0):
        self.request_timeout = request_timeout
        self.last_scan_time: Optional[datetime] = None
        self.headers = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"
        }
        self.cached_movers: List[RadarMover] = []
        self.last_fetch_ts: float = 0.0

    def scan_market_movers(self) -> List[RadarMover]:
        """
        KOSPI + KOSDAQ 시장 전체에서 현재 가장 활발한 수급 및 가격 변동 종목 탐지
        (캐시 TTL: 2초 - 불필요한 네트워크 부하 방지)
        """
        now_ts = time.time()
        if now_ts - self.last_fetch_ts < 2.0 and self.cached_movers:
            return self.cached_movers

        movers: List[RadarMover] = []
        seen_codes = set()

        # 1. 거래량 상위 (KOSPI=0, KOSDAQ=1)
        for sosok in ["0", "1"]:
            url = f"https://finance.naver.com/sise/sise_quant.naver?sosok={sosok}"
            try:
                req = urllib.request.Request(url, headers=self.headers)
                with urllib.request.urlopen(req, timeout=self.request_timeout) as res:
                    html = res.read().decode("cp949", errors="ignore")
                    matches = re.findall(
                        r'href="/item/main\.naver\?code=(\d{6})"[^>]*class="title">([^<]+)</a>.*?'
                        r'<td class="number">([0-9,]+)</td>.*?'
                        r'<td class="number">.*?([+-]?[0-9.]+%)</td>.*?'
                        r'<td class="number">([0-9,]+)</td>',
                        html, re.DOTALL
                    )
                    for code, name, pr_str, rate_str, vol_str in matches[:40]:
                        if code in seen_codes:
                            continue
                        seen_codes.add(code)
                        try:
                            price = int(pr_str.replace(",", "").strip())
                            rate_val = float(rate_str.replace("%", "").replace("+", "").strip())
                            volume = int(vol_str.replace(",", "").strip())
                            turnover = float(price * volume)
                            movers.append(RadarMover(
                                iem_cd=code,
                                name=name.strip(),
                                price=price,
                                change_rate=rate_val / 100.0,
                                volume=volume,
                                turnover=turnover,
                                source="VOLUME_TOP"
                            ))
                        except Exception:
                            continue
            except Exception as e:
                logger.debug(f"거래량 상위 스캔 오류 ({sosok}): {e}")

        # 2. 상승률 상위 (급등주 탐지, KOSPI=0, KOSDAQ=1)
        for sosok in ["0", "1"]:
            url = f"https://finance.naver.com/sise/sise_rise.naver?sosok={sosok}"
            try:
                req = urllib.request.Request(url, headers=self.headers)
                with urllib.request.urlopen(req, timeout=self.request_timeout) as res:
                    html = res.read().decode("cp949", errors="ignore")
                    matches = re.findall(
                        r'href="/item/main\.naver\?code=(\d{6})"[^>]*class="title">([^<]+)</a>.*?'
                        r'<td class="number">([0-9,]+)</td>.*?'
                        r'<td class="number">.*?([+-]?[0-9.]+%)</td>.*?'
                        r'<td class="number">([0-9,]+)</td>',
                        html, re.DOTALL
                    )
                    for code, name, pr_str, rate_str, vol_str in matches[:30]:
                        if code in seen_codes:
                            continue
                        seen_codes.add(code)
                        try:
                            price = int(pr_str.replace(",", "").strip())
                            rate_val = float(rate_str.replace("%", "").replace("+", "").strip())
                            volume = int(vol_str.replace(",", "").strip())
                            turnover = float(price * volume)
                            movers.append(RadarMover(
                                iem_cd=code,
                                name=name.strip(),
                                price=price,
                                change_rate=rate_val / 100.0,
                                volume=volume,
                                turnover=turnover,
                                source="GAIN_TOP"
                            ))
                        except Exception:
                            continue
            except Exception as e:
                logger.debug(f"상승률 상위 스캔 오류 ({sosok}): {e}")

        self.cached_movers = movers
        self.last_fetch_ts = now_ts
        self.last_scan_time = datetime.now()
        return movers
