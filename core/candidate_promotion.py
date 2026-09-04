"""후보 승격 및 동적 워치리스트 관리 엔진 (Candidate Promotion Engine v6.0)
- Execution-Grade Specification v6.0 (Section 4, 9, 40, 41, 42 준수)
- 전체 시장 2,670+개 종목의 상태 머신 관리
- INACTIVE -> WATCH -> ACTIVE -> SIGNAL -> POSITION -> COOLDOWN
- 이벤트 강도에 따른 동적 승격 및 강등
- 즉시 재감지 (Immediate Re-detection): 탈락 종목도 재발생 시 즉시 재승격
"""

import logging
from datetime import datetime
from typing import Dict, List, Optional, Tuple
from core.models import SymbolInfo, SymbolState, CandidatePriority, MarketEvent
from core.symbol_store import SymbolStateStore
from core.event_detector import EventDetector
from core.aggregator import CandleAggregator

logger = logging.getLogger("CandidatePromotion")


class CandidatePromotionEngine:
    def __init__(self, store: SymbolStateStore):
        self.store = store

    def process_event_evaluation(
        self,
        iem_cd: str,
        agg: CandleAggregator,
        now: datetime,
        execution_intensity: float = 100.0,
        obi: float = 0.0,
        has_news: bool = False,
        rank_surged: bool = False
    ) -> Tuple[SymbolState, float, List[MarketEvent], Dict[str, Any]]:
        """
        한 종목의 이벤트를 평가하고 상태를 승격/유지/강등
        """
        empty_patterns = {
            "is_ignition": False, "is_acceleration": False,
            "is_compression_breakout": False, "is_strong_chart": False,
            "is_chase_forbidden": False, "is_fake_breakout": False
        }
        sym = self.store.get(iem_cd)
        if not sym:
            return SymbolState.INACTIVE, 0.0, [], empty_patterns

        # 1. 쿨다운 상태 점검 (손절 후 일정 시간 거래 제한)
        if sym.state == SymbolState.COOLDOWN:
            if sym.cooldown_until and now < sym.cooldown_until:
                return SymbolState.COOLDOWN, sym.event_score, [], empty_patterns
            else:

                # 쿨다운 만료 시 INACTIVE로 복귀 후 이벤트 평가 진행
                self.store.transition_state(iem_cd, SymbolState.INACTIVE, reason="쿨다운 만료")

        # 2. 포지션 보유 중인 종목은 상태 유지
        if sym.state == SymbolState.POSITION:
            return SymbolState.POSITION, sym.event_score, [], empty_patterns

        # 3. 이벤트 평가 및 점수 산출 (EventDetector)
        score, events, priority, patterns = EventDetector.evaluate_events(
            sym, agg, now,
            execution_intensity=execution_intensity,
            obi=obi,
            has_news=has_news,
            rank_surged=rank_surged
        )

        old_state = sym.state

        # 4. 상태 머신 승격 및 강등 규칙 (Section 9 & 40)
        # 80점 이상 (Priority 1) 또는 65점 이상 (Priority 2) -> ACTIVE 승격
        if priority in (CandidatePriority.PRIORITY_1, CandidatePriority.PRIORITY_2):
            if old_state != SymbolState.ACTIVE and old_state != SymbolState.SIGNAL:
                self.store.promote(iem_cd, SymbolState.ACTIVE, reason=f"이벤트 점수 {score:.1f}점 ({priority.value})")
            new_state = SymbolState.ACTIVE

        # 50~64점 -> WATCH 승격 또는 유지
        elif priority == CandidatePriority.WATCH:
            if old_state == SymbolState.ACTIVE:
                # ACTIVE에서 관심도 하락 시 WATCH로 강등 (Section 41)
                self.store.demote(iem_cd, SymbolState.WATCH, reason=f"점수 하락 ({score:.1f}점)")
            elif old_state == SymbolState.INACTIVE:
                self.store.promote(iem_cd, SymbolState.WATCH, reason=f"관심 이벤트 발생 ({score:.1f}점)")
            new_state = SymbolState.WATCH

        # 49점 이하 -> INACTIVE (이벤트 소멸 강등 또는 유지)
        else:
            if old_state in (SymbolState.ACTIVE, SymbolState.WATCH):
                self.store.demote(iem_cd, SymbolState.INACTIVE, reason=f"이벤트 종료 ({score:.1f}점)")
            new_state = SymbolState.INACTIVE

        return new_state, score, events, patterns


    def get_promoted_candidates(self) -> List[SymbolInfo]:
        """정밀 분석 및 신호 검증 대상 (ACTIVE 및 SIGNAL) 리스트 반환"""
        actives = self.store.get_by_state(SymbolState.ACTIVE)
        signals = self.store.get_by_state(SymbolState.SIGNAL)
        all_candidates = actives + signals
        # 이벤트 점수 높은 순으로 정렬
        all_candidates.sort(key=lambda s: s.event_score, reverse=True)
        return all_candidates
