"""저비용 시장 전체 스캐너 및 이벤트 감지 버스 (Low-Cost Market Scanner v6.0)
- Execution-Grade Specification v6.0 (Section 3, 6, 7, 44 준수)
- 2,670+ KOSPI/KOSDAQ 전체 종목에 대한 저비용 실시간 이벤트 모니터링
- 개별 REST 1초 Polling 금지: 스트리밍/피드/이벤트 기반 처리
- 이벤트 감지 시 정밀 분석 대상(ACTIVE)으로 고속 승격(Candidate Promotion)
- 메모리 효율적 캔들 애그리게이션 관리
"""

import logging
from datetime import datetime
from typing import Dict, List, Optional, Tuple, Any
from core.models import SymbolInfo, SymbolState, Tick, TradeSignal, MarketRegime
from core.symbol_store import SymbolStateStore
from core.candidate_promotion import CandidatePromotionEngine
from core.aggregator import CandleAggregator
from strategies.full_strategy_suite import FullStrategySuite
from strategies.scoring_engine import ScoringEngine

logger = logging.getLogger("LowCostScanner")


class LowCostMarketScanner:
    def __init__(self, store: SymbolStateStore):
        self.store = store
        self.promotion_engine = CandidatePromotionEngine(store)
        
        # 활성(ACTIVE/WATCH/SIGNAL/POSITION) 종목별 고정밀 캔들 애그리게이터 맵
        # 전체 2,670 종목 중 이벤트 발생 종목만 온디맨드로 생성/유지하여 메모리 최적화
        self.active_aggregators: Dict[str, CandleAggregator] = {}

    def get_aggregator(self, iem_cd: str) -> CandleAggregator:
        """종목별 애그리게이터 반환 (없으면 온디맨드 생성)"""
        if iem_cd not in self.active_aggregators:
            self.active_aggregators[iem_cd] = CandleAggregator(iem_cd)
        return self.active_aggregators[iem_cd]

    def on_market_tick(
        self,
        iem_cd: str,
        price: int,
        volume: int,
        timestamp: datetime,
        execution_intensity: float = 100.0,
        obi: float = 0.0,
        has_news: bool = False,
        rank_surged: bool = False,
        open_price: int = 0,
        high_price: int = 0,
        low_price: int = 0
    ) -> Tuple[SymbolState, float, Optional[SymbolInfo]]:
        """
        시장 전 종목에서 틱/체결 수신 시 이벤트 감지 및 승격 처리
        - 사전 등록 여부와 상관없이 전체 2,670+ 상장 종목 어디서든 발생한 이벤트를 즉시 포착
        """
        sym = self.store.get(iem_cd)
        if not sym:
            # 마스터에 없는 종목도 동적 자동 등록 (신규 상장주 등 방어)
            sym = SymbolInfo(
                iem_cd=iem_cd,
                name=f"KRX_{iem_cd}",
                market="KOSPI" if iem_cd.startswith("0") else "KOSDAQ",
                price=price
            )
            self.store.register_symbol(sym)

        # 1. 실시간 시세 및 호가 갱신
        self.store.update_quote(
            iem_cd=iem_cd,
            price=price,
            volume=volume,
            turnover=price * volume,
            high=high_price or price,
            low=low_price or price,
            open_price=open_price or price
        )

        # 2. 캔들 애그리게이터에 틱 주입
        agg = self.get_aggregator(iem_cd)
        tick = Tick(timestamp=timestamp, iem_cd=iem_cd, price=price, volume=volume)
        agg.on_tick(tick)

        # 3. 이벤트 평가 및 동적 승격 처리
        state, score, events, patterns = self.promotion_engine.process_event_evaluation(
            iem_cd=iem_cd,
            agg=agg,
            now=timestamp,
            execution_intensity=execution_intensity,
            obi=obi,
            has_news=has_news,
            rank_surged=rank_surged
        )

        return state, score, sym


    def scan_active_signals(
        self,
        regime: MarketRegime,
        now: datetime,
        or_high_map: Dict[str, int] = None,
        daily_candles_cache: Dict[str, List[Dict[str, Any]]] = None
    ) -> List[TradeSignal]:
        """
        승격된 ACTIVE 종목들을 대상으로 11대 단타 + 9대 스윙 전략 신호 생성
        """
        promoted = self.promotion_engine.get_promoted_candidates()
        valid_signals: List[TradeSignal] = []
        or_high_map = or_high_map or {}
        daily_candles_cache = daily_candles_cache or {}

        for sym in promoted:
            agg = self.get_aggregator(sym.iem_cd)
            
            # 패턴 및 지표 검사
            _, _, _, patterns = self.promotion_engine.process_event_evaluation(
                sym.iem_cd, agg, now
            )

            # [1] 단타 11대 전략 평가
            intra_sigs = FullStrategySuite.evaluate_intraday_all(
                sym=sym,
                agg=agg,
                regime=regime,
                now=now,
                patterns=patterns,
                spread_ratio=0.001,
                or_high=or_high_map.get(sym.iem_cd),
                has_news=any("뉴스" in ev for ev in sym.active_events)
            )
            for sig in intra_sigs:
                self.store.promote(sym.iem_cd, SymbolState.SIGNAL, reason=f"단타 신호 생성: {sig.strategy_id}")
                valid_signals.append(sig)

            # [2] 스윙 9대 전략 평가
            daily_candles = daily_candles_cache.get(sym.iem_cd, [])
            if daily_candles:
                swing_sigs = FullStrategySuite.evaluate_swing_all(
                    sym=sym,
                    daily_candles=daily_candles,
                    regime=regime,
                    now=now,
                    has_catalyst=any("뉴스" in ev for ev in sym.active_events),
                    agg=agg
                )

                for sig in swing_sigs:
                    self.store.promote(sym.iem_cd, SymbolState.SIGNAL, reason=f"스윙 신호 생성: {sig.strategy_id}")
                    valid_signals.append(sig)

        return valid_signals
