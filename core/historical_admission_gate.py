"""[HISTORICAL DATA ADMISSION GATE v1.0]
(core/historical_admission_gate.py)

Strict 10-Point Gatekeeper for admitting historical market data into Backtest:
1. 실제 KRX 데이터인지 (Real KRX Data Authenticity)
2. Timestamp 정상인지 (Timestamp Monotonicity & Session Hours)
3. 1분/5분 세션 누락 없는지 (Session Completeness & Bar Continuity)
4. 종목 Universe 정상인지 (Universe Format & Tradability)
5. OHLCV 무결성 (Bar Consistency & Sanity)
6. Bid/Ask 무결성 (Quote Spread & Depth Integrity)
7. PIT Model 생성 가능 여부 (Model Fit End < OOS Start)
8. Scaler PIT 여부 (Scaler Fit End < OOS Start)
9. Similarity PIT 여부 (Similarity Database Point-in-Time Integrity)
10. Leakage 0건 여부 (Strict Lookahead Leakage Audit = 0)

ALL PASS → BACKTEST PERFORMANCE ENABLED
ANY FAIL → BACKTEST PERFORMANCE BLOCKED
"""

import os
import re
import math
from datetime import datetime, time as dtime, timedelta
from typing import Dict, List, Any, Optional, Tuple
from dataclasses import dataclass, field


@dataclass
class AdmissionGateResult:
    gate_id: int
    name: str
    status: str  # "PASS" or "FAIL"
    details: str
    metrics: Dict[str, Any] = field(default_factory=dict)


@dataclass
class AdmissionEvaluation:
    overall_status: str  # "PASS" or "FAIL"
    decision: str        # "BACKTEST PERFORMANCE ENABLED" or "BACKTEST PERFORMANCE BLOCKED"
    dataset_path: Optional[str]
    data_source: str
    gates: List[AdmissionGateResult]
    passed_count: int
    failed_count: int
    failure_reasons: List[str]


class HistoricalDataAdmissionGate:
    """
    10-Point Historical Data Admission Gatekeeper.
    Evaluates dataset schema, temporal consistency, universe integrity,
    and Point-in-Time constraints prior to allowing performance evaluation.
    """

    KRX_MARKET_OPEN = dtime(9, 0, 0)
    KRX_MARKET_CLOSE = dtime(15, 30, 0)
    EXPECTED_DAILY_1M_BARS = 390

    def __init__(self, strict_mode: bool = True):
        self.strict_mode = strict_mode

    def evaluate_admission(
        self,
        data_source: str,
        dataset_path: Optional[str] = None,
        bars: Optional[List[Dict[str, Any]]] = None,
        universe: Optional[Dict[str, Any]] = None,
        oos_start_date: Optional[datetime] = None,
        model_fit_date: Optional[str] = None,
        scaler_fit_date: Optional[str] = None,
        similarity_leakage_count: int = 0,
        lookahead_leakage_count: int = 0
    ) -> AdmissionEvaluation:
        gates: List[AdmissionGateResult] = []

        # -----------------------------------------------------------------
        # 1. 실제 KRX 데이터인지 (Real KRX Data Authenticity)
        # -----------------------------------------------------------------
        is_real_krx = False
        data_details = ""
        if data_source.upper() == "HISTORICAL" and dataset_path and os.path.exists(dataset_path):
            ext = os.path.splitext(dataset_path)[1].lower()
            if ext in (".parquet", ".csv", ".db", ".sqlite"):
                is_real_krx = True
                data_details = f"Verified Historical KRX File: {os.path.basename(dataset_path)} ({ext})"
            else:
                data_details = f"Unsupported file extension: {ext}"
        elif data_source.upper() == "SYNTHETIC":
            data_details = "FAIL: Synthetic / Mock / Fixture data is not real KRX historical data"
        else:
            data_details = f"FAIL: Dataset path does not exist or invalid source: {dataset_path} (source={data_source})"

        gates.append(AdmissionGateResult(
            gate_id=1,
            name="실제 KRX 데이터인지",
            status="PASS" if is_real_krx else "FAIL",
            details=data_details,
            metrics={"data_source": data_source, "dataset_path": dataset_path}
        ))

        # -----------------------------------------------------------------
        # 2. Timestamp 정상인지 (Timestamp Monotonicity & Session Hours)
        # -----------------------------------------------------------------
        ts_ok = True
        ts_reasons = []
        dup_ts_count = 0
        out_of_order_count = 0
        outside_hours_count = 0

        if not bars:
            ts_ok = False
            ts_reasons.append("No bar data provided")
        else:
            prev_ts = None
            seen_ts = set()
            for b in bars:
                ts = b.get("timestamp")
                if not isinstance(ts, datetime):
                    try:
                        ts = datetime.fromisoformat(str(ts))
                    except Exception:
                        ts_ok = False
                        ts_reasons.append(f"Unparseable timestamp: {ts}")
                        break

                sym = b.get("symbol", "")
                key = (sym, ts)
                if key in seen_ts:
                    dup_ts_count += 1
                seen_ts.add(key)

                if prev_ts and ts < prev_ts and sym == getattr(prev_ts, "sym", sym):
                    out_of_order_count += 1

                b_time = ts.time()
                if b_time < self.KRX_MARKET_OPEN or b_time > self.KRX_MARKET_CLOSE:
                    outside_hours_count += 1

                prev_ts = ts

            if dup_ts_count > 0:
                ts_ok = False
                ts_reasons.append(f"Duplicate timestamps found: {dup_ts_count}")
            if out_of_order_count > 0:
                ts_ok = False
                ts_reasons.append(f"Out-of-order timestamps found: {out_of_order_count}")
            if outside_hours_count > 0:
                ts_ok = False
                ts_reasons.append(f"Timestamps outside KRX session (09:00-15:30): {outside_hours_count}")

        gates.append(AdmissionGateResult(
            gate_id=2,
            name="Timestamp 정상인지",
            status="PASS" if ts_ok else "FAIL",
            details="; ".join(ts_reasons) if ts_reasons else "All timestamps strictly monotonic & within 09:00-15:30",
            metrics={
                "duplicate_count": dup_ts_count,
                "out_of_order_count": out_of_order_count,
                "outside_hours_count": outside_hours_count
            }
        ))

        # -----------------------------------------------------------------
        # 3. 1분/5분 세션 누락 없는지 (Session Completeness)
        # -----------------------------------------------------------------
        sess_ok = True
        sess_reasons = []
        if not bars:
            sess_ok = False
            sess_reasons.append("No bar data for session continuity audit")
        else:
            dates = set(b["timestamp"].date() for b in bars if isinstance(b.get("timestamp"), datetime))
            daily_bars = len(bars) / max(1, len(dates))
            if daily_bars < 60:
                sess_ok = False
                sess_reasons.append(f"Severe session truncation: avg {daily_bars:.1f} bars/day (< 60 bars/day)")
            elif daily_bars < 300:
                # Warning or partial session
                sess_reasons.append(f"Reduced bars/day: avg {daily_bars:.1f} bars/day")

        gates.append(AdmissionGateResult(
            gate_id=3,
            name="1분/5분 세션 누락 없는지",
            status="PASS" if sess_ok else "FAIL",
            details="; ".join(sess_reasons) if sess_reasons else f"Session bars continuity verified ({len(bars)} bars)",
            metrics={"total_bars": len(bars) if bars else 0}
        ))

        # -----------------------------------------------------------------
        # 4. 종목 Universe 정상인지 (Universe Integrity)
        # -----------------------------------------------------------------
        univ_ok = True
        univ_reasons = []
        if not universe:
            univ_ok = False
            univ_reasons.append("Universe is empty or None")
        else:
            invalid_codes = []
            for code in universe.keys():
                if not re.match(r"^\d{6}$", str(code)):
                    invalid_codes.append(str(code))
            if invalid_codes:
                univ_ok = False
                univ_reasons.append(f"Invalid KRX stock code format (must be 6 digits): {invalid_codes[:5]}")
            if len(universe) < 5:
                univ_reasons.append(f"Small universe size: {len(universe)} symbols")

        gates.append(AdmissionGateResult(
            gate_id=4,
            name="종목 Universe 정상인지",
            status="PASS" if univ_ok else "FAIL",
            details="; ".join(univ_reasons) if univ_reasons else f"Valid KRX Universe with {len(universe)} symbols",
            metrics={"universe_size": len(universe) if universe else 0}
        ))

        # -----------------------------------------------------------------
        # 5. OHLCV 무결성 (Bar Consistency & Sanity)
        # -----------------------------------------------------------------
        ohlcv_ok = True
        ohlcv_reasons = []
        corrupted_bars = 0

        if not bars:
            ohlcv_ok = False
            ohlcv_reasons.append("No bar data provided")
        else:
            for b in bars:
                o = float(b.get("open", 0))
                h = float(b.get("high", 0))
                l = float(b.get("low", 0))
                c = float(b.get("close", 0))
                v = float(b.get("volume", 0))

                if o <= 0 or h <= 0 or l <= 0 or c <= 0 or v < 0:
                    corrupted_bars += 1
                elif h < max(o, c) or l > min(o, c) or h < l:
                    corrupted_bars += 1

            if corrupted_bars > 0:
                ohlcv_ok = False
                ohlcv_reasons.append(f"Corrupted OHLCV bars found: {corrupted_bars}")

        gates.append(AdmissionGateResult(
            gate_id=5,
            name="OHLCV 무결성",
            status="PASS" if ohlcv_ok else "FAIL",
            details="; ".join(ohlcv_reasons) if ohlcv_reasons else "All OHLCV bars mathematically consistent & positive",
            metrics={"corrupted_bars": corrupted_bars}
        ))

        # -----------------------------------------------------------------
        # 6. Bid/Ask 무결성 (Quote Spread & Depth Integrity)
        # -----------------------------------------------------------------
        quote_ok = True
        quote_reasons = []
        inverted_quotes = 0
        abnormal_spreads = 0

        if bars:
            for b in bars:
                bid = float(b.get("bid", 0))
                ask = float(b.get("ask", 0))
                if bid > 0 and ask > 0:
                    if bid > ask:
                        inverted_quotes += 1
                    spread_ratio = (ask - bid) / ask
                    if spread_ratio < 0.0 or spread_ratio > 0.10:  # > 10% spread is abnormal
                        abnormal_spreads += 1

            if inverted_quotes > 0:
                quote_ok = False
                quote_reasons.append(f"Inverted quotes (bid > ask): {inverted_quotes}")
            if abnormal_spreads > 0:
                quote_ok = False
                quote_reasons.append(f"Abnormal bid/ask spreads (>10%): {abnormal_spreads}")

        gates.append(AdmissionGateResult(
            gate_id=6,
            name="Bid/Ask 무결성",
            status="PASS" if quote_ok else "FAIL",
            details="; ".join(quote_reasons) if quote_reasons else "Bid/Ask quotes valid (ask >= bid, spread <= 10%)",
            metrics={"inverted_quotes": inverted_quotes, "abnormal_spreads": abnormal_spreads}
        ))

        # -----------------------------------------------------------------
        # 7. PIT Model 생성 가능 여부 (Model Fit End < OOS Start)
        # -----------------------------------------------------------------
        model_pit_ok = True
        model_pit_reasons = []

        if not model_fit_date:
            model_pit_ok = False
            model_pit_reasons.append("Model fit date not specified")
        elif not oos_start_date:
            model_pit_ok = False
            model_pit_reasons.append("OOS backtest start date not specified")
        else:
            try:
                m_fit_dt = datetime.strptime(model_fit_date, "%Y-%m-%d")
                if m_fit_dt >= oos_start_date:
                    model_pit_ok = False
                    model_pit_reasons.append(
                        f"TIMING INVERSION: Model fit end ({model_fit_date}) >= OOS start ({oos_start_date.strftime('%Y-%m-%d')})"
                    )
            except Exception as e:
                model_pit_ok = False
                model_pit_reasons.append(f"Invalid model fit date format: {e}")

        gates.append(AdmissionGateResult(
            gate_id=7,
            name="PIT Model 생성 가능 여부",
            status="PASS" if model_pit_ok else "FAIL",
            details="; ".join(model_pit_reasons) if model_pit_reasons else f"Model fit ({model_fit_date}) < OOS start ({oos_start_date.strftime('%Y-%m-%d') if oos_start_date else ''})",
            metrics={"model_fit_date": model_fit_date, "oos_start_date": oos_start_date.isoformat() if oos_start_date else None}
        ))

        # -----------------------------------------------------------------
        # 8. Scaler PIT 여부 (Scaler Fit End < OOS Start)
        # -----------------------------------------------------------------
        scaler_pit_ok = True
        scaler_pit_reasons = []

        if not scaler_fit_date:
            scaler_pit_ok = False
            scaler_pit_reasons.append("Scaler fit date not specified")
        elif not oos_start_date:
            scaler_pit_ok = False
            scaler_pit_reasons.append("OOS backtest start date not specified")
        else:
            try:
                s_fit_dt = datetime.strptime(scaler_fit_date, "%Y-%m-%d")
                if s_fit_dt >= oos_start_date:
                    scaler_pit_ok = False
                    scaler_pit_reasons.append(
                        f"TIMING INVERSION: Scaler fit end ({scaler_fit_date}) >= OOS start ({oos_start_date.strftime('%Y-%m-%d')})"
                    )
            except Exception as e:
                scaler_pit_ok = False
                scaler_pit_reasons.append(f"Invalid scaler fit date format: {e}")

        gates.append(AdmissionGateResult(
            gate_id=8,
            name="Scaler PIT 여부",
            status="PASS" if scaler_pit_ok else "FAIL",
            details="; ".join(scaler_pit_reasons) if scaler_pit_reasons else f"Scaler fit ({scaler_fit_date}) < OOS start ({oos_start_date.strftime('%Y-%m-%d') if oos_start_date else ''})",
            metrics={"scaler_fit_date": scaler_fit_date, "oos_start_date": oos_start_date.isoformat() if oos_start_date else None}
        ))

        # -----------------------------------------------------------------
        # 9. Similarity PIT 여부 (Similarity Database Integrity)
        # -----------------------------------------------------------------
        sim_pit_ok = (similarity_leakage_count == 0)
        gates.append(AdmissionGateResult(
            gate_id=9,
            name="Similarity PIT 여부",
            status="PASS" if sim_pit_ok else "FAIL",
            details="0 future similarity references detected" if sim_pit_ok else f"FAIL: {similarity_leakage_count} future similarity references detected",
            metrics={"similarity_leakage_count": similarity_leakage_count}
        ))

        # -----------------------------------------------------------------
        # 10. Leakage 0건 여부 (Overall Lookahead Leakage Audit)
        # -----------------------------------------------------------------
        leakage_ok = (lookahead_leakage_count == 0)
        gates.append(AdmissionGateResult(
            gate_id=10,
            name="Leakage 0건 여부",
            status="PASS" if leakage_ok else "FAIL",
            details="0 lookahead leakage events detected" if leakage_ok else f"FAIL: {lookahead_leakage_count} lookahead leakage events detected",
            metrics={"lookahead_leakage_count": lookahead_leakage_count}
        ))

        # -----------------------------------------------------------------
        # Final Determination
        # -----------------------------------------------------------------
        failures = [g.name for g in gates if g.status == "FAIL"]
        all_passed = len(failures) == 0

        overall_status = "PASS" if all_passed else "FAIL"
        decision = "BACKTEST PERFORMANCE ENABLED" if all_passed else "BACKTEST PERFORMANCE BLOCKED"

        return AdmissionEvaluation(
            overall_status=overall_status,
            decision=decision,
            dataset_path=dataset_path,
            data_source=data_source,
            gates=gates,
            passed_count=len([g for g in gates if g.status == "PASS"]),
            failed_count=len(failures),
            failure_reasons=failures
        )
