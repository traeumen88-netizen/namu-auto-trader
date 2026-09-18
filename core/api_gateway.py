"""중앙 집중식 API 게이트웨이 (Centralized API Gateway)
- Priority Queue Rate Limiter (긴급 손절/청산 최우선 처리)
- Error Classifier (토큰 만료, 계좌 오류, 서버 일시 오류, 미지원 종목 엄격 분리)
- Exponential Backoff + Jitter Retry Manager (무한루프 원천 차단)
- SingleFlight / Request Deduplication (동일 API 동시 요청 1회 호출 병합)
- Endpoint-level Circuit Breaker (시세 장애와 주문 장애 독립 분리)
- Endpoint Metrics & Failure Rate 수집 (P50/P95/P99 지연시간, 에러율)
"""

import os
import time
import random
import logging
import threading
from enum import Enum
from typing import Dict, Any, Optional, Callable, Tuple
import nhplug
from nhplug.errors import NhplugError

from core.token_manager import TokenManager

os.environ["NHPLUG_SUCCESS_CODES"] = "00000,00166,00221,13578,XA109,00001,00167,00218,00219,00220,00168"
logger = logging.getLogger("APIGateway")


class RequestPriority(int, Enum):
    """API 호출 우선순위 (낮을수록 최우선 실행)"""
    EMERGENCY_STOP = 1        # 긴급 비상 매도 / 하드 스톱
    EXIT_ORDER = 2            # 일반 익절/손절 매도 주문
    ENTRY_ORDER = 3           # 신규 진입 매수 주문
    RECONCILIATION = 4        # 원장/주문 정합성 대사 (Zombie / Open Orders)
    REALTIME_QUOTE = 5        # 실시간 현재가 / 호가 조회
    DAILY_HISTORICAL = 6      # 일봉 / 분봉 등 히스토리 데이터
    DASHBOARD_ANALYTICS = 7   # 대시보드 / 텔레메트리 폴링


class ErrorCategory(str, Enum):
    """API 에러 엄격 분류 (분류별 재시도 및 토큰 정책 분리)"""
    SUCCESS = "SUCCESS"
    TOKEN_EXPIRED = "TOKEN_EXPIRED"                         # 401, IGW40043, IGW40002 (토큰 재발급 후 재시도)
    ACCOUNT_CONFIGURATION_ERROR = "ACCOUNT_CONFIG_ERROR"   # IGW40018, IGW40020 (계좌번호 오류, 토큰삭제 절대금지, 재시도금지)
    TEMPORARY_SERVER_ERROR = "TEMPORARY_SERVER_ERROR"       # IGW50025, 500, 502, 503 (백오프 재시도, 토큰유지)
    RATE_LIMIT = "RATE_LIMIT"                               # 429, IGW42902 (대기 후 재시도)
    NETWORK_TIMEOUT = "NETWORK_TIMEOUT"                     # Timeout, 10053, ConnectionReset (백오프 재시도)
    INVALID_SYMBOL = "INVALID_SYMBOL"                       # 00200, IGW00121, 404 (미상장, 재시도금지, 네거티브캐시)
    BUSINESS_ERROR = "BUSINESS_ERROR"                       # 증권사 비즈니스 거부 (재시도금지)
    UNKNOWN_ERROR = "UNKNOWN_ERROR"


class ResponseClassifier:
    """나무증권 API 응답 및 예외 정밀 분류기"""
    @staticmethod
    def classify(e: Exception) -> Tuple[ErrorCategory, str]:
        err_str = str(e)
        status = getattr(e, "status", 0)
        code = getattr(e, "code", "")

        # 1. 계좌번호 불일치/미등록 오류 (IGW40018 등) -> 토큰 오류가 절대 아님!
        if "IGW40018" in err_str or "계좌번호" in err_str and "존재하지 않습니다" in err_str:
            return ErrorCategory.ACCOUNT_CONFIGURATION_ERROR, "계좌번호 불일치 또는 API 미등록 계좌 (토큰 유지)"

        # 2. 실제 토큰 만료/무효화 오류
        if "IGW40043" in err_str or "IGW40002" in err_str or status == 401:
            return ErrorCategory.TOKEN_EXPIRED, "접근 토큰 만료 또는 무효화"

        # 3. 서버 일시 장애 (IGW50025 등)
        if "IGW50025" in err_str or status in (500, 502, 503, 504):
            return ErrorCategory.TEMPORARY_SERVER_ERROR, "증권사 서버 일시적 처리 장애"

        # 4. 호출 유량 제한 (Rate Limit)
        if getattr(e, "category", "") == "rate_limit" or "429" in err_str or "IGW42902" in err_str:
            return ErrorCategory.RATE_LIMIT, "초당 호출 유량 한도 초과"

        # 5. 네트워크 지연 및 타임아웃
        if getattr(e, "category", "") == "network" or any(k in err_str for k in ("timed out", "timeout", "Connection", "10053")):
            return ErrorCategory.NETWORK_TIMEOUT, "네트워크 통신 지연 또는 소켓 끊김"

        # 6. 미지원/미상장 종목코드 (00200 등)
        if "00200" in err_str or "입력정보" in err_str or "IGW00121" in err_str or status == 404:
            return ErrorCategory.INVALID_SYMBOL, "미등록/미상장 또는 시세 미제공 종목코드"

        # 7. 비즈니스 오류 (23962 매매가능시간 아님, 잔고부족, 가격단위 등 브로커 업무 거절)
        if (
            getattr(e, "category", "") == "business"
            or "[business]" in err_str
            or "23962" in err_str
            or "매매가능" in err_str
            or "예수금" in err_str
            or "주문가능" in err_str
            or "잔고" in err_str
            or "호가단위" in err_str
            or "거래정지" in err_str
        ):
            return ErrorCategory.BUSINESS_ERROR, f"증권사 업무 거절: {err_str[:60]}"

        return ErrorCategory.UNKNOWN_ERROR, err_str[:80]


class EndpointCircuitBreaker:
    """엔드포인트별 서킷 브레이커 (시세 장애와 주문 장애를 철저히 격리)"""
    def __init__(self, failure_threshold: int = 5, recovery_time: float = 30.0):
        self.failure_threshold = failure_threshold
        self.recovery_time = recovery_time
        self.failure_count = 0
        self.state = "NORMAL"  # NORMAL, PAUSED, HALF_OPEN
        self.last_failure_time = 0.0
        self._lock = threading.Lock()

    def record_success(self):
        with self._lock:
            self.failure_count = 0
            self.state = "NORMAL"

    def record_failure(self):
        with self._lock:
            self.failure_count += 1
            self.last_failure_time = time.time()
            if self.failure_count >= self.failure_threshold:
                self.state = "PAUSED"
                logger.warning(f"[CircuitBreaker PAUSED] 연속 {self.failure_count}회 장애 감지 -> {self.recovery_time}초간 일시 중단")

    def can_execute(self) -> bool:
        with self._lock:
            if self.state == "NORMAL":
                return True
            if self.state == "PAUSED":
                if time.time() - self.last_failure_time > self.recovery_time:
                    self.state = "HALF_OPEN"
                    logger.info("[CircuitBreaker HALF_OPEN] 회복 시험 호출 허용")
                    return True
                return False
            if self.state == "HALF_OPEN":
                return True
            return True


class SingleFlightDeduplicator:
    """동일 엔드포인트/파라미터 동시 요청 병합기 (Request Deduplication / SingleFlight)"""
    def __init__(self):
        self._inflight: Dict[str, Tuple[threading.Event, Dict[str, Any]]] = {}
        self._lock = threading.Lock()

    def execute(self, key: str, fn: Callable[[], Any]) -> Tuple[Any, bool]:
        """
        동일 key에 대해 동시 호출 시 1회만 실제 실행하고 나머지 호출자는 결과를 공유
        Returns: (result, is_deduplicated)
        """
        with self._lock:
            if key in self._inflight:
                event, result_box = self._inflight[key]
                first_caller = False
            else:
                event = threading.Event()
                result_box = {"result": None, "error": None}
                self._inflight[key] = (event, result_box)
                first_caller = True

        if first_caller:
            try:
                res = fn()
                result_box["result"] = res
                return res, False
            except Exception as e:
                result_box["error"] = e
                raise e
            finally:
                with self._lock:
                    del self._inflight[key]
                event.set()
        else:
            # 다른 워커가 실행 중이므로 결과 대기
            event.wait(timeout=15.0)
            if result_box["error"]:
                raise result_box["error"]
            return result_box["result"], True


class CentralAPIGateway:
    """중앙 집중식 API 게이트웨이 (API Gateway)"""
    _instance: Optional['CentralAPIGateway'] = None
    _singleton_lock = threading.Lock()

    @classmethod
    def get_instance(cls) -> 'CentralAPIGateway':
        with cls._singleton_lock:
            if cls._instance is None:
                cls._instance = cls()
            return cls._instance

    def __init__(self):
        self.token_manager = TokenManager.get_instance()
        self.deduplicator = SingleFlightDeduplicator()
        
        # 엔드포인트별 서킷 브레이커 (주문 vs 조회 독립)
        self.order_circuit = EndpointCircuitBreaker(failure_threshold=5, recovery_time=15.0)
        self.quote_circuit = EndpointCircuitBreaker(failure_threshold=8, recovery_time=20.0)

        # 유량 제한 (Token Bucket Rate Limiter - 초당 최대 4회 보장)
        self._rate_limit_lock = threading.Lock()
        self._last_call_time = 0.0
        self._min_interval = 0.25  # 초당 4회

        # 텔레메트리 메트릭 집계
        self._metrics_lock = threading.Lock()
        self.metrics: Dict[str, Dict[str, Any]] = {}

    def _get_metric_bucket(self, endpoint: str) -> Dict[str, Any]:
        if endpoint not in self.metrics:
            self.metrics[endpoint] = {
                "calls": 0,
                "success": 0,
                "errors": 0,
                "retries": 0,
                "rate_limits": 0,
                "cache_hits": 0,
                "dedup_hits": 0,
                "latencies_ms": []
            }
        return self.metrics[endpoint]

    def _throttle(self, priority: RequestPriority):
        """우선순위 기반 유량 제한 (긴급 손절/청산은 딜레이 최소화)"""
        if priority == RequestPriority.EMERGENCY_STOP:
            return  # 비상 주문은 유량 제한 즉시 통과

        with self._rate_limit_lock:
            now = time.time()
            elapsed = now - self._last_call_time
            needed = self._min_interval
            if priority in (RequestPriority.EXIT_ORDER, RequestPriority.ENTRY_ORDER):
                needed = 0.15  # 주문 요청은 가중 우대
            
            if elapsed < needed:
                time.sleep(needed - elapsed)
            self._last_call_time = time.time()

    def call_api(
        self,
        path: str,
        input_data: Dict[str, Any],
        target_url: str,
        priority: RequestPriority = RequestPriority.REALTIME_QUOTE,
        max_retries: int = 3,
        timeout: int = 12,
        dedup_key: Optional[str] = None
    ) -> Any:
        """
        중앙 게이트웨이를 통한 표준 브로커 API 호출
        - 토큰 검증, 서킷브레이커, Rate Limiter, Deduplication, Exponential Backoff 일괄 처리
        """
        # 1. 서킷 브레이커 점검 (주문 vs 조회 분리)
        is_order = "/order/" in path
        circuit = self.order_circuit if is_order else self.quote_circuit
        
        # 비상 청산 주문이 아니면서 서킷이 차단된 경우 에러 방출
        if priority != RequestPriority.EMERGENCY_STOP and not circuit.can_execute():
            raise NhplugError(f"[{path}] 서킷 브레이커 차단 상태 (일시 장애 격리 중)", category="circuit_breaker")

        # 2. SingleFlight 중복 요청 병합 (dedup_key 제공 시)
        if dedup_key and not is_order:
            return self.deduplicator.execute(
                dedup_key,
                lambda: self._execute_with_retry(path, input_data, target_url, priority, max_retries, timeout, circuit)
            )[0]

        return self._execute_with_retry(path, input_data, target_url, priority, max_retries, timeout, circuit)

    def _execute_with_retry(
        self,
        path: str,
        input_data: Dict[str, Any],
        target_url: str,
        priority: RequestPriority,
        max_retries: int,
        timeout: int,
        circuit: EndpointCircuitBreaker
    ) -> Any:
        # 대상 서버 URL 환경변수 동기화
        os.environ["NHPLUG_BASE_URL"] = target_url

        with self._metrics_lock:
            bucket = self._get_metric_bucket(path)
            bucket["calls"] += 1

        last_exception = None

        for attempt in range(max_retries):
            # 우선순위 스로틀링
            self._throttle(priority)

            # 유효 토큰 확인 (토큰 관리자 연동)
            token = self.token_manager.get_token()

            t_start = time.time()
            try:
                # 브로커 API 호출 실행
                res = nhplug.call(path, input_data, timeout=timeout)
                t_latency = (time.time() - t_start) * 1000.0

                # 성공 메트릭 기록
                with self._metrics_lock:
                    bucket["success"] += 1
                    bucket["latencies_ms"].append(t_latency)
                    if len(bucket["latencies_ms"]) > 200:
                        bucket["latencies_ms"].pop(0)

                circuit.record_success()
                return res

            except Exception as e:
                t_latency = (time.time() - t_start) * 1000.0
                last_exception = e
                
                # 연속조회 안내 정상 코드(00218, 00219, XA109 등)는 raw 반환
                if hasattr(e, "raw") and e.raw and any(c in str(e) for c in ("00218", "00219", "00220", "00166", "00167", "00221", "XA109", "00001", "13578")):
                    with self._metrics_lock:
                        bucket["success"] += 1
                    circuit.record_success()
                    return e.raw

                # 에러 분류 수행
                category, desc = ResponseClassifier.classify(e)

                with self._metrics_lock:
                    bucket["errors"] += 1

                # [A] 계좌번호 불일치(IGW40018): 토큰 삭제 절대 금지, 재시도 금지 -> 즉시 에러 방출
                if category == ErrorCategory.ACCOUNT_CONFIGURATION_ERROR:
                    logger.error(f"[{path}] 계좌 설정 오류 감지 ({desc}): {e}")
                    circuit.record_failure()
                    raise e

                # [B] 미지원 종목(00200): 재시도 불필요 -> 즉시 상위 전달 (Negative Cache 처리용)
                if category == ErrorCategory.INVALID_SYMBOL:
                    raise e

                # [C] 비즈니스 거절: 재시도 불필요
                if category == ErrorCategory.BUSINESS_ERROR:
                    raise e

                # [D] 실제 토큰 만료(401, IGW40043): Refresh Lock 획득 후 단 1회만 토큰 갱신
                if category == ErrorCategory.TOKEN_EXPIRED:
                    circuit.record_failure()
                    if attempt < max_retries - 1:
                        logger.warning(f"[{path}] 토큰 만료 감지 -> Refresh Lock 획득 후 토큰 갱신... ({attempt+1}/{max_retries})")
                        with self._metrics_lock:
                            bucket["retries"] += 1
                        self.token_manager.mark_invalid(error_code="TOKEN_EXPIRED", reason=str(e))
                        self.token_manager.get_token(force=True, reason=f"RETRY_ATTEMPT_{attempt+1}")
                        continue
                    raise e

                # [E] 유량 제한(Rate Limit 429)
                if category == ErrorCategory.RATE_LIMIT:
                    with self._metrics_lock:
                        bucket["rate_limits"] += 1
                        bucket["retries"] += 1
                    wait_time = (attempt + 1) * 0.6 + random.uniform(0.1, 0.3)
                    logger.warning(f"[{path}] 초당 유량 제한 감지 -> {wait_time:.2f}초 지터 대기 후 재시도... ({attempt+1}/{max_retries})")
                    time.sleep(wait_time)
                    continue

                # [F] 서버 일시 장애(IGW50025) 또는 네트워크 타임아웃 -> Exponential Backoff + Jitter
                if category in (ErrorCategory.TEMPORARY_SERVER_ERROR, ErrorCategory.NETWORK_TIMEOUT):
                    circuit.record_failure()
                    if attempt < max_retries - 1:
                        with self._metrics_lock:
                            bucket["retries"] += 1
                        # Retry 1: 0.5s, Retry 2: 1.0s, Retry 3: 2.0s (+ Jitter)
                        backoff = (0.5 * (2 ** attempt)) + random.uniform(0.05, 0.25)
                        logger.warning(f"[{path}] {desc} ({e}) -> {backoff:.2f}s 지터 백오프 후 재시도 ({attempt+1}/{max_retries})")
                        time.sleep(backoff)
                        continue
                    raise e

                # 기타 알 수 없는 에러
                circuit.record_failure()
                if attempt < max_retries - 1:
                    with self._metrics_lock:
                        bucket["retries"] += 1
                    time.sleep(0.5)
                    continue
                raise e

        raise last_exception or RuntimeError(f"[{path}] API 호출 최대 재시도({max_retries}회) 초과 실패")

    def record_cache_hit(self, endpoint: str):
        with self._metrics_lock:
            bucket = self._get_metric_bucket(endpoint)
            bucket["cache_hits"] += 1

    def get_endpoint_telemetry(self) -> Dict[str, Any]:
        """대시보드 표시용 엔드포인트별 호출량 및 실패율/지연시간 통계"""
        with self._metrics_lock:
            res = {}
            for ep, b in self.metrics.items():
                total = b["calls"]
                succ = b["success"]
                err = b["errors"]
                c_hit = b["cache_hits"]
                total_inquiry = total + c_hit
                hit_rate = (c_hit / total_inquiry * 100.0) if total_inquiry > 0 else 0.0
                fail_rate = (err / total * 100.0) if total > 0 else 0.0
                
                latencies = sorted(b["latencies_ms"])
                avg_lat = sum(latencies) / len(latencies) if latencies else 0.0
                p95 = latencies[int(len(latencies) * 0.95)] if latencies else 0.0
                p99 = latencies[int(len(latencies) * 0.99)] if latencies else 0.0

                res[ep] = {
                    "calls": total,
                    "success": succ,
                    "errors": err,
                    "retries": b["retries"],
                    "cache_hits": c_hit,
                    "hit_rate_pct": round(hit_rate, 1),
                    "fail_rate_pct": round(fail_rate, 1),
                    "avg_latency_ms": round(avg_lat, 1),
                    "p95_latency_ms": round(p95, 1),
                    "p99_latency_ms": round(p99, 1),
                }
            return res
