"""
Order Quote Manager (Execution Architecture v16.1)
- 3,136개 전체 시장 스캔과 BUY 직전 개별 Quote Freshness 검증 분리
- GLOBAL_SCAN_AGE / CANDIDATE_QUOTE_AGE / ORDER_QUOTE_AGE 3계층 신선도 분리 관리
- BUY 직전 최신 Quote On-demand 동기화 및 3.0초 엄격 가드 (DATA_STALE 단순 완화 금지)
- Priority API Queue 관리 (HIGH: 보유/BUY직전, MEDIUM: SETUP, LOW: Universe)
- Quote Age 분포 히스토그램 및 텔레메트리
"""

import time
import logging
from datetime import datetime, timedelta
from typing import Optional, Dict, Any, List, Tuple
from dataclasses import dataclass, field

logger = logging.getLogger("OrderQuoteManager")


@dataclass
class OrderQuoteSnapshot:
    """BUY 직전 최신 Quote 불변 스냅샷"""
    symbol: str
    current_price: int
    bid: int
    ask: int
    quote_time: str
    quote_timestamp: datetime
    received_at: datetime
    api_latency_ms: float
    quote_data_age_ms: float
    order_quote_age_ms: float
    is_fresh: bool = True
    staleness_reason: str = ""
    source: str = "REST_ON_DEMAND"

    @property
    def data_age_ms(self) -> float:
        return self.quote_data_age_ms

    def update_order_age(self, now: Optional[datetime] = None) -> float:
        now = now or datetime.now()
        self.order_quote_age_ms = max(0.0, (now - self.received_at).total_seconds() * 1000.0)
        return self.order_quote_age_ms

    def format_log(self) -> str:
        return (
            f"[ORDER QUOTE]\n"
            f"symbol:             {self.symbol}\n"
            f"api_latency_ms:     {self.api_latency_ms:.1f}ms\n"
            f"quote_time:         {self.quote_time}\n"
            f"received_at:        {self.received_at.strftime('%H:%M:%S.%f')[:-3]}\n"
            f"quote_data_age_ms:  {self.quote_data_age_ms:.1f}ms\n"
            f"order_quote_age_ms: {self.order_quote_age_ms:.1f}ms\n"
            f"is_fresh:           {self.is_fresh}"
        )


class OrderQuoteManager:
    """
    주문 직전 개별 종목 Quote 신선도 관리자
    """
    def __init__(self, max_order_age_sec: float = 3.0):
        self.max_order_age_sec = max_order_age_sec
        self.global_scan_start_time: Optional[datetime] = None
        self.global_scan_duration_sec: float = 0.0

        # 종목별 최근 수신 Quote 타임스탬프 캐시
        self.candidate_quote_timestamps: Dict[str, datetime] = {}
        self.candidate_quote_latencies: List[float] = []

        # Freshness 통계
        self.fresh_quote_passed: int = 0
        self.data_stale_count: int = 0

        # DATA_STALE 연령 분포 히스토그램 (초 단위)
        self.age_distribution: Dict[str, int] = {
            "0~1초": 0,
            "1~3초": 0,
            "3~5초": 0,
            "5~10초": 0,
            "10~30초": 0,
            "30초 이상": 0
        }

        # API 호출 및 캐시 통계
        self.api_calls_count: int = 0
        self.cache_hit_count: int = 0
        self.cache_miss_count: int = 0

    def start_global_scan(self):
        """전체 시장 스캔 시작 시각 기록"""
        self.global_scan_start_time = datetime.now()

    def end_global_scan(self) -> float:
        """전체 시장 스캔 종료 및 GLOBAL_SCAN_AGE 기록"""
        if self.global_scan_start_time:
            self.global_scan_duration_sec = (datetime.now() - self.global_scan_start_time).total_seconds()
        return self.global_scan_duration_sec

    @property
    def global_scan_age(self) -> float:
        if self.global_scan_start_time:
            return (datetime.now() - self.global_scan_start_time).total_seconds()
        return self.global_scan_duration_sec

    def record_candidate_quote(self, symbol: str, quote_time: datetime, latency_ms: float = 0.0):
        """후보 종목 시세 수신 시각 기록"""
        self.candidate_quote_timestamps[symbol] = quote_time
        if latency_ms > 0:
            self.candidate_quote_latencies.append(latency_ms)
            if len(self.candidate_quote_latencies) > 500:
                self.candidate_quote_latencies.pop(0)

    def get_candidate_quote_age(self, symbol: str, now: Optional[datetime] = None) -> float:
        """후보 종목의 시세 경과 시간 (초)"""
        now = now or datetime.now()
        ts = self.candidate_quote_timestamps.get(symbol)
        if not ts:
            return 999.0
        return max(0.0, (now - ts).total_seconds())

    def _classify_age(self, age_sec: float) -> str:
        if age_sec < 1.0:
            return "0~1초"
        elif age_sec < 3.0:
            return "1~3초"
        elif age_sec < 5.0:
            return "3~5초"
        elif age_sec < 10.0:
            return "5~10초"
        elif age_sec < 30.0:
            return "10~30초"
        else:
            return "30초 이상"

    def record_age_histogram(self, age_sec: float):
        bucket = self._classify_age(age_sec)
        self.age_distribution[bucket] = self.age_distribution.get(bucket, 0) + 1

    def sync_fresh_quote(
        self,
        client: Any,
        symbol: str,
        signal_price: int,
        now: Optional[datetime] = None
    ) -> OrderQuoteSnapshot:
        """
        [BUY 직전 전용 최신 Quote 동기화]
        - Risk Passed 통과 후 주문 직전에 호출
        - HIGH Priority로 최신 시세를 검증
        - 3.0초 이내 신선도 확인
        """
        now = now or datetime.now()
        self.api_calls_count += 1

        t0 = time.perf_counter()
        raw_quote = None
        try:
            if hasattr(client, "get_current_price"):
                raw_quote = client.get_current_price(symbol)
            else:
                self.cache_hit_count += 1
        except Exception as e:
            logger.error(f"[FreshQuote] {symbol} 시세 조회 예외: {e}")

        api_latency_ms = (time.perf_counter() - t0) * 1000.0
        received_at = datetime.now()

        if raw_quote and raw_quote.get("is_valid", False) and raw_quote.get("price", 0) > 0:
            curr_price = int(raw_quote["price"])
            # 매수1호가/매도1호가 추출
            bid = int(raw_quote.get("bid", curr_price))
            ask = int(raw_quote.get("ask", curr_price))
            quote_time = str(raw_quote.get("quote_time") or received_at.strftime("%H:%M:%S"))

            # 시세 타임스탬프 (브로커 체결/호가 시각)
            quote_ts = raw_quote.get("timestamp")
            if isinstance(quote_ts, str):
                try:
                    quote_dt = datetime.fromisoformat(quote_ts)
                except Exception:
                    quote_dt = received_at
            elif isinstance(quote_ts, datetime):
                quote_dt = quote_ts
            else:
                quote_dt = received_at

            # quote_data_age_ms = 증권사 실제 시세 시각 -> 로컬 응답 수신 시각
            quote_data_age_ms = max(0.0, (received_at - quote_dt).total_seconds() * 1000.0)
            # order_quote_age_ms = Quote 수신 완료 -> 현재 시각 (주문 직전까지의 경과시간)
            order_quote_age_ms = max(0.0, (datetime.now() - received_at).total_seconds() * 1000.0)

            # 3.0초 엄격 신선도 검증: quote_data_age_ms 기준 (증권사 데이터 신선도)
            age_sec = quote_data_age_ms / 1000.0
            self.record_age_histogram(age_sec)

            if age_sec <= self.max_order_age_sec and api_latency_ms <= (self.max_order_age_sec * 1000.0):
                self.fresh_quote_passed += 1
                self.candidate_quote_timestamps[symbol] = quote_dt
                return OrderQuoteSnapshot(
                    symbol=symbol,
                    current_price=curr_price,
                    bid=bid,
                    ask=ask,
                    quote_time=quote_time,
                    quote_timestamp=quote_dt,
                    received_at=received_at,
                    api_latency_ms=api_latency_ms,
                    quote_data_age_ms=quote_data_age_ms,
                    order_quote_age_ms=order_quote_age_ms,
                    is_fresh=True,
                    source="REST_ON_DEMAND"
                )
            else:
                self.data_stale_count += 1
                reason = (
                    f"ORDER_QUOTE_AGE_EXCEEDED ({age_sec:.1f}s > {self.max_order_age_sec}s)"
                    if age_sec > self.max_order_age_sec
                    else f"API_LATENCY_EXCEEDED ({api_latency_ms:.0f}ms > {self.max_order_age_sec*1000:.0f}ms)"
                )
                return OrderQuoteSnapshot(
                    symbol=symbol,
                    current_price=curr_price,
                    bid=bid,
                    ask=ask,
                    quote_time=quote_time,
                    quote_timestamp=quote_dt,
                    received_at=received_at,
                    api_latency_ms=api_latency_ms,
                    quote_data_age_ms=quote_data_age_ms,
                    order_quote_age_ms=order_quote_age_ms,
                    is_fresh=False,
                    staleness_reason=reason,
                    source="REST_STALE"
                )
        else:
            # 브로커 조회 실패 또는 가격 비정상
            age_sec = 999.0
            self.record_age_histogram(age_sec)
            self.data_stale_count += 1
            return OrderQuoteSnapshot(
                symbol=symbol,
                current_price=signal_price,
                bid=signal_price,
                ask=signal_price,
                quote_time="N/A",
                quote_timestamp=now - timedelta(seconds=60),
                received_at=received_at,
                api_latency_ms=api_latency_ms,
                quote_data_age_ms=60000.0,
                order_quote_age_ms=60000.0,
                is_fresh=False,
                staleness_reason="QUOTE_FETCH_FAILED_OR_INVALID",
                source="FALLBACK_STALE"
            )

    def get_candidate_quote_age_stats(self) -> Tuple[float, float]:
        """Candidate quote age 평균 및 P95 반환 (ms 단위)"""
        now = datetime.now()
        ages = [
            (now - ts).total_seconds() * 1000.0
            for ts in self.candidate_quote_timestamps.values()
        ]
        if not ages:
            return 0.0, 0.0
        sorted_ages = sorted(ages)
        p95_idx = int(len(sorted_ages) * 0.95)
        p95_val = sorted_ages[min(p95_idx, len(sorted_ages) - 1)]
        avg_val = sum(sorted_ages) / float(len(sorted_ages))
        return round(avg_val, 1), round(p95_val, 1)

    def get_telemetry_dict(self) -> Dict[str, Any]:
        """대시보드 및 콘솔용 텔레메트리 딕셔너리"""
        avg_age, p95_age = self.get_candidate_quote_age_stats()
        return {
            "global_scan_duration_sec": round(self.global_scan_duration_sec, 2),
            "candidate_count": len(self.candidate_quote_timestamps),
            "candidate_quote_age_avg_ms": avg_age,
            "candidate_quote_age_p95_ms": p95_age,
            "fresh_quote_passed": self.fresh_quote_passed,
            "data_stale_count": self.data_stale_count,
            "age_distribution": dict(self.age_distribution),
            "api_calls": self.api_calls_count,
            "cache_hit": self.cache_hit_count,
            "cache_miss": self.cache_miss_count
        }
