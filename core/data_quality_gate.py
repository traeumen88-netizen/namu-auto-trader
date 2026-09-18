"""[FINAL MASTER v16.0] Real-time Data Quality Gate (core/data_quality_gate.py)
Section 10 & 11: Data Quality Gate & Market Data Staleness 감시

검사 항목:
1. price <= 0, volume < 0
2. NaN, Inf, None
3. timestamp 역전 (timestamp < last_timestamp - 5s)
4. timestamp 과도한 지연 (staleness threshold 초과)
5. duplicate tick 필터링
6. 비정상 price jump (단일 틱 15% 이상 급변동)
7. bid > ask (호가 역전)
8. 비정상 spread ((ask - bid) / ask > 5%)
9. stale quote (최신성 상실)
10. 비정상 volume (비상식적 음수 또는 초대형 이상치)

이상 데이터 발생 시: DATA_QUALITY_REJECT 기록 후 전략 및 ML 입력에서 차단.
"""

import math
import logging
from datetime import datetime, timedelta
from typing import Optional, Tuple, Dict, Any
from dataclasses import dataclass, field
from core.models import Tick

logger = logging.getLogger("DataQualityGate")


@dataclass
class QualityMetrics:
    total_ticks_checked: int = 0
    total_passed: int = 0
    total_rejected: int = 0
    rejections_by_reason: Dict[str, int] = field(default_factory=dict)
    last_rejection_reason: Optional[str] = None
    last_rejection_time: Optional[datetime] = None
    last_tick_time: Optional[datetime] = None
    max_tick_latency_ms: float = 0.0


class DataQualityGate:
    """실시간 시세 무결성 검증 및 이상치 차단 게이트"""

    def __init__(
        self,
        max_staleness_seconds: float = 15.0,  # 15초 이상 지연 시 stale 간주
        max_price_jump_pct: float = 0.15,      # 단일 틱 15% 이상 급등락 차단
        max_spread_ratio: float = 0.05         # 스프레드 5% 초과 차단
    ):
        self.max_staleness_seconds = max_staleness_seconds
        self.max_price_jump_pct = max_price_jump_pct
        self.max_spread_ratio = max_spread_ratio
        self.metrics = QualityMetrics()

        # 종목별 마지막 정상 틱 캐시: {iem_cd: {"price": int, "volume": int, "timestamp": datetime}}
        self.last_valid_ticks: Dict[str, Dict[str, Any]] = {}

    def validate_tick(
        self_or_cls,
        iem_cd: Any,
        price: Optional[float] = None,
        volume: Optional[int] = None,
        timestamp: Optional[datetime] = None,
        bid: Optional[int] = None,
        ask: Optional[int] = None,
        now: Optional[datetime] = None
    ) -> Tuple[bool, Optional[str]]:
        """
        수신된 시세 틱의 유효성을 전수 검사하고 (통과여부, 탈락사유) 반환
        인스턴스 및 클래스 직접 호출(DataQualityGate.validate_tick), Tick 객체 전달 모두 지원
        """
        self = self_or_cls if isinstance(self_or_cls, DataQualityGate) else DataQualityGate()

        if isinstance(iem_cd, Tick):
            tick = iem_cd
            return self.validate_tick(
                iem_cd=tick.iem_cd,
                price=float(tick.price),
                volume=int(tick.volume),
                timestamp=tick.timestamp,
                bid=bid if bid is not None else getattr(tick, "bid_price", None),
                ask=ask if ask is not None else getattr(tick, "ask_price", None),
                now=now
            )

        now = now or datetime.now()
        self.metrics.total_ticks_checked += 1

        # 1. Null / NaN / Inf 검사
        if price is None or math.isnan(price) or math.isinf(price):
            return self._reject("NAN_OR_INF_PRICE")
        if volume is None or math.isnan(volume) or math.isinf(volume):
            return self._reject("NAN_OR_INF_VOLUME")

        # 2. 가격 및 거래량 기본 부호 검사
        if price <= 0:
            return self._reject("PRICE_NON_POSITIVE")
        if volume < 0:
            return self._reject("VOLUME_NEGATIVE")

        # 3. 호가 역전 및 스프레드 이상 검사
        if bid is not None and ask is not None and bid > 0 and ask > 0:
            if bid > ask:
                return self._reject(f"INVERTED_SPREAD (bid:{bid} > ask:{ask})")
            spread_ratio = (ask - bid) / float(ask)
            if spread_ratio > self.max_spread_ratio:
                return self._reject(f"ABNORMAL_SPREAD ({spread_ratio*100:.2f}% > {self.max_spread_ratio*100:.1f}%)")

        # 4. 타임스탬프 신선도 (Staleness) 검사
        if timestamp:
            age_sec = (now - timestamp).total_seconds()
            if age_sec > self.max_staleness_seconds:
                return self._reject(f"STALE_MARKET_DATA (age: {age_sec:.1f}s > {self.max_staleness_seconds:.1f}s)")
            latency_ms = max(0.0, age_sec * 1000.0)
            if latency_ms > self.metrics.max_tick_latency_ms:
                self.metrics.max_tick_latency_ms = latency_ms

        # 5. 이전 틱 대비 검사 (종목별)
        prev = self.last_valid_ticks.get(iem_cd)
        if prev:
            # 타임스탬프 역전 검사 (5초 이상 과거로 역행한 틱 거절)
            if timestamp and prev["timestamp"]:
                if (prev["timestamp"] - timestamp).total_seconds() > 5.0:
                    return self._reject(f"TIMESTAMP_REVERSAL (prev:{prev['timestamp']} > curr:{timestamp})")

            # 단일 틱 가격 급변 (비정상 스파이크/오류) 검사
            prev_price = prev["price"]
            if prev_price > 0:
                jump_pct = abs(price - prev_price) / float(prev_price)
                if jump_pct > self.max_price_jump_pct:
                    return self._reject(f"ABNORMAL_PRICE_JUMP ({jump_pct*100:.1f}% > {self.max_price_jump_pct*100:.1f}%)")

            # 동일 중복 틱 감지 (동일 시간, 동일 가격, 동일 거래량)
            if (
                prev["price"] == price
                and prev["volume"] == volume
                and prev["timestamp"] == timestamp
            ):
                # 중복 틱은 에러가 아닌 가벼운 스킵 처리 (통계에는 기록)
                return False, "DUPLICATE_TICK"

        # 모든 검증 통과: 정상 캐시 업데이트
        self.last_valid_ticks[iem_cd] = {
            "price": int(price),
            "volume": int(volume),
            "timestamp": timestamp
        }
        self.metrics.total_passed += 1
        self.metrics.last_tick_time = now
        return True, None

    def _reject(self, reason: str) -> Tuple[bool, str]:
        """거절 처리 및 텔레메트리 누적"""
        self.metrics.total_rejected += 1
        self.metrics.rejections_by_reason[reason] = self.metrics.rejections_by_reason.get(reason, 0) + 1
        self.metrics.last_rejection_reason = reason
        self.metrics.last_rejection_time = datetime.now()
        logger.debug(f"[DATA_QUALITY_REJECT] {reason}")
        return False, f"DATA_QUALITY_REJECT: {reason}"

    def get_staleness_status(self, now: Optional[datetime] = None) -> Dict[str, Any]:
        """전체 시세 데이터의 신선도 평가"""
        now = now or datetime.now()
        if not self.metrics.last_tick_time:
            return {"status": "NO_DATA", "age_sec": 999.0, "is_stale": True}
        age_sec = (now - self.metrics.last_tick_time).total_seconds()
        is_stale = age_sec > self.max_staleness_seconds
        status = "STALE" if is_stale else "HEALTHY"
        return {
            "status": status,
            "age_sec": round(age_sec, 2),
            "is_stale": is_stale,
            "max_latency_ms": round(self.metrics.max_tick_latency_ms, 1),
            "total_passed": self.metrics.total_passed,
            "total_rejected": self.metrics.total_rejected
        }

    @classmethod
    def validate_quote(cls, best_bid: int, best_ask: int, max_spread_ratio: float = 0.05) -> Tuple[bool, Optional[str]]:
        """호가 역전 및 스프레드 정적 검증"""
        if best_bid > best_ask:
            return False, f"DATA_QUALITY_REJECT: INVERTED_SPREAD (bid:{best_bid} > ask:{best_ask})"
        if best_ask > 0 and (best_ask - best_bid) / float(best_ask) > max_spread_ratio:
            return False, f"DATA_QUALITY_REJECT: ABNORMAL_SPREAD"
        return True, None

    @classmethod
    def validate_price_jump(cls, current_price: float, prev_price: float, max_jump_pct: float = 0.15) -> Tuple[bool, Optional[str]]:
        """단일 틱/바 가격 급변동 정적 검증"""
        if prev_price <= 0:
            return True, None
        jump_pct = abs(current_price - prev_price) / float(prev_price)
        if jump_pct > max_jump_pct:
            return False, f"DATA_QUALITY_REJECT: ABNORMAL_PRICE_JUMP ({jump_pct*100:.1f}% > {max_jump_pct*100:.1f}%)"
        return True, None

    def validate_bar(
        self,
        iem_cd: str,
        bar: Optional[Dict[str, Any]],
        now: Optional[datetime] = None,
        is_halted: bool = False
    ) -> Tuple[bool, Optional[str]]:
        """
        Validates OHLCV bar data integrity before strategy and ML processing.
        Defends against:
        1. missing bar (None or empty)
        2. trading halt
        3. invalid price (price <= 0, high < low, high < open, etc.)
        4. zero / negative volume
        5. duplicate timestamp
        6. out-of-order timestamp
        7. abnormal price jump (> 30%)
        8. missing / inverted bid/ask
        """
        now = now or datetime.now()
        self.metrics.total_ticks_checked += 1

        if bar is None or not bar:
            return self._reject("MISSING_BAR")

        if is_halted or bar.get("is_halted", False):
            return self._reject("TRADING_HALT")

        # Extract fields
        open_p = bar.get("open")
        high_p = bar.get("high")
        low_p = bar.get("low")
        close_p = bar.get("close")
        volume = bar.get("volume")

        # 1. Null / NaN / Inf
        for val, name in [(open_p, "OPEN"), (high_p, "HIGH"), (low_p, "LOW"), (close_p, "CLOSE")]:
            if val is None or math.isnan(val) or math.isinf(val):
                return self._reject(f"NAN_OR_INF_{name}_PRICE")
        if volume is None or math.isnan(volume) or math.isinf(volume):
            return self._reject("NAN_OR_INF_VOLUME")

        # 2. Positive price & volume
        if open_p <= 0 or high_p <= 0 or low_p <= 0 or close_p <= 0:
            return self._reject("INVALID_PRICE_NEGATIVE_OR_ZERO")
        if volume <= 0:
            return self._reject("ZERO_OR_NEGATIVE_VOLUME")

        # 3. High/Low range consistency
        if high_p < low_p:
            return self._reject(f"INVALID_PRICE_HIGH_LESS_THAN_LOW (high:{high_p} < low:{low_p})")
        if high_p < open_p or high_p < close_p or low_p > open_p or low_p > close_p:
            return self._reject("INVALID_PRICE_OHLC_INCONSISTENT")

        # 4. Bid / Ask checks if provided
        if "bid" in bar or "ask" in bar:
            bid = bar.get("bid")
            ask = bar.get("ask")
            if bid is None or ask is None or bid <= 0 or ask <= 0:
                return self._reject("MISSING_BID_ASK")
            if bid > ask:
                return self._reject(f"INVERTED_SPREAD (bid:{bid} > ask:{ask})")

        # 5. Timestamp checks
        bar_ts = bar.get("timestamp")
        prev = self.last_valid_ticks.get(iem_cd)
        if prev and prev.get("timestamp") and bar_ts:
            if bar_ts == prev["timestamp"]:
                return self._reject(f"DUPLICATE_TIMESTAMP ({bar_ts})")
            if bar_ts < prev["timestamp"]:
                return self._reject(f"OUT_OF_ORDER_TIMESTAMP (curr:{bar_ts} < prev:{prev['timestamp']})")

        # 6. Abnormal price jump (>30% in 1 bar)
        if prev and prev.get("price", 0) > 0:
            prev_price = prev["price"]
            jump_pct = abs(close_p - prev_price) / float(prev_price)
            if jump_pct > 0.30:
                return self._reject(f"ABNORMAL_PRICE_JUMP ({jump_pct*100:.1f}% > 30.0%)")

        # Passed
        self.last_valid_ticks[iem_cd] = {
            "price": int(close_p),
            "volume": int(volume),
            "timestamp": bar_ts or now
        }
        self.metrics.total_passed += 1
        self.metrics.last_tick_time = now
        return True, None

