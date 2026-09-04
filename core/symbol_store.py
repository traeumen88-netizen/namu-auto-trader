"""전체 시장 종목 상태 관리 저장소 (Symbol State Store v6.0)
- Execution-Grade Specification v6.0 (Section 4 & Section 44 준수)
- 2,670+ 전체 상장 종목을 메모리에 인덱싱하여 마이크로초 단위 상태 전이 보장
- INACTIVE -> WATCH -> ACTIVE -> SIGNAL -> POSITION -> COOLDOWN 상태 머신
- Section 69: DISPLAY LIMIT(화면표시 상위 10/20)과 SCANNER LIMIT(전체 시장 감시)의 완전 분리
"""

import logging
from datetime import datetime, timedelta
from typing import Dict, List, Set, Optional, Tuple
from core.models import SymbolInfo, SymbolState, CandidatePriority

logger = logging.getLogger("SymbolStateStore")


class SymbolStateStore:
    def __init__(self, symbols: Dict[str, SymbolInfo] = None):
        self._symbols: Dict[str, SymbolInfo] = symbols or {}
        
        # O(1) 상태별 인덱스
        self._state_index: Dict[SymbolState, Set[str]] = {
            state: set() for state in SymbolState
        }
        
        # 초기 상태 인덱싱 (기본 INACTIVE)
        for code, sym in self._symbols.items():
            self._state_index[sym.state].add(code)

    def register_symbol(self, sym: SymbolInfo):
        """신규 종목 등록"""
        self._symbols[sym.iem_cd] = sym
        self._state_index[sym.state].add(sym.iem_cd)

    def get(self, iem_cd: str) -> Optional[SymbolInfo]:
        """종목 조회"""
        return self._symbols.get(iem_cd)

    def get_all(self) -> Dict[str, SymbolInfo]:
        """전체 종목 딕셔너리 반환"""
        return self._symbols

    def total_count(self) -> int:
        """전체 상장 종목 수"""
        return len(self._symbols)

    def kospi_count(self) -> int:
        return sum(1 for s in self._symbols.values() if s.market == "KOSPI")

    def kosdaq_count(self) -> int:
        return sum(1 for s in self._symbols.values() if s.market == "KOSDAQ")

    def get_by_state(self, state: SymbolState) -> List[SymbolInfo]:
        """특정 상태의 종목 리스트 반환"""
        codes = self._state_index.get(state, set())
        return [self._symbols[c] for c in codes if c in self._symbols]

    def transition_state(self, iem_cd: str, new_state: SymbolState, reason: str = "") -> bool:
        """
        종목 상태 전이 (INACTIVE <-> WATCH <-> ACTIVE <-> SIGNAL <-> POSITION <-> COOLDOWN)
        """
        sym = self._symbols.get(iem_cd)
        if not sym:
            return False

        old_state = sym.state
        if old_state == new_state:
            return True

        # 이전 인덱스에서 제거 후 신규 인덱스에 추가
        self._state_index[old_state].discard(iem_cd)
        self._state_index[new_state].add(iem_cd)
        sym.state = new_state

        logger.debug(f"[상태 전이] {sym.name}({iem_cd}): {old_state.value} -> {new_state.value} ({reason})")
        return True

    def promote(self, iem_cd: str, target_state: SymbolState, reason: str = "") -> bool:
        """승격 (INACTIVE -> WATCH -> ACTIVE -> SIGNAL)"""
        return self.transition_state(iem_cd, target_state, reason=f"PROMOTION: {reason}")

    def demote(self, iem_cd: str, target_state: SymbolState, reason: str = "") -> bool:
        """강등 (ACTIVE -> WATCH -> INACTIVE)"""
        return self.transition_state(iem_cd, target_state, reason=f"DEMOTION: {reason}")

    def update_quote(
        self,
        iem_cd: str,
        price: int,
        volume: int = 0,
        turnover: int = 0,
        high: int = 0,
        low: int = 0,
        open_price: int = 0
    ):
        """실시간 시세 데이터 갱신"""
        sym = self._symbols.get(iem_cd)
        if not sym:
            return
        sym.price = price
        if volume > 0:
            sym.acml_vol = volume
        if turnover > 0:
            sym.acml_trde_amt = turnover
        if high > sym.high_price:
            sym.high_price = high
        if low > 0 and (sym.low_price == 0 or low < sym.low_price):
            sym.low_price = low
        if open_price > 0 and sym.open_price == 0:
            sym.open_price = open_price

    def set_cooldown(self, iem_cd: str, duration_seconds: int = 1800):
        """손절 후 쿨다운 상태 진입"""
        sym = self._symbols.get(iem_cd)
        if not sym:
            return
        now = datetime.now()
        sym.cooldown_until = now + timedelta(seconds=duration_seconds)
        sym.loss_count_today += 1
        self.transition_state(iem_cd, SymbolState.COOLDOWN, reason=f"쿨다운 설정({duration_seconds}초)")

    def check_cooldown_expiry(self, now: datetime = None):
        """쿨다운 만료 종목을 INACTIVE로 복귀"""
        now = now or datetime.now()
        cooldown_codes = list(self._state_index.get(SymbolState.COOLDOWN, set()))
        for code in cooldown_codes:
            sym = self._symbols.get(code)
            if sym and sym.cooldown_until and now >= sym.cooldown_until:
                sym.cooldown_until = None
                self.transition_state(code, SymbolState.INACTIVE, reason="쿨다운 만료 복귀")

    def get_state_counts(self) -> Dict[str, int]:
        """대시보드 표출용 상태별 종목 수 집계 (Section 59)"""
        return {
            state.value: len(self._state_index[state])
            for state in SymbolState
        }

    def get_display_candidates(self, limit: int = 10) -> List[SymbolInfo]:
        """
        화면 표시용 상위 후보 추출 (Section 69: DISPLAY TOP 10 != SCAN TOP 10)
        - SIGNAL 및 ACTIVE 종목을 이벤트 점수 내림차순으로 정렬하여 반환
        """
        active_codes = self._state_index[SymbolState.ACTIVE] | self._state_index[SymbolState.SIGNAL] | self._state_index[SymbolState.WATCH]
        candidates = [self._symbols[c] for c in active_codes if c in self._symbols]
        candidates.sort(key=lambda s: (s.state == SymbolState.SIGNAL, s.event_score, s.acml_trde_amt), reverse=True)
        return candidates[:limit]
