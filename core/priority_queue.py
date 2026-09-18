"""[FINAL MASTER v11.0] 이벤트 폭풍 우선순위 큐 및 중복 방지 엔진 (Priority Queue & Dedupe v11.0)
- Section 53: Event Storm 대응 다중 기준 우선순위 큐 (Expected Net R -> Setup Score -> Liquidity -> Execution Quality -> RS)
- Section 54: 동일 종목 3~5초 Event Dedupe Window 및 실질 이벤트(신고가/거래량 폭발) 바이패스
"""

import heapq
from datetime import datetime, timedelta
from typing import Dict, List, Any, Optional, Tuple
from dataclasses import dataclass, field

from core.models import TradeSignal, SymbolInfo


@dataclass(order=True)
class PrioritizedEventItem:
    """우선순위 큐 항목 (Section 53)"""
    priority_tuple: Tuple[float, float, float, float, float]
    timestamp: datetime = field(compare=False)
    iem_cd: str = field(compare=False)
    item_data: Any = field(compare=False)


class EventPriorityQueue:
    """수십~수백 개 동시 이벤트 발생 시 기대값 및 셋업 점수 기반 우선순위 큐"""

    def __init__(self):
        self._heap: List[PrioritizedEventItem] = []

    def push(
        self,
        item_data: Any,
        expected_net_r: float = 0.0,
        setup_score: float = 0.0,
        liquidity: float = 0.0,
        spread_ratio: float = 0.001,
        relative_strength: float = 0.0,
        timestamp: Optional[datetime] = None
    ):
        """
        우선순위 계산 및 힙 삽입
        heapq는 최소 힙이므로 내림차순 정렬을 위해 음수 변환
        우선순위:
        1. Expected Net R (높을수록)
        2. Setup Score (높을수록)
        3. Liquidity (높을수록)
        4. Spread Ratio (낮을수록 좋으므로 양수 그대로)
        5. Relative Strength (높을수록)
        """
        ts = timestamp or datetime.now()
        iem_cd = getattr(item_data, "iem_cd", str(item_data))

        # 음수 튜플로 변환하여 최고 우선순위가 먼저 나오도록 구성
        p_tuple = (
            -round(expected_net_r, 4),
            -round(setup_score, 2),
            -round(liquidity, 0),
            round(spread_ratio, 6),
            -round(relative_strength, 4)
        )
        entry = PrioritizedEventItem(priority_tuple=p_tuple, timestamp=ts, iem_cd=iem_cd, item_data=item_data)
        heapq.heappush(self._heap, entry)

    def pop(self) -> Optional[Any]:
        if not self._heap:
            return None
        entry = heapq.heappop(self._heap)
        return entry.item_data

    def pop_batch(self, max_items: Optional[int] = None) -> List[Any]:
        """우선순위 상위 항목 배치 인출 (max_items 미지정 시 전체 인출하여 개수 제한 배제)"""
        results = []
        if max_items is None or max_items <= 0:
            while self._heap:
                results.append(heapq.heappop(self._heap).item_data)
        else:
            while self._heap and len(results) < max_items:
                results.append(heapq.heappop(self._heap).item_data)
        return results

    def __len__(self) -> int:
        return len(self._heap)

    def is_empty(self) -> bool:
        return len(self._heap) == 0


class EventDeduplicator:
    """Section 54: 동일 종목 3~5초 Event Dedupe Window 및 실질 신이벤트 허용 엔진"""

    def __init__(self, window_seconds: float = 4.0):
        self.window_seconds = window_seconds
        # { (iem_cd, event_type): {"timestamp": datetime, "last_price": int, "last_vol": int, "last_high": int} }
        self._history: Dict[Tuple[str, str], Dict[str, Any]] = {}

    def should_process(
        self,
        iem_cd: str,
        event_type: str,
        price: int,
        volume: int,
        now: datetime,
        is_new_high: bool = False
    ) -> bool:
        """
        동일 이벤트 중복 여부 판정
        - 동일 종목+유형 이벤트가 3~5초 이내면 무시
        - 단, 새로운 가격돌파(신고가) 또는 거래량 폭발은 실질 이벤트로 간주하여 즉시 처리
        """
        key = (iem_cd, event_type)
        prev = self._history.get(key)

        if not prev:
            self._history[key] = {
                "timestamp": now,
                "last_price": price,
                "last_vol": volume,
                "last_high": price
            }
            return True

        elapsed = (now - prev["timestamp"]).total_seconds()

        # 윈도우 시간 경과 시 즉시 승인
        if elapsed >= self.window_seconds:
            prev["timestamp"] = now
            prev["last_price"] = price
            prev["last_vol"] = volume
            if price > prev["last_high"]:
                prev["last_high"] = price
            return True

        # 윈도우 내부라도 실질적 이벤트(새로운 고점 돌파 또는 거래량 2배 이상 급증) 발생 시 바이패스
        if is_new_high or price > prev["last_high"]:
            prev["timestamp"] = now
            prev["last_price"] = price
            prev["last_high"] = max(price, prev["last_high"])
            return True

        if prev["last_vol"] > 0 and volume >= prev["last_vol"] * 2.0:
            prev["timestamp"] = now
            prev["last_vol"] = volume
            return True

        # 중복 이벤트 차단
        return False
