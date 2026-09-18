"""[FINAL MASTER v16.0] 후보 승격 및 동적 워치리스트 관리 엔진 (core/candidate_promotion.py)
Section 14: Candidate Engine
- Candidate는 BUY 신호가 아님 (관찰 가치가 있는 후보 종목군)
- 느슨한 조건 구성: 가격 급변, 거래량 증가, RVOL 증가, 고점 갱신, VWAP/EMA 변화, 변동성 확대 등 중 하나 이상 만족 시 승격
- Hysteresis Band 및 관찰 TTL(120초) 적용으로 찰나의 틱 변동에 의한 잦은 강등(Flapping) 방지
- 즉시 재감지(Immediate Re-detection): 탈락 종목도 재발생 시 즉시 재승격
"""

import logging
from datetime import datetime, timedelta
from typing import Dict, List, Optional, Tuple, Any
from core.models import SymbolInfo, SymbolState, CandidatePriority, MarketEvent
from core.symbol_store import SymbolStateStore
from core.event_detector import EventDetector
from core.aggregator import CandleAggregator

logger = logging.getLogger("CandidatePromotion")


class CandidatePromotionEngine:
    def __init__(self, store: SymbolStateStore, candidate_ttl_seconds: int = 120):
        self.store = store
        self.candidate_ttl_seconds = candidate_ttl_seconds
        # 종목별 마지막 승격 시각: {iem_cd: datetime}
        self.promoted_at: Dict[str, datetime] = {}

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
            "is_chase_forbidden": False, "is_fake_breakout": False,
            "is_vwap_reclaim": False, "is_ema_pullback": False
        }
        sym = self.store.get(iem_cd)
        if not sym:
            return SymbolState.INACTIVE, 0.0, [], empty_patterns

        # 1. 쿨다운 상태 점검 (손절 후 일정 시간 거래 제한)
        if sym.state == SymbolState.COOLDOWN:
            if sym.cooldown_until and now < sym.cooldown_until:
                return SymbolState.COOLDOWN, sym.event_score, [], empty_patterns
            else:
                self.store.transition_state(iem_cd, SymbolState.INACTIVE, reason="쿨다운 만료")

        # 2. 포지션 보유 여부와 무관하게 시장 이벤트 평가는 항상 지속 (계좌별 독립 판단)
        # SymbolStateStore는 시장 공통 상태만 관리하며, 실제 매매 차단은 AccountContext.position_manager가 수행

        # 3. 이벤트 평가 및 점수 산출
        score, events, priority, patterns = EventDetector.evaluate_events(
            sym, agg, now,
            execution_intensity=execution_intensity,
            obi=obi,
            has_news=has_news,
            rank_surged=rank_surged
        )

        old_state = sym.state

        # 4. 상태 머신 승격 및 강등 규칙 (Section 14)
        # Priority 1 (75점+) 또는 Priority 2 (55점+) -> ACTIVE 고속 승격
        if priority in (CandidatePriority.PRIORITY_1, CandidatePriority.PRIORITY_2):
            if old_state not in (SymbolState.ACTIVE, SymbolState.SIGNAL):
                self.store.promote(iem_cd, SymbolState.ACTIVE, reason=f"이벤트 점수 {score:.1f}점 ({priority.value})")
            self.promoted_at[iem_cd] = now
            new_state = SymbolState.ACTIVE

        # WATCH 단계 (35~54점 또는 1개 이상 이벤트 포착) -> WATCH 승격 또는 유지
        elif priority == CandidatePriority.WATCH or len(events) >= 1:
            if old_state == SymbolState.ACTIVE:
                # ACTIVE 상태였던 종목은 TTL(120초) 내에는 유지
                last_p = self.promoted_at.get(iem_cd, now)
                if (now - last_p).total_seconds() <= self.candidate_ttl_seconds:
                    new_state = SymbolState.ACTIVE
                else:
                    self.store.demote(iem_cd, SymbolState.WATCH, reason=f"관심도 완화 ({score:.1f}점)")
                    new_state = SymbolState.WATCH
            elif old_state == SymbolState.INACTIVE:
                self.store.promote(iem_cd, SymbolState.WATCH, reason=f"수급 이벤트 포착 ({score:.1f}점)")
                self.promoted_at[iem_cd] = now
                new_state = SymbolState.WATCH
            else:
                new_state = SymbolState.WATCH

        # INACTIVE 강등 검사 (TTL 경과 확인)
        else:
            last_p = self.promoted_at.get(iem_cd)
            if last_p and (now - last_p).total_seconds() <= self.candidate_ttl_seconds:
                # TTL 기간 중에는 임의 탈락 방지 (Hysteresis 관찰 유지)
                new_state = old_state
            else:
                if old_state in (SymbolState.ACTIVE, SymbolState.WATCH):
                    self.store.demote(iem_cd, SymbolState.INACTIVE, reason=f"이벤트 종료 ({score:.1f}점)")
                new_state = SymbolState.INACTIVE

        return new_state, score, events, patterns

    def get_promoted_candidates(self) -> List[SymbolInfo]:
        """정밀 분석 및 신호 검증 대상 (ACTIVE, SIGNAL 및 이벤트 발생한 WATCH) 리스트 반환"""
        actives = self.store.get_by_state(SymbolState.ACTIVE)
        signals = self.store.get_by_state(SymbolState.SIGNAL)
        watches = [s for s in self.store.get_by_state(SymbolState.WATCH) if s.event_score >= 35.0 or len(s.active_events) >= 1]
        positions = [s for s in self.store.get_by_state(SymbolState.POSITION) if s.event_score >= 35.0 or len(s.active_events) >= 1]
        all_candidates = actives + signals + watches + positions

        seen = set()
        unique_candidates = []
        for c in all_candidates:
            if c.iem_cd not in seen:
                seen.add(c.iem_cd)
                unique_candidates.append(c)
        unique_candidates.sort(key=lambda s: s.event_score, reverse=True)
        return unique_candidates

    def on_event_detected(self, sym: SymbolInfo, score: float, reason: str, now: datetime):
        sym.event_score = score
        if reason not in sym.active_events:
            sym.active_events.append(reason)
        sym.last_event_time = now

    def evaluate_promotion(self, sym: SymbolInfo, now: datetime) -> bool:
        if sym.event_score >= 35.0 or len(sym.active_events) >= 1:
            self.store.promote(sym.iem_cd, SymbolState.CANDIDATE, reason="; ".join(sym.active_events) if sym.active_events else "Score promotion")
            self.promoted_at[sym.iem_cd] = now
            return True
        return False


CandidatePromoter = CandidatePromotionEngine

