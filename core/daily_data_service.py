"""중앙 집중식 일봉 시세 서비스 (Daily Data Service)
- Section 16-22 완벽 준수
- Broker currentDaily -> DailyDataService -> Daily Cache -> Intraday/Swing/ML/Dashboard
- Positive Cache (1시간 TTL)
- 5종 세분화 Negative Cache (PERMANENT, TRANSIENT, EMPTY_RESPONSE, RATE_LIMITED, NETWORK_FAILURE)
- SingleFlight 중복 요청 병합 (여러 워커가 동시 요청 시 1회만 호출)
"""

import time
import logging
from enum import Enum
from typing import Dict, Any, List, Optional, Tuple

from core.api_gateway import CentralAPIGateway, RequestPriority, ResponseClassifier, ErrorCategory

logger = logging.getLogger("DailyDataService")


class NegativeCacheType(str, Enum):
    PERMANENT_UNSUPPORTED = "PERMANENT_UNSUPPORTED"         # 00200, 미상장, 스펙 미제공 (24시간 TTL)
    TRANSIENT_SERVER_FAILURE = "TRANSIENT_SERVER_FAILURE"   # IGW50025, 5xx 서버 오류 (10분 TTL)
    EMPTY_RESPONSE = "EMPTY_RESPONSE"                       # 빈 배열 회신 (5분 TTL)
    RATE_LIMITED = "RATE_LIMITED"                           # 유량 초과 (30초 TTL)
    NETWORK_FAILURE = "NETWORK_FAILURE"                     # 통신 타임아웃 (60초 TTL)


class DailyDataService:
    """일봉 시세 중앙 데이터 서비스 및 계층형 캐시"""
    _instance: Optional['DailyDataService'] = None

    @classmethod
    def get_instance(cls) -> 'DailyDataService':
        if cls._instance is None:
            cls._instance = cls()
        return cls._instance

    def __init__(self, positive_ttl_sec: float = 3600.0, transient_ttl_sec: float = 600.0):
        self.gateway = CentralAPIGateway.get_instance()
        self.positive_ttl_sec = positive_ttl_sec      # 정상 데이터 캐시 (1시간)
        self.transient_ttl_sec = transient_ttl_sec    # 일시 장애 캐시 (10분 - Section 20)
        self.empty_ttl_sec = 300.0                    # 빈 응답 캐시 (5분)
        self.permanent_ttl_sec = 86400.0              # 미지원 종목 캐시 (24시간)

        # 1. Positive Cache: {iem_cd: (timestamp, candles_list)}
        self._positive_cache: Dict[str, Tuple[float, List[Dict[str, Any]]]] = {}

        # 2. Categorized Negative Cache: {iem_cd: {"type": NegativeCacheType, "timestamp": float, "ttl": float, "reason": str, "retry_count": int}}
        self._negative_cache: Dict[str, Dict[str, Any]] = {}

    def is_cached_or_blocked(self, iem_cd: str) -> Tuple[bool, Optional[List[Dict[str, Any]]]]:
        """캐시 유효성 또는 네거티브 블록 여부 확인 (네트워크 통신 전 0ms 점검)"""
        iem_cd = str(iem_cd).strip()
        now = time.time()

        # 1. Positive Cache 점검
        if iem_cd in self._positive_cache:
            ts, candles = self._positive_cache[iem_cd]
            if now - ts < self.positive_ttl_sec:
                self.gateway.record_cache_hit("/krstock/quote/v1/currentDaily")
                return True, candles

        # 2. Negative Cache 점검
        if iem_cd in self._negative_cache:
            info = self._negative_cache[iem_cd]
            if now - info["timestamp"] < info["ttl"]:
                # 캐시 TTL이 아직 유효한 경우 요청 차단 (빈 리스트 반환)
                self.gateway.record_cache_hit("/krstock/quote/v1/currentDaily")
                return True, []
            else:
                # TTL 만료 시 네거티브 캐시 해제 후 재검증 기회 부여 (Section 18, 20)
                logger.info(f"[{iem_cd}] 네거티브 캐시 TTL 만료 ({info['type']}) -> 재검증 허용")
                del self._negative_cache[iem_cd]

        return False, None

    def get_daily_candles(self, client_or_url: Any, iem_cd: str, count: int = 20) -> List[Dict[str, Any]]:
        """
        일봉 데이터 조회 표준 인터페이스
        - Positive/Negative 캐시 확인 -> SingleFlight 병합 -> API Gateway 호출 -> 캐시 갱신
        """
        iem_cd = str(iem_cd).strip()
        if not iem_cd or len(iem_cd) != 6 or not iem_cd.isalnum():
            return []

        # 1. 캐시 사전 점검
        hit, cached_data = self.is_cached_or_blocked(iem_cd)
        if hit:
            return cached_data

        target_url = getattr(client_or_url, "QUOTE_BASE_URL", "https://api.nhplug.com:8443")
        now = time.time()
        dedup_key = f"currentDaily:{iem_cd}:{count}"

        try:
            # 2. 중앙 게이트웨이 호출 (SingleFlight + 우선순위 DAILY_HISTORICAL)
            data = self.gateway.call_api(
                path="/krstock/quote/v1/currentDaily",
                input_data={
                    "market_cd": "KRX",
                    "iem_cd": iem_cd,
                    "array_cnt": str(count),
                },
                target_url=target_url,
                priority=RequestPriority.DAILY_HISTORICAL,
                max_retries=3,
                dedup_key=dedup_key
            )

            raw_rows = data.get("Output_0", []) if isinstance(data, dict) else []

            # 3. 빈 배열 응답 처리 (Section 22: Empty Response 처리)
            if not raw_rows:
                self._negative_cache[iem_cd] = {
                    "type": NegativeCacheType.EMPTY_RESPONSE,
                    "timestamp": now,
                    "ttl": self.empty_ttl_sec,
                    "reason": "EMPTY_OUTPUT_0",
                    "retry_count": 0
                }
                logger.warning(f"[{iem_cd}] 일봉 응답 데이터 없음 (Output_0 빈 배열) -> 5분 네거티브 캐시 등록")
                return []

            # 4. 정상 캔들 파싱 및 Positive Cache 등록
            candles = []
            for row in raw_rows:
                candles.append({
                    "date": row.get("bsop_date"),
                    "open": int(row.get("stck_oprc", 0)),
                    "high": int(row.get("stck_hgpr", 0)),
                    "low": int(row.get("stck_lwpr", 0)),
                    "close": int(row.get("stck_clpr", 0)),
                    "volume": int(row.get("acml_vol", 0)),
                    "rate": float(row.get("prdy_ctrt", 0.0)),
                })

            self._positive_cache[iem_cd] = (now, candles)
            # 만약 기존에 네거티브 캐시에 등록되어 있었다면 즉시 해제
            self._negative_cache.pop(iem_cd, None)
            return candles

        except Exception as e:
            category, desc = ResponseClassifier.classify(e)
            
            # [A] 미지원/미상장 종목코드 (Section 21: 실제 미지원 종목 24시간 캐시)
            if category == ErrorCategory.INVALID_SYMBOL:
                self._negative_cache[iem_cd] = {
                    "type": NegativeCacheType.PERMANENT_UNSUPPORTED,
                    "timestamp": now,
                    "ttl": self.permanent_ttl_sec,
                    "reason": desc,
                    "retry_count": 1
                }
                logger.warning(f"[{iem_cd}] 실제 미지원 종목 등록 (24시간 일봉 조회 차단): {e}")
                return []

            # [B] 서버 일시 장애 (Section 20: IGW50025 등 10분 네거티브 캐시)
            if category == ErrorCategory.TEMPORARY_SERVER_ERROR:
                prev = self._negative_cache.get(iem_cd, {})
                retries = prev.get("retry_count", 0) + 1
                self._negative_cache[iem_cd] = {
                    "type": NegativeCacheType.TRANSIENT_SERVER_FAILURE,
                    "timestamp": now,
                    "ttl": self.transient_ttl_sec,
                    "reason": desc,
                    "retry_count": retries
                }
                logger.warning(f"[{iem_cd}] 일봉 서버 일시 오류(IGW50025 등) -> {self.transient_ttl_sec/60:.0f}분 네거티브 캐시 등록 (반복 호출 방어)")
                return []

            # [C] 유량 초과
            if category == ErrorCategory.RATE_LIMIT:
                self._negative_cache[iem_cd] = {
                    "type": NegativeCacheType.RATE_LIMITED,
                    "timestamp": now,
                    "ttl": 30.0,
                    "reason": desc,
                    "retry_count": 1
                }
                return []

            # [D] 네트워크 장애
            if category == ErrorCategory.NETWORK_TIMEOUT:
                self._negative_cache[iem_cd] = {
                    "type": NegativeCacheType.NETWORK_FAILURE,
                    "timestamp": now,
                    "ttl": 60.0,
                    "reason": desc,
                    "retry_count": 1
                }
                return []

            logger.error(f"[{iem_cd}] 일봉 조회 예외 ({category}): {e}")
            return []

    def get_cache_stats(self) -> Dict[str, Any]:
        """일봉 캐시 상태 통계"""
        now = time.time()
        active_pos = sum(1 for ts, _ in self._positive_cache.values() if now - ts < self.positive_ttl_sec)
        active_neg = sum(1 for info in self._negative_cache.values() if now - info["timestamp"] < info["ttl"])
        
        breakdown = {}
        for info in self._negative_cache.values():
            if now - info["timestamp"] < info["ttl"]:
                t = info["type"].value if hasattr(info["type"], "value") else str(info["type"])
                breakdown[t] = breakdown.get(t, 0) + 1

        return {
            "positive_cached_symbols": active_pos,
            "negative_blocked_symbols": active_neg,
            "negative_breakdown": breakdown
        }
