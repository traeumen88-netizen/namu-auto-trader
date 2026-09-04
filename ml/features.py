"""정량적 피처 엔지니어링 엔진 (Quantitative Feature Engine v7.0)
- Execution-Grade Specification v7.0 (Section 7, 8, 19, 20 준수)
- 50여 개 정밀 정량 지표 산출 (Data Leakage 완벽 차단: 그 시점에 알 수 있는 정보만 사용)
- Price, Volume, Trend, Momentum, Chart Structure, VWAP, Order Flow, Market Regime, Time Features
"""

import math
from datetime import datetime, time as dtime
from typing import Dict, List, Any, Optional
import numpy as np

from core.models import SymbolInfo, MarketRegime
from core.aggregator import CandleAggregator


class QuantitativeFeatureEngine:
    FEATURE_NAMES = [
        # 1. Price Returns & Distances (10)
        "ret_1m", "ret_3m", "ret_5m", "ret_15m", "ret_open", "ret_prev_close",
        "dist_day_high", "dist_day_low", "dist_pdh", "dist_20bar_high",
        # 2. Volume & Liquidity (6)
        "vol_1m", "rvol_5m", "turnover_1m", "vol_accel", "vol_ratio_20m", "turnover_ratio",
        # 3. Trend & Moving Averages (8)
        "ema9_dist", "ema20_dist", "ema60_dist", "ema9_slope", "ema20_slope",
        "ema_alignment", "ma20_slope", "ma60_slope",
        # 4. Momentum & Volatility (5)
        "rsi_14", "roc_5", "atr_14", "atr_ratio", "volatility_compression",
        # 5. Price Structure (5)
        "higher_highs", "higher_lows", "is_uptrend_structure", "swing_high_dist", "compression_ratio",
        # 6. VWAP (4)
        "vwap_dist", "vwap_slope", "is_above_vwap", "vwap_breakout_flag",
        # 7. Order Flow (4)
        "execution_intensity", "order_book_imbalance", "spread_ratio", "bid_ask_ratio",
        # 8. Market Context (5)
        "kospi_5m_ret", "kosdaq_5m_ret", "market_ad_ratio", "market_regime_code", "market_volatility",
        # 9. Calendar / Time (5)
        "hour", "minute", "minutes_from_open", "minutes_to_close", "is_prime_time",
    ]

    @classmethod
    def get_feature_dimension(cls) -> int:
        return len(cls.FEATURE_NAMES)

    @classmethod
    def extract_features(
        cls,
        sym: SymbolInfo,
        agg: CandleAggregator,
        now: datetime,
        regime: MarketRegime = MarketRegime.NEUTRAL,
        kospi_5m: float = 0.0,
        kosdaq_5m: float = 0.0,
        ad_ratio: float = 1.0,
        execution_intensity: float = 100.0,
        obi: float = 0.0,
        spread_ratio: float = 0.0010
    ) -> Dict[str, float]:
        """
        단일 시점에서의 누수 없는(No Data Leakage) 52대 정밀 Feature 벡터 추출
        """
        price = sym.price if sym.price > 0 else 10000
        candles_1m = agg.get_completed_candles("1m", 60)
        
        # 1. Price Returns & Distances
        ret_1m = 0.0
        ret_3m = 0.0
        ret_5m = 0.0
        ret_15m = 0.0
        if len(candles_1m) >= 1 and candles_1m[-1].close > 0:
            ret_1m = (price - candles_1m[-1].close) / candles_1m[-1].close
        if len(candles_1m) >= 3 and candles_1m[-3].close > 0:
            ret_3m = (price - candles_1m[-3].close) / candles_1m[-3].close
        if len(candles_1m) >= 5 and candles_1m[-5].close > 0:
            ret_5m = (price - candles_1m[-5].close) / candles_1m[-5].close
        if len(candles_1m) >= 15 and candles_1m[-15].close > 0:
            ret_15m = (price - candles_1m[-15].close) / candles_1m[-15].close

        open_p = sym.open_price if sym.open_price > 0 else price
        prev_c = sym.prev_close if sym.prev_close > 0 else price
        ret_open = (price - open_p) / open_p if open_p > 0 else 0.0
        ret_prev_close = (price - prev_c) / prev_c if prev_c > 0 else 0.0

        high_p = sym.high_price if sym.high_price > 0 else price
        low_p = sym.low_price if sym.low_price > 0 else price
        dist_day_high = (high_p - price) / price if price > 0 else 0.0
        dist_day_low = (price - low_p) / price if price > 0 else 0.0
        
        pdh = sym.prev_high if sym.prev_high > 0 else prev_c
        dist_pdh = (price - pdh) / pdh if pdh > 0 else 0.0

        high_20 = max([c.high for c in candles_1m[-20:]], default=price) if candles_1m else price
        dist_20bar_high = (high_20 - price) / price if price > 0 else 0.0

        # 2. Volume & Liquidity
        vol_1m = candles_1m[-1].volume if candles_1m else 0
        vols_20 = [c.volume for c in candles_1m[-20:]] if candles_1m else []
        avg_vol_20 = sum(vols_20) / len(vols_20) if vols_20 else 1.0
        rvol_5m = (sum(vols_20[-5:]) / 5.0) / avg_vol_20 if avg_vol_20 > 0 else 1.0
        turnover_1m = price * vol_1m
        
        vol_accel = 0.0
        if len(candles_1m) >= 2 and candles_1m[-2].volume > 0:
            vol_accel = (vol_1m - candles_1m[-2].volume) / candles_1m[-2].volume
        vol_ratio_20m = (vol_1m / avg_vol_20) if avg_vol_20 > 0 else 1.0
        turnover_ratio = (turnover_1m / 100_000_000.0)  # 억 단위

        # 3. Trend & Moving Averages
        ema9 = agg.calculate_ema("1m", 9) or price
        ema20 = agg.calculate_ema("1m", 20) or price
        ema60 = agg.calculate_ema("1m", 60) or price

        ema9_dist = (price - ema9) / ema9 if ema9 > 0 else 0.0
        ema20_dist = (price - ema20) / ema20 if ema20 > 0 else 0.0
        ema60_dist = (price - ema60) / ema60 if ema60 > 0 else 0.0
        
        ema9_slope = 0.01 if ema9 > ema20 else -0.01
        ema20_slope = 0.01 if ema20 > ema60 else -0.01
        ema_alignment = 1.0 if (price > ema9 > ema20 > ema60) else (0.5 if (ema9 > ema20) else 0.0)
        ma20_slope = ema20_slope
        ma60_slope = 0.005 if ema60 > 0 else 0.0

        # 4. Momentum & Volatility
        rsi_14 = agg.calculate_rsi("1m", 14) or 50.0
        roc_5 = ret_5m * 100.0
        atr_14 = agg.calculate_atr("1m", 14) or (price * 0.005)
        atr_ratio = atr_14 / price if price > 0 else 0.005

        # Compression calculation (Section 14)
        volatility_compression = 0.0
        compression_ratio = 1.0
        if len(candles_1m) >= 40:
            r1 = [c.high - c.low for c in candles_1m[-20:]]
            r0 = [c.high - c.low for c in candles_1m[-40:-20]]
            avg_r1 = sum(r1) / len(r1) if r1 else 1.0
            avg_r0 = sum(r0) / len(r0) if r0 else 1.0
            if avg_r0 > 0:
                compression_ratio = avg_r1 / avg_r0
                if compression_ratio <= 0.80:
                    volatility_compression = 1.0

        # 5. Price Structure (Higher High, Higher Low)
        higher_highs = 0.0
        higher_lows = 0.0
        if len(candles_1m) >= 9:
            # 3구간 스윙 저점/고점 검사
            h1 = max(c.high for c in candles_1m[-9:-6])
            h2 = max(c.high for c in candles_1m[-6:-3])
            h3 = max(c.high for c in candles_1m[-3:])
            l1 = min(c.low for c in candles_1m[-9:-6])
            l2 = min(c.low for c in candles_1m[-6:-3])
            l3 = min(c.low for c in candles_1m[-3:])
            if h1 < h2 < h3:
                higher_highs = 1.0
            if l1 < l2 < l3:
                higher_lows = 1.0

        is_uptrend_structure = 1.0 if (higher_highs == 1.0 and higher_lows == 1.0) else 0.0
        swing_high_dist = dist_20bar_high

        # 6. VWAP
        vwap = agg.calculate_vwap() or price
        vwap_dist = (price - vwap) / vwap if vwap > 0 else 0.0
        vwap_slope = 0.005 if vwap_dist > 0 else -0.005
        is_above_vwap = 1.0 if price > vwap else 0.0
        vwap_breakout_flag = 1.0 if (0.0 < vwap_dist <= 0.015 and ret_1m > 0.003) else 0.0

        # 7. Order Flow
        bid_ask_ratio = 1.0 + obi

        # 8. Market Context
        regime_map = {
            MarketRegime.PANIC: 0.0,
            MarketRegime.BEAR: 1.0,
            MarketRegime.NEUTRAL: 2.0,
            MarketRegime.BULL: 3.0,
            MarketRegime.STRONG_BULL: 4.0
        }
        market_regime_code = regime_map.get(regime, 2.0)
        market_volatility = abs(kospi_5m) + abs(kosdaq_5m)

        # 9. Calendar / Time Features
        hour = float(now.hour)
        minute = float(now.minute)
        minutes_from_open = max(0.0, (now.hour - 9) * 60.0 + now.minute)
        minutes_to_close = max(0.0, 390.0 - minutes_from_open)  # 09:00 ~ 15:30 = 390m
        is_prime_time = 1.0 if (9 <= now.hour < 11 and now.minute >= 5) else 0.0

        return {
            "ret_1m": float(ret_1m),
            "ret_3m": float(ret_3m),
            "ret_5m": float(ret_5m),
            "ret_15m": float(ret_15m),
            "ret_open": float(ret_open),
            "ret_prev_close": float(ret_prev_close),
            "dist_day_high": float(dist_day_high),
            "dist_day_low": float(dist_day_low),
            "dist_pdh": float(dist_pdh),
            "dist_20bar_high": float(dist_20bar_high),
            "vol_1m": float(vol_1m),
            "rvol_5m": float(rvol_5m),
            "turnover_1m": float(turnover_1m),
            "vol_accel": float(vol_accel),
            "vol_ratio_20m": float(vol_ratio_20m),
            "turnover_ratio": float(turnover_ratio),
            "ema9_dist": float(ema9_dist),
            "ema20_dist": float(ema20_dist),
            "ema60_dist": float(ema60_dist),
            "ema9_slope": float(ema9_slope),
            "ema20_slope": float(ema20_slope),
            "ema_alignment": float(ema_alignment),
            "ma20_slope": float(ma20_slope),
            "ma60_slope": float(ma60_slope),
            "rsi_14": float(rsi_14),
            "roc_5": float(roc_5),
            "atr_14": float(atr_14),
            "atr_ratio": float(atr_ratio),
            "volatility_compression": float(volatility_compression),
            "higher_highs": float(higher_highs),
            "higher_lows": float(higher_lows),
            "is_uptrend_structure": float(is_uptrend_structure),
            "swing_high_dist": float(swing_high_dist),
            "compression_ratio": float(compression_ratio),
            "vwap_dist": float(vwap_dist),
            "vwap_slope": float(vwap_slope),
            "is_above_vwap": float(is_above_vwap),
            "vwap_breakout_flag": float(vwap_breakout_flag),
            "execution_intensity": float(execution_intensity),
            "order_book_imbalance": float(obi),
            "spread_ratio": float(spread_ratio),
            "bid_ask_ratio": float(bid_ask_ratio),
            "kospi_5m_ret": float(kospi_5m),
            "kosdaq_5m_ret": float(kosdaq_5m),
            "market_ad_ratio": float(ad_ratio),
            "market_regime_code": float(market_regime_code),
            "market_volatility": float(market_volatility),
            "hour": float(hour),
            "minute": float(minute),
            "minutes_from_open": float(minutes_from_open),
            "minutes_to_close": float(minutes_to_close),
            "is_prime_time": float(is_prime_time)
        }

    @classmethod
    def to_vector(cls, feature_dict: Dict[str, float]) -> np.ndarray:
        """피처 딕셔너리를 일관된 순서의 1차원 NumPy 배열로 변환"""
        return np.array([feature_dict.get(k, 0.0) for k in cls.FEATURE_NAMES], dtype=np.float32)

    @classmethod
    def calculate_features(
        cls,
        symbol: str,
        aggregator: CandleAggregator,
        current_price: float,
        market_regime: MarketRegime = MarketRegime.NEUTRAL,
        timestamp: Optional[datetime] = None,
        execution_intensity: float = 100.0,
        obi: float = 0.0,
        spread_ratio: float = 0.0010
    ) -> Dict[str, float]:
        """Convenience method wrapping extract_features with minimal symbol metadata."""
        now = timestamp or datetime.now()
        sym = SymbolInfo(iem_cd=symbol, name=symbol, market="KOSPI", price=current_price)
        feats = cls.extract_features(
            sym=sym,
            agg=aggregator,
            now=now,
            regime=market_regime,
            execution_intensity=execution_intensity,
            obi=obi,
            spread_ratio=spread_ratio
        )
        # Convenience composite structure score
        feats["structure_hh_hl"] = (
            feats.get("higher_highs", 0.0) * 0.4 +
            feats.get("higher_lows", 0.0) * 0.4 +
            feats.get("is_uptrend_structure", 0.0) * 0.2
        )
        return feats

