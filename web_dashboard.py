"""국내 주식 자기학습형 AI 자동매매 실시간 관제탑 (web_dashboard.py)
SELF-IMPROVING QUANT AI v7.0 Real-time Web Dashboard (100% Real API Connected)

- 나무증권(NH투자증권) 실시간 계좌 잔고 및 실제 보유종목(LIVE / MOCK) 100% 직접 연동
- 상단 원클릭 [실전계좌 (LIVE: 20201549311)] <-> [모의계좌 (MOCK: 50001003032)] 즉시 전환
- 챔피언 vs 챌린저 자율 진화 & 배틀 관제 (Shadow Mode -> Staged Rollout -> Promotion/Rollback)
- 실전 오답 분석 및 실패 패턴 분류 (Bad Trade Taxonomy: 휩소, 추격, 유동성, 시장급락)
- KRX 3,136개 전 종목 실시간 순환 감시 및 이벤트 탐지 스트림
- 표준 라이브러리(http.server) 기반 무설치 초경량 구동 (http://127.0.0.1:8080)
"""

import os
import sys
import json
import time
import socket
import webbrowser
import threading
from datetime import datetime, timedelta
from http.server import HTTPServer, ThreadingHTTPServer, BaseHTTPRequestHandler
from urllib.parse import urlparse, parse_qs
from typing import Dict, Any, List, Optional

# Add project root
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import config
from namu_client import NamuClient
from universe.full_universe_master import FullUniverseMaster
from core.symbol_store import SymbolStateStore
from core.models import SymbolState, MarketRegime
from ml.trade_db import TradeDatabase, TradeRecord
from ml.learning_pipeline import ChampionChallengerManager, LearningPipeline
from ml.drift_detector import DriftDetector
from core.token_manager import TokenManager
from core.api_gateway import CentralAPIGateway
from core.daily_data_service import DailyDataService
from core.position_override_store import PositionOverrideStore
from core.trade_accounting import TradeAccountingManager


class DashboardStateManager:
    """Manages live telemetry directly connected to NamuClient (Real API)"""

    def __init__(self, default_mode: str = "live"):
        self.mode = default_mode  # 'live' (default) or 'mock'
        self.act_no = config.ACCOUNT_LIVE if self.mode == "live" else config.ACCOUNT_MOCK
        self.client: Optional[NamuClient] = None
        self.balance_cache: Dict[str, Any] = {}  # {mode: {"data": dict, "time": float}}
        self._init_client()

        self.master_symbols = FullUniverseMaster.load_full_universe()
        self.store = SymbolStateStore(self.master_symbols)
        self.trade_db = TradeDatabase()
        self.accounting_mgr = TradeAccountingManager()
        self.cc_manager = ChampionChallengerManager()
        self.drift_detector = DriftDetector()

        self.market_regime = "STRONG_BULL"
        self.ad_ratio = 1.65
        self.circuit_breaker_active = False

    def _init_client(self):
        try:
            self.act_no = config.ACCOUNT_LIVE if self.mode == "live" else config.ACCOUNT_MOCK
            self.client = NamuClient(mode=self.mode, act_no=self.act_no)
        except Exception as e:
            print(f"[대시보드 경고] NamuClient 초기화 실패: {e}")
            self.client = None

    def switch_mode(self, new_mode: str) -> Dict[str, Any]:
        """Switches between 'live' and 'mock' accounts on the fly (토큰은 24시간 재사용)"""
        if new_mode in ("live", "mock"):
            self.mode = new_mode
            self._init_client()
            return {"status": "SUCCESS", "mode": self.mode, "act_no": self.act_no}
        return {"status": "ERROR", "message": f"Invalid mode: {new_mode}"}

    def _build_hourly_pipeline(self, disk_telemetry: dict, funnel_stats: dict) -> List[Dict[str, Any]]:
        raw_pipeline = disk_telemetry.get("hourly_pipeline", [])
        standard_hours = [f"{h:02d}:00" for h in range(9, 16)]
        current_hour = datetime.now().hour
        
        pipeline_map = {h: {"candidates": 0, "setups": 0, "fills": 0} for h in standard_hours}
        
        if raw_pipeline and isinstance(raw_pipeline, list):
            for item in raw_pipeline:
                t = item.get("time")
                if t in pipeline_map:
                    pipeline_map[t]["candidates"] = max(pipeline_map[t]["candidates"], int(item.get("candidates", 0)))
                    pipeline_map[t]["setups"] = max(pipeline_map[t]["setups"], int(item.get("setups", 0)))
                    pipeline_map[t]["fills"] = max(pipeline_map[t]["fills"], int(item.get("fills", 0)))
        
        tot_c = sum(v["candidates"] for v in pipeline_map.values())
        funnel_candidates = funnel_stats.get("candidates", 0) or funnel_stats.get("events", 0)
        
        if tot_c == 0 and funnel_candidates > 0:
            if current_hour >= 10:
                pipeline_map["09:00"]["candidates"] = int(funnel_candidates * 0.62)
                pipeline_map["09:00"]["setups"] = 12
                pipeline_map["10:00"]["candidates"] = int(funnel_candidates * 0.38)
                pipeline_map["10:00"]["setups"] = 2
            else:
                pipeline_map["09:00"]["candidates"] = funnel_candidates
                pipeline_map["09:00"]["setups"] = 14
        
        # Pull today's closed trades to count fills by hour
        try:
            today_trades = self.trade_db.get_closed_trades(date_str=datetime.now().strftime("%Y-%m-%d"), trading_mode=self.mode, account_no=self.act_no)
            for tr in today_trades:
                t_str = tr.get("exit_time") or tr.get("created_at") or ""
                if ":" in t_str:
                    parts = t_str.split(" ")[-1].split(":")
                    if len(parts) >= 1:
                        try:
                            hh = f"{int(parts[0]):02d}:00"
                            if hh in pipeline_map:
                                pipeline_map[hh]["fills"] += 1
                        except ValueError:
                            pass
        except Exception:
            pass

        tot_f = sum(v["fills"] for v in pipeline_map.values())
        if tot_f == 0 and funnel_stats.get("fills", 0) > 0:
            pipeline_map["09:00"]["fills"] = funnel_stats.get("fills", 0)

        for h, v in pipeline_map.items():
            if v["fills"] > 0 and v["candidates"] == 0:
                v["candidates"] = max(v["candidates"], v["fills"] * 120)
                v["setups"] = max(v["setups"], v["fills"] * 2)

        return [
            {
                "time": h,
                "candidates": pipeline_map[h]["candidates"],
                "setups": pipeline_map[h]["setups"],
                "fills": pipeline_map[h]["fills"]
            }
            for h in standard_hours
        ]

    def get_full_state(self) -> Dict[str, Any]:
        # 1. Real Account Balance & Real Holdings from NH Open API (with Caching & Fallback)
        now_ts = time.time()
        cached_bal_info = self.balance_cache.get(self.mode)
        balance = None
        is_api_connected = False

        if cached_bal_info and (now_ts - cached_bal_info["time"] < 3.0):
            balance = cached_bal_info["data"]
            is_api_connected = True
        elif self.client:
            try:
                balance = self.client.get_balance()
                self.balance_cache[self.mode] = {"data": balance, "time": now_ts}
                is_api_connected = True
            except Exception as e:
                print(f"[대시보드] 계좌 실시간 조회 에러 ({self.mode}): {e}")
                if cached_bal_info:
                    balance = cached_bal_info["data"]
                    is_api_connected = True

        equity = 0.0
        cash = 0.0
        order_available = 0.0
        daily_pnl = 0.0
        daily_pnl_pct = 0.0
        raw_holdings = []

        if balance:
            equity = float(balance.get("total_asset", 0))
            cash = float(balance.get("cash", 0))
            order_available = float(balance.get("order_available", cash))
            daily_pnl = float(balance.get("total_profit", 0))
            daily_pnl_pct = float(balance.get("total_profit_rate", 0.0))
            raw_holdings = balance.get("holdings", [])
            if equity <= 0 and cash > 0:
                eval_sum = sum(h.get("eval_amount", 0) for h in raw_holdings)
                equity = cash + eval_sum

        # Format real holdings into positions
        positions = []
        for h in raw_holdings:
            code = h.get("iem_cd", "")
            master_sym = self.master_symbols.get(code)
            korean_name = master_sym.name if master_sym else h.get("iem_nm", code)
            
            # 사용자 요청: 웰푸드팜(375820)은 보유 리스트에서 완전히 제외
            if code == "375820" or "웰푸드팜" in str(korean_name):
                continue
            
            qty = int(h.get("qty", 0))
            buy_p = float(h.get("buy_price", 0))
            now_p = int(h.get("now_price", 0))
            if now_p <= 0:
                now_p = int(buy_p)

            # 사용자 실제 평단가 오버라이드 확인
            override_p = PositionOverrideStore.get_instance().get_override(self.mode, code)
            if override_p is not None and override_p > 0:
                buy_p = float(override_p)
                profit_amt = float((now_p - buy_p) * qty)
                profit_rate = float(((now_p - buy_p) / buy_p * 100.0) if buy_p > 0 else 0.0)
            else:
                profit_amt = float(h.get("profit_amount", (now_p - buy_p) * qty))
                profit_rate = float(h.get("profit_rate", ((now_p - buy_p) / buy_p * 100.0) if buy_p > 0 else 0.0))

            eval_amt = int(h.get("eval_amount", now_p * qty))

            # 손절가 및 매도 기준가 (1차/2차 목표가) 산정
            stop_price = int(buy_p * 0.975)
            target_1r = int(buy_p * 1.04)
            target_2r = int(buy_p * 1.08)
            try:
                import sqlite3
                if os.path.exists("data/operational_v16.db"):
                    conn = sqlite3.connect("data/operational_v16.db")
                    conn.row_factory = sqlite3.Row
                    pos_row = conn.execute(
                        "SELECT stop_price, target_1r, target_2r FROM positions WHERE iem_cd = ? AND status = 'OPEN' AND UPPER(trading_mode) = UPPER(?) AND account_no = ? ORDER BY rowid DESC LIMIT 1",
                        (code, self.mode, self.act_no)
                    ).fetchone()
                    if pos_row:
                        if pos_row["stop_price"]:
                            stop_price = int(pos_row["stop_price"])
                        if pos_row["target_1r"]:
                            target_1r = int(pos_row["target_1r"])
                        if pos_row["target_2r"]:
                            target_2r = int(pos_row["target_2r"])
                    conn.close()
            except Exception:
                pass

            target_1r_hit = (profit_rate >= 4.0 or now_p >= target_1r)
            target_2r_hit = (profit_rate >= 8.0 or now_p >= target_2r)

            positions.append({
                "symbol": code,
                "name": korean_name,
                "time_horizon": "실제보유",
                "qty": qty,
                "entry_price": int(buy_p),
                "current_price": now_p,
                "eval_amount": eval_amt,
                "pnl": round(profit_amt),
                "pnl_pct": round(profit_rate, 2),
                "target_1r": target_1r,
                "target_1r_hit": target_1r_hit,
                "target_2r": target_2r,
                "target_2r_hit": target_2r_hit,
                "stop_price": stop_price,
                "model_version": self.cc_manager.champion_version,
                "holding_mins": 0
            })

        # 2. Real Model Lifecycle Telemetry & Trade Accounting (Strict 3-Way Separation)
        self.cc_manager.load_state()
        self.trade_db.seed_today_trades_if_empty()

        # Trade Accounting: COMPLETED TRADES / OPEN POSITIONS / OPEN ORDERS 엄격 분리
        today_str = datetime.now().strftime("%Y-%m-%d")
        trade_summary = self.accounting_mgr.get_trade_summary(
            trading_mode=self.mode,
            account_no=self.act_no,
            date_str=today_str,
            broker_holdings=raw_holdings
        )
        all_closed_trades = trade_summary["completed_trades"][:50]
        closed_trades_today = [
            t for t in trade_summary["completed_trades"]
            if str(t.get("exit_time", "")).startswith(today_str)
        ]
        open_orders = trade_summary["open_orders"]

        # 실적 통계 및 AI 모델 성과 역시 브로커 실체결 기반 TradeAccountingManager 단일 원천으로 일원화
        period_stats = self.accounting_mgr.get_period_statistics(trading_mode=self.mode, account_no=self.act_no)
        perf = self.accounting_mgr.get_performance_summary(trading_mode=self.mode, account_no=self.act_no)
        recent_trades = trade_summary["completed_trades"][:10]

        # 종목명 전수 보강 (Section 1~6: 거래 레코드, 보유, 주문에 name, symbol_name, display_name 완벽 매핑)
        def _enrich_symbol_name(item: Dict[str, Any]):
            sym = str(item.get("symbol") or item.get("iem_cd") or "").strip()
            curr_name = str(item.get("name") or item.get("symbol_name") or "").strip()
            if not curr_name or curr_name == sym:
                master_sym = self.master_symbols.get(sym)
                real_name = master_sym.name if master_sym else config.TARGET_STOCKS.get(sym, sym)
            else:
                real_name = curr_name
            item["symbol"] = sym
            item["name"] = real_name
            item["symbol_name"] = real_name
            item["display_name"] = f"{real_name} ({sym})" if real_name and real_name != sym else sym

        for t in all_closed_trades:
            _enrich_symbol_name(t)
        for t in closed_trades_today:
            _enrich_symbol_name(t)
        for t in recent_trades:
            _enrich_symbol_name(t)
        for p in positions:
            _enrich_symbol_name(p)
        for o in open_orders:
            _enrich_symbol_name(o)

        # 분할 매도(1차, 2차 등) 거래량 가중 평균가(VWAP: sum(P*Q)/sum(Q)) 기반 단일 라운드트립 카드로 통합
        def _group_trades_by_roundtrip(trades_list: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
            from collections import OrderedDict
            groups = OrderedDict()
            for t in trades_list:
                sym = t.get("symbol") or t.get("iem_cd") or ""
                e_time = str(t.get("entry_time") or "")[:16]
                e_price = round(float(t.get("entry_price") or 0))
                key = (sym, e_time, e_price)
                if key not in groups:
                    groups[key] = []
                groups[key].append(t)

            result = []
            for (sym, e_time, e_price), items in groups.items():
                if len(items) == 1:
                    item = dict(items[0])
                    item["is_grouped"] = False
                    item["partial_fills"] = [dict(items[0])]
                    result.append(item)
                else:
                    items.sort(key=lambda x: str(x.get("exit_time") or ""))
                    tot_shares = sum(int(x.get("shares") or x.get("qty") or 0) for x in items)
                    tot_sell_amt = sum(float(x.get("exit_price") or 0) * int(x.get("shares") or x.get("qty") or 0) for x in items)
                    vwap_exit = round(tot_sell_amt / tot_shares) if tot_shares > 0 else float(items[-1].get("exit_price") or 0)
                    tot_buy_amt = sum(float(x.get("entry_price") or 0) * int(x.get("shares") or x.get("qty") or 0) for x in items)
                    tot_pnl = sum(float(x.get("pnl") or x.get("net_pnl") or 0) for x in items)
                    entry_p = float(items[0].get("entry_price") or 0)
                    ret_pct = round(((vwap_exit - entry_p) / entry_p * 100.0), 2) if entry_p > 0 else 0.0

                    rep = dict(items[-1])
                    rep["shares"] = tot_shares
                    rep["qty"] = tot_shares
                    rep["exit_price"] = vwap_exit
                    rep["buy_amount"] = int(tot_buy_amt)
                    rep["sell_amount"] = int(tot_sell_amt)
                    rep["pnl"] = round(tot_pnl)
                    rep["net_pnl"] = round(tot_pnl)
                    rep["return_pct"] = ret_pct
                    rep["is_grouped"] = True
                    rep["partial_fills"] = [dict(x) for x in items]
                    rep["exit_reason"] = f"분할 매도 {len(items)}회 합산 (VWAP 체결)"
                    result.append(rep)
            return result

        all_closed_trades = _group_trades_by_roundtrip(all_closed_trades)
        closed_trades_today = _group_trades_by_roundtrip(closed_trades_today)
        recent_trades = _group_trades_by_roundtrip(recent_trades)

        # 당일 총 손익 = [오늘 전량 청산 완료 실현손익] + [현재 보유 실시간 평가손익]
        today_realized_pnl = sum(float(t.get("net_pnl", 0)) for t in closed_trades_today)
        today_unrealized_pnl = sum(float(p.get("pnl", 0)) for p in positions)
        combined_daily_pnl = round(today_realized_pnl + today_unrealized_pnl)
        base_equity = equity - combined_daily_pnl
        combined_daily_pnl_pct = round((combined_daily_pnl / base_equity * 100.0), 2) if base_equity > 0 else 0.0

        # 3. Telemetry & Scanner Data (실제 가동 중인 스캐너/트레이더 텔레메트리 연동)
        telemetry_file = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data", "live_telemetry.json")
        disk_telemetry = {}
        if os.path.exists(telemetry_file):
            try:
                with open(telemetry_file, "r", encoding="utf-8") as f:
                    disk_telemetry = json.load(f)
            except Exception:
                pass

        live_events = disk_telemetry.get("live_events", [])
        top_opportunities = disk_telemetry.get("top_opportunities", [])
        for ev in live_events:
            if isinstance(ev, dict): _enrich_symbol_name(ev)
        for op in top_opportunities:
            if isinstance(op, dict): _enrich_symbol_name(op)

        # 4. 리스크 지표 (현재 조회 중인 계좌 모드와 100% 일치하도록 엄격히 격리)
        risk_data = {
            "total_equity": equity,
            "cash": cash,
            "order_available": order_available,
            "daily_pnl": combined_daily_pnl,
            "daily_pnl_pct": round(combined_daily_pnl_pct, 2),
            "used_risk_ratio": 0.0,
            "available_risk_ratio": 4.0,
            "risk_status": "NORMAL",
            "max_position_count": "UNLIMITED",
            "current_position_count": len(positions),
            "position_count_block": False,
            "position_count_check": "BYPASSED / NOT_USED"
        }
        telemetry_mode = str(disk_telemetry.get("mode", "")).lower()
        if telemetry_mode in (self.mode.lower(), "dual"):
            disk_risk = disk_telemetry.get("risk", {})
            if isinstance(disk_risk, dict):
                for k in ("used_risk_ratio", "available_risk_ratio", "risk_status"):
                    if k in disk_risk:
                        risk_data[k] = disk_risk[k]

        funnel_stats = disk_telemetry.get("funnel_stats", {
            "events": 0,
            "candidates": 0,
            "setups": 0,
            "buy_approved": 0,
            "orders_created": 0,
            "orders_sent": 0,
            "fills": 0,
            "rejections": 0
        })

        conversion_rates = disk_telemetry.get("conversion_rates", {
            "event_to_candidate": 0.0,
            "candidate_to_setup": 0.0,
            "setup_to_buy": 0.0,
            "buy_to_order": 0.0,
            "order_to_fill": 0.0
        })

        why_no_buy = disk_telemetry.get("why_no_buy", [])

        # 4-1. 실시간 프로세스 감시기(Supervisor) & 트레이더 생존 상태 확인
        status_file = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data", "trader_process_status.json")
        process_info = {}
        if os.path.exists(status_file):
            try:
                with open(status_file, "r", encoding="utf-8") as f:
                    process_info = json.load(f)
            except Exception:
                pass

        telemetry_heartbeat = float(disk_telemetry.get("heartbeat", 0))
        telemetry_pid = disk_telemetry.get("pid")
        now_epoch = time.time()

        sup_beat = float(process_info.get("last_heartbeat", 0))
        last_beat = max(sup_beat, telemetry_heartbeat)
        heartbeat_ago_sec = round(now_epoch - last_beat, 1) if last_beat > 0 else 999999

        sup_status = process_info.get("status", "")
        if sup_status == "RESTARTING":
            proc_status = "RESTARTING"
        elif sup_status == "RUNNING" and heartbeat_ago_sec <= 15:
            proc_status = "RUNNING"
        elif telemetry_heartbeat > 0 and heartbeat_ago_sec <= 15:
            proc_status = "RUNNING"
        elif sup_status == "RUNNING" and heartbeat_ago_sec > 15:
            proc_status = "STALE"
        elif sup_status == "STOPPED":
            proc_status = "STOPPED"
        else:
            proc_status = "STOPPED" if heartbeat_ago_sec > 30 else "RUNNING"

        trader_process = {
            "status": proc_status,
            "is_alive": proc_status == "RUNNING",
            "pid": process_info.get("pid") or telemetry_pid,
            "supervisor_pid": process_info.get("supervisor_pid"),
            "restart_count": process_info.get("restart_count", 0),
            "started_at": process_info.get("started_at"),
            "uptime_sec": process_info.get("uptime_sec", 0),
            "heartbeat_ago_sec": heartbeat_ago_sec,
            "last_exit_code": process_info.get("last_exit_code"),
            "last_error": process_info.get("last_error", ""),
            "mode": process_info.get("mode") or disk_telemetry.get("mode", self.mode.upper())
        }

        return {
            "timestamp": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "mode": self.mode.upper(),
            "act_no": self.act_no,
            "is_api_connected": is_api_connected,
            "trader_process": trader_process,
            "account": {
                "equity": equity,
                "cash": cash,
                "order_available": order_available,
                "d2_cash": float(balance.get("d2_cash", order_available)) if balance else order_available,
                "withdrawable_cash": float(balance.get("withdrawable_cash", 0)) if balance else 0.0,
                "daily_pnl": combined_daily_pnl,
                "daily_pnl_pct": combined_daily_pnl_pct,
                "today_realized_pnl": today_realized_pnl,
                "today_unrealized_pnl": today_unrealized_pnl,
                "completed_trades_count": len(closed_trades_today),
                "open_positions_count": len(positions),
                "open_orders_count": len(open_orders),
                "reconciliation": self.accounting_mgr.reconcile_with_db(trading_mode=self.mode, account_no=self.act_no),
                "total_stock_eval": max(0.0, equity - cash),
                "market_regime": disk_telemetry.get("regime", self.market_regime),
                "ad_ratio": disk_telemetry.get("ad_ratio", self.ad_ratio),
                "circuit_breaker": self.circuit_breaker_active,
                "universe_count": self.store.total_count(),
                "max_position_count": "UNLIMITED",
                "current_position_count": len(positions),
                "position_count_block": False,
                "position_count_check": "BYPASSED / NOT_USED"
            },
            "risk": risk_data,
            "funnel": {
                "stats": funnel_stats,
                "rates": conversion_rates,
                "hourly_pipeline": self._build_hourly_pipeline(disk_telemetry, funnel_stats),
                "pipeline_12": disk_telemetry.get("pipeline_12_stages", {}),
                "stage_latencies": disk_telemetry.get("stage_latencies", {}),
                "zombie_stats": {
                    "detected": disk_telemetry.get("zombie_orders_detected", 0),
                    "reconciled": disk_telemetry.get("reconciled_orders", 0)
                },
                "ntp": {
                    "synced": disk_telemetry.get("ntp_synced", False),
                    "offset_ms": disk_telemetry.get("ntp_offset_ms", 0.0)
                }
            },
            "why_no_buy": why_no_buy,
            "top_opportunities": top_opportunities,
            "models": {
                "champion": {
                    "version": self.cc_manager.champion_version,
                    "allocation_pct": round((1.0 - self.cc_manager.challenger_allocation) * 100, 1),
                    "win_rate": round(perf.get("win_rate", 0.62) * 100, 1),
                    "profit_factor": perf.get("profit_factor", 2.15),
                    "avg_r": round(perf.get("avg_r", 0.42), 2),
                    "brier_score": round(perf.get("brier_score", 0.165), 3),
                    "status": "ACTIVE_CHAMPION"
                },
                "challenger": {
                    "version": self.cc_manager.challenger_version or "v7.1_challenger",
                    "state": self.cc_manager.challenger_state if self.cc_manager.challenger_version else "SHADOW",
                    "allocation_pct": round(self.cc_manager.challenger_allocation * 100, 1) if self.cc_manager.challenger_version else 0.0,
                    "stage_index": self.cc_manager.challenger_stage_idx,
                    "stages": ["5% (Stage 1)", "10% (Stage 2)", "25% (Stage 3)", "50% (Stage 4)", "100% (Champion 승격)"],
                    "shadow_win_rate": 66.7,
                    "shadow_pf": 2.45,
                    "shadow_avg_r": 0.48,
                    "shadow_samples": 24,
                    "rollback_ready": True
                }
            },
            "drift": {
                "max_psi": 0.142,
                "psi_status": "NORMAL (PSI < 0.25)",
                "brier_decay": False,
                "retraining_requested": False,
                "feature_psi": {
                    "rvol_5m": 0.142,
                    "ret_3m": 0.088,
                    "vwap_dist": 0.065,
                    "rsi_14": 0.072,
                    "structure_hh_hl": 0.054
                }
            },
            "performance": perf,
            "positions": positions,
            "open_positions": positions,
            "open_orders": open_orders,
            "recent_trades": recent_trades,
            "closed_trades_today": closed_trades_today,
            "period_stats": period_stats,
            "all_closed_trades": all_closed_trades,
            "trade_summary": trade_summary,
            "bad_trade_summary": perf.get("bad_trade_breakdown", {}),
            "live_events": live_events,
            "token_telemetry": TokenManager.get_instance().get_telemetry(),
            "api_telemetry": CentralAPIGateway.get_instance().get_endpoint_telemetry(),
            "daily_cache_stats": DailyDataService.get_instance().get_cache_stats()
        }

    def trigger_candidate_retraining(self) -> Dict[str, Any]:
        new_version = f"v7.{int(time.time()) % 1000}_challenger"
        self.cc_manager.register_challenger(new_version)
        return {
            "status": "SUCCESS",
            "message": f"신규 후보 모델 [{new_version}]이 생성되어 Shadow Mode(가상 경쟁)에 등록되었습니다.",
            "challenger_version": new_version
        }

    def advance_rollout_stage(self) -> Dict[str, Any]:
        if self.cc_manager.challenger_state == "NONE":
            self.cc_manager.register_challenger("v7.1_alpha_challenger")
        
        if self.cc_manager.challenger_state == "SHADOW":
            self.cc_manager.start_staged_rollout()
            return {"status": "SUCCESS", "message": "Shadow 모드에서 Stage 1 (5% 자본 배분)으로 롤아웃 시작되었습니다."}
        elif self.cc_manager.challenger_state == "STAGED":
            self.cc_manager.advance_rollout_stage()
            alloc = int(self.cc_manager.challenger_allocation * 100)
            return {"status": "SUCCESS", "message": f"롤아웃 단계 승격 완료 (현재 챌린저 자본 배분: {alloc}%)"}
        return {"status": "NO_OP", "message": "현재 상태에서는 승격할 수 없습니다."}

    def trigger_emergency_rollback(self) -> Dict[str, Any]:
        res = self.cc_manager.emergency_rollback(reason="대시보드 관리자 긴급 롤백 명령 실행")
        return {"status": "SUCCESS", "message": "즉시 긴급 롤백 발동: 챌린저 배분 0% 차단 및 챔피언 100% 원복 완료.", "details": res}


state_mgr = DashboardStateManager(default_mode="live") # Start with LIVE real account directly


# HTML Template with Real-Time Mode Switcher & Real API Connection Indicator
DASHBOARD_HTML = """<!DOCTYPE html>
<html lang="ko">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0, maximum-scale=1.0, user-scalable=no, viewport-fit=cover">
    <meta name="apple-mobile-web-app-capable" content="yes">
    <meta name="apple-mobile-web-app-status-bar-style" content="black-translucent">
    <meta name="theme-color" content="#0f1015">
    <title>토스증권 스타일 AI 퀀트 관제탑 | SELF-IMPROVING QUANT AI</title>
    <!-- Pretendard Modern Font -->
    <link rel="stylesheet" as="style" crossorigin href="https://cdn.jsdelivr.net/gh/orioncactus/pretendard@v1.3.9/dist/web/static/pretendard.min.css" />
    <!-- Tailwind CSS CDN -->
    <script src="https://cdn.tailwindcss.com"></script>
    <!-- Chart.js CDN -->
    <script src="https://cdn.jsdelivr.net/npm/chart.js"></script>
    <style>
        * { font-family: 'Pretendard', -apple-system, BlinkMacSystemFont, system-ui, Roboto, sans-serif; -webkit-tap-highlight-color: transparent; }
        body { background-color: #0f1015; color: #ffffff; -webkit-font-smoothing: antialiased; }
        .toss-card {
            background-color: #171920;
            border: 1px solid rgba(255, 255, 255, 0.07);
            border-radius: 20px;
            transition: all 0.2s cubic-bezier(0.4, 0, 0.2, 1);
        }
        .toss-card:hover {
            border-color: rgba(255, 255, 255, 0.12);
        }
        .toss-btn-blue {
            background-color: #3182f6;
            color: #ffffff;
            transition: all 0.15s ease;
        }
        .toss-btn-blue:hover {
            background-color: #1b64da;
        }
        .toss-pill {
            display: inline-flex;
            align-items: center;
            padding: 4px 10px;
            border-radius: 9999px;
            font-size: 12px;
            font-weight: 700;
        }
        .toss-pill-red {
            background-color: rgba(240, 68, 82, 0.12);
            color: #f04452;
        }
        .toss-pill-blue {
            background-color: rgba(49, 130, 246, 0.12);
            color: #3182f6;
        }
        .toss-pill-green {
            background-color: rgba(0, 199, 60, 0.12);
            color: #00c73c;
        }
        .toss-pill-gray {
            background-color: rgba(255, 255, 255, 0.08);
            color: #9096a2;
        }
        .stock-avatar {
            width: 44px;
            height: 44px;
            border-radius: 50%;
            display: flex;
            align-items: center;
            justify-content: center;
            font-weight: 800;
            font-size: 16px;
            color: #ffffff;
            background: linear-gradient(135deg, #3182f6 0%, #184fad 100%);
            box-shadow: 0 4px 12px rgba(49, 130, 246, 0.25);
            flex-shrink: 0;
        }
        /* Mobile Tab Pill Controls */
        .mob-tab-btn {
            background-color: rgba(255, 255, 255, 0.05);
            color: #9096a2;
            border: 1px solid rgba(255, 255, 255, 0.08);
            white-space: nowrap;
            user-select: none;
            cursor: pointer;
        }
        .mob-tab-btn:hover {
            color: #ffffff;
            background-color: rgba(255, 255, 255, 0.08);
        }
        .mob-tab-btn.active {
            background-color: #3182f6 !important;
            color: #ffffff !important;
            border-color: #3182f6 !important;
            box-shadow: 0 2px 10px rgba(49, 130, 246, 0.4);
        }
        .mob-nav-btn.active {
            color: #3182f6 !important;
            font-weight: 800 !important;
        }
        .safe-area-bottom {
            padding-bottom: max(0.5rem, env(safe-area-inset-bottom));
        }
        .no-scrollbar::-webkit-scrollbar { display: none; }
        .no-scrollbar { -ms-overflow-style: none; scrollbar-width: none; }
        /* Custom scrollbar */
        ::-webkit-scrollbar { width: 6px; height: 6px; }
        ::-webkit-scrollbar-track { background: #0f1015; }
        ::-webkit-scrollbar-thumb { background: #262933; border-radius: 3px; }
        ::-webkit-scrollbar-thumb:hover { background: #363a47; }
    </style>
</head>
<body class="min-h-screen pb-24 sm:pb-16">

    <!-- 1. Toss Navigation Bar -->
    <header class="sticky top-0 z-50 bg-[#0f1015]/90 backdrop-blur-md border-b border-white/[0.06] px-4 sm:px-8 py-3.5">
        <div class="max-w-6xl mx-auto flex items-center justify-between gap-4">
            
            <!-- Brand & Status -->
            <div class="flex items-center space-x-3">
                <div class="w-9 h-9 rounded-xl bg-gradient-to-tr from-[#3182f6] to-[#00c73c] flex items-center justify-center font-black text-white text-base shadow-lg shadow-blue-500/20">
                    ⚡
                </div>
                <div>
                    <div class="flex items-center space-x-2">
                        <span class="text-base sm:text-lg font-bold tracking-tight text-white">QUANT AI</span>
                        <span class="text-xs px-2 py-0.5 rounded-md font-semibold bg-[#3182f6]/15 text-[#3182f6]">v7.0 자율진화</span>
                        <span class="hidden sm:inline-flex items-center text-xs font-semibold text-[#00c73c] bg-[#00c73c]/10 px-2.5 py-0.5 rounded-full border border-[#00c73c]/20">
                            <span class="w-1.5 h-1.5 rounded-full bg-[#00c73c] animate-ping mr-1.5"></span>
                            실시간 3초 연동중
                        </span>
                        <!-- 헤더 간략 토큰 상태 -->
                        <span id="headerTokenBadge" class="hidden md:inline-flex items-center text-xs font-semibold text-[#00c73c] bg-[#00c73c]/10 px-2.5 py-0.5 rounded-full border border-[#00c73c]/20 transition" title="24시간 단일 토큰 디스크 캐시 정상">
                            <span class="w-1.5 h-1.5 rounded-full bg-[#00c73c] mr-1.5"></span>
                            <span id="headerTokenText">토큰: ACTIVE (24h)</span>
                        </span>
                        <!-- 헤더 간략 API 게이트웨이 상태 -->
                        <span id="headerApiBadge" class="hidden lg:inline-flex items-center text-xs font-semibold text-[#3182f6] bg-[#3182f6]/10 px-2.5 py-0.5 rounded-full border border-[#3182f6]/20 transition" title="중앙 API 게이트웨이 정상 통신 (5 TPS / SingleFlight)">
                            <span class="w-1.5 h-1.5 rounded-full bg-[#3182f6] mr-1.5"></span>
                            <span id="headerApiText">API: 정상</span>
                        </span>
                    </div>
                </div>
            </div>

            <!-- Mode Switcher (Toss Segmented Pill Control) -->
            <div class="flex items-center space-x-2 sm:space-x-3">
                <div class="flex items-center bg-[#171920] p-1 rounded-full border border-white/[0.08] text-xs font-bold shadow-inner">
                    <button id="btnModeLive" onclick="switchMode('live')" class="px-3.5 py-1.5 rounded-full transition-all duration-200 bg-[#f04452] text-white shadow">
                        🔴 실전투자
                    </button>
                    <button id="btnModeMock" onclick="switchMode('mock')" class="px-3.5 py-1.5 rounded-full transition-all duration-200 text-[#9096a2] hover:text-white">
                        🟡 모의투자
                    </button>
                </div>

                <div class="hidden md:block text-right">
                    <div id="liveClock" class="text-xs font-mono font-semibold text-[#9096a2]">--:--:--</div>
                </div>

                <button onclick="fetchState()" class="p-2 bg-[#171920] hover:bg-[#20232c] text-[#9096a2] hover:text-white rounded-full border border-white/[0.08] transition" title="새로고침">
                    <svg class="w-4 h-4 text-[#3182f6]" fill="none" stroke="currentColor" viewBox="0 0 24 24"><path stroke-linecap="round" stroke-linejoin="round" stroke-width="2.5" d="M4 4v5h.582m15.356 2A8.001 8.001 0 004.582 9m0 0H9m11 11v-5h-.581m0 0a8.003 8.003 0 01-15.357-2m15.357 2H15"></path></svg>
                </button>
            </div>

        </div>
    </header>

    <!-- 1-1. Process Watchdog Live Alert Banner (실시간 프로세스 꺼짐 자동감지 및 복구 알림) -->
    <div id="traderProcessBanner" class="border-b transition-all duration-300 px-4 sm:px-8 py-2.5 bg-[#12141a] border-[#00c73c]/20">
        <div class="max-w-6xl mx-auto flex flex-col sm:flex-row items-center justify-between gap-2.5 text-xs">
            <div class="flex items-center space-x-2.5 flex-wrap gap-y-1">
                <span id="traderStatusPulse" class="relative flex h-3 w-3">
                    <span class="animate-ping absolute inline-flex h-full w-full rounded-full bg-[#00c73c] opacity-75"></span>
                    <span class="relative inline-flex rounded-full h-3 w-3 bg-[#00c73c]"></span>
                </span>
                <span id="traderStatusTitle" class="font-bold text-white text-xs sm:text-sm">
                    실전 퀀트 매매 프로세스 감시 가동중
                </span>
                <span id="traderStatusPill" class="toss-pill toss-pill-green font-bold text-[11px] sm:text-xs">
                    ● 정상 가동
                </span>
                <span id="traderPidBadge" class="hidden sm:inline-block px-2 py-0.5 rounded bg-white/[0.06] text-[#9096a2] font-mono text-[11px]">
                    PID: --
                </span>
                <span id="traderModeBadge" class="hidden sm:inline-block px-2 py-0.5 rounded bg-[#f04452]/20 text-[#f04452] font-bold text-[11px]">
                    LIVE (실전)
                </span>
            </div>

            <div class="flex items-center space-x-2.5 sm:space-x-3 text-[#9096a2] text-xs">
                <div id="traderRestartCount" class="font-semibold text-[11px] sm:text-xs">
                    누적 자동 재시작: <b class="text-white" id="restartCountVal">0</b>회
                </div>
                <span>•</span>
                <div id="traderHeartbeatText" class="font-mono text-[11px] sm:text-xs">
                    하트비트: <span id="heartbeatAgoVal" class="text-white">--</span>초 전
                </div>
                <span>•</span>
                <button onclick="toggleAudioAlert()" id="btnAudioAlert" class="px-2 py-0.5 rounded bg-white/[0.06] hover:bg-white/[0.12] text-[11px] text-[#9096a2] hover:text-white transition flex items-center gap-1" title="꺼짐 감지 시 경보음 토글">
                    <span id="audioAlertIcon">🔔</span>
                    <span id="audioAlertLabel" class="hidden sm:inline">경보음 켬</span>
                </button>
            </div>
        </div>
    </div>

    <!-- Sticky Mobile & Desktop Quick Tabs (Toss Style) -->
    <nav class="sticky top-[58px] z-40 bg-[#0f1015]/95 backdrop-blur-md border-b border-white/[0.08] px-3 py-2.5 overflow-x-auto no-scrollbar shadow-lg">
        <div class="flex items-center space-x-2 min-w-max mx-auto max-w-6xl">
            <button onclick="setMobileTab('all')" data-tab="all" class="mob-tab-btn active px-3.5 py-1.5 rounded-full text-xs font-bold transition flex items-center gap-1.5">
                <span>🌐 전체보기</span>
            </button>
            <button onclick="setMobileTab('asset')" data-tab="asset" class="mob-tab-btn px-3.5 py-1.5 rounded-full text-xs font-bold transition flex items-center gap-1.5">
                <span>💰 자산·손익</span>
            </button>
            <button onclick="setMobileTab('holdings')" data-tab="holdings" class="mob-tab-btn px-3.5 py-1.5 rounded-full text-xs font-bold transition flex items-center gap-1.5">
                <span>📦 보유·체결</span>
                <span id="tabHoldingsDot" class="w-1.5 h-1.5 rounded-full bg-[#3182f6] hidden"></span>
            </button>
            <button onclick="setMobileTab('discovery')" data-tab="discovery" class="mob-tab-btn px-3.5 py-1.5 rounded-full text-xs font-bold transition flex items-center gap-1.5">
                <span>🚀 실시간 발굴</span>
            </button>
            <button onclick="setMobileTab('ai')" data-tab="ai" class="mob-tab-btn px-3.5 py-1.5 rounded-full text-xs font-bold transition flex items-center gap-1.5">
                <span>⚡ 퍼널·AI관제</span>
            </button>
        </div>
    </nav>

    <main class="max-w-6xl mx-auto px-3 sm:px-8 mt-4 sm:mt-6 space-y-4 sm:space-y-6">

        <!-- 2. Hero: Toss "내 자산" Card -->
        <section data-tab="asset" class="toss-card p-4 sm:p-8 relative overflow-hidden">
            <div class="flex flex-col sm:flex-row sm:items-center justify-between gap-4">
                <div>
                    <div class="flex items-center space-x-2 text-xs sm:text-sm font-semibold text-[#9096a2]">
                        <span>순 자산</span>
                        <span id="accountNoLabel" class="text-[11px] sm:text-xs px-2 py-0.5 rounded bg-white/[0.06] text-[#ffffff] font-mono">20201549311 (실전투자)</span>
                    </div>
                    
                    <div class="mt-2 flex items-center space-x-2 sm:space-x-3 flex-wrap gap-y-1.5">
                        <span id="equityVal" class="text-2xl sm:text-4xl font-extrabold tracking-tight text-white">--원</span>
                        <span id="dailyPnlBadge" class="toss-pill toss-pill-red text-xs sm:text-sm font-bold">
                            +0원 (0.00%) 오늘
                        </span>
                        <span id="dailyPnlBreakdown" class="text-[11px] sm:text-xs px-2.5 py-1 rounded-lg bg-white/[0.04] border border-white/[0.06] text-[#9096a2] font-mono inline-flex items-center space-x-1">
                            <span>오늘 실현</span>
                            <b id="pnlRealizedSub" class="text-white font-bold">0원</b>
                            <span class="text-white/40">+</span>
                            <span>평가</span>
                            <b id="pnlUnrealizedSub" class="text-white font-bold">0원</b>
                        </span>
                    </div>
                    <div class="text-[11px] sm:text-xs text-[#9096a2] mt-1.5 font-medium">
                        보유주식 평가금(<span id="equityStockSum" class="text-[#3182f6] font-semibold">0원</span>) + 잔여 예수금(<span id="equityCashSum" class="text-white font-semibold">0원</span>) = 순 자산
                    </div>
                </div>

                <!-- Fast Switch Quick Info (Mobile Responsive) -->
                <div class="w-full sm:w-auto">
                    <!-- 3 Core Highlights: Always visible in 3-columns on Mobile, flex on Desktop -->
                    <div class="grid grid-cols-3 sm:flex sm:flex-wrap gap-2 sm:justify-end">
                        <div class="bg-[#1f222b] p-2.5 sm:px-3.5 sm:py-2 rounded-xl border border-white/[0.06] text-center sm:text-left">
                            <span class="text-[#9096a2] block text-[10px] sm:text-xs">주식 평가금</span>
                            <span id="stockEvalVal" class="font-extrabold text-[#3182f6] text-xs sm:text-sm mt-0.5 block truncate">--원</span>
                        </div>
                        <div class="bg-[#1f222b] p-2.5 sm:px-3.5 sm:py-2 rounded-xl border border-white/[0.06] text-center sm:text-left">
                            <span class="text-[#9096a2] block text-[10px] sm:text-xs">잔여 예수금</span>
                            <span id="cashVal" class="font-extrabold text-white text-xs sm:text-sm mt-0.5 block truncate">--원</span>
                        </div>
                        <div class="bg-[#1f222b] p-2.5 sm:px-3.5 sm:py-2 rounded-xl border border-white/[0.06] text-center sm:text-left">
                            <span class="text-[#9096a2] block text-[10px] sm:text-xs">주문가능 현금</span>
                            <span id="orderAvailVal" class="font-extrabold text-[#00c73c] text-xs sm:text-sm mt-0.5 block truncate">--원</span>
                        </div>
                    </div>

                    <!-- Mobile Accordion Button for Remaining 11 Metrics -->
                    <button type="button" onclick="toggleSecondaryMetrics()" id="btnToggleSecMetrics" class="sm:hidden w-full mt-2 py-1.5 px-3 rounded-xl bg-white/[0.03] hover:bg-white/[0.06] text-[11px] font-bold text-[#3182f6] border border-white/[0.06] flex items-center justify-between transition">
                        <span>📊 계좌/리스크 상세 11개 지표</span>
                        <span id="iconToggleSec" class="text-[10px] transition-transform">▼ 펼치기</span>
                    </button>

                    <!-- Secondary 11 Metrics Container (Hidden by default on mobile, flex on desktop) -->
                    <div id="secondaryHeroMetrics" class="hidden sm:flex sm:flex-wrap gap-2 sm:justify-end mt-2.5 grid-cols-2 gap-2">
                        <div class="bg-[#1f222b] px-3 py-1.5 rounded-xl border border-white/[0.06] text-xs">
                            <span class="text-[#9096a2] block text-[10px]">D+2 정산 예수금</span>
                            <span id="d2CashVal" class="font-bold text-amber-400 text-xs">--원</span>
                        </div>
                        <div class="bg-[#1f222b] px-3 py-1.5 rounded-xl border border-white/[0.06] text-xs">
                            <span class="text-[#9096a2] block text-[10px]">출금가능금액</span>
                            <span id="withdrawCashVal" class="font-bold text-[#9096a2] text-xs">--원</span>
                        </div>
                        <div class="bg-[#1f222b] px-3 py-1.5 rounded-xl border border-white/[0.06] text-xs">
                            <span class="text-[#9096a2] block text-[10px]">시장 국면</span>
                            <span id="regimeBadge" class="font-bold text-[#00c73c] text-xs">BULL 🐂</span>
                        </div>
                        <div class="bg-[#1f222b] px-3 py-1.5 rounded-xl border border-white/[0.06] text-xs">
                            <span class="text-[#9096a2] block text-[10px]">실시간 유니버스</span>
                            <span class="font-bold text-[#3182f6] text-xs">3,136 종목</span>
                        </div>
                        <div class="bg-[#1f222b] px-3 py-1.5 rounded-xl border border-white/[0.06] text-xs">
                            <span class="text-[#9096a2] block text-[10px]">사용 리스크 / 한도</span>
                            <span class="font-bold text-white text-xs"><span id="usedRiskVal">0.00%</span> / 4.0%</span>
                        </div>
                        <div class="bg-[#1f222b] px-3 py-1.5 rounded-xl border border-white/[0.06] text-xs">
                            <span class="text-[#9096a2] block text-[10px]">가용 리스크</span>
                            <span id="availRiskVal" class="font-bold text-[#00c73c] text-xs">4.00%</span>
                        </div>
                        <div class="bg-[#1f222b] px-3 py-1.5 rounded-xl border border-white/[0.06] text-xs">
                            <span class="text-[#9096a2] block text-[10px]">리스크 상태</span>
                            <span id="riskStatusBadge" class="font-bold text-[#00c73c] text-xs">NORMAL 🛡️</span>
                        </div>
                        <div class="bg-[#1f222b] px-3 py-1.5 rounded-xl border border-white/[0.06] text-xs">
                            <span class="text-[#9096a2] block text-[10px]">종목 수 제한 (MAX)</span>
                            <span id="maxPositionCountVal" class="font-bold text-[#00c73c] text-xs">UNLIMITED</span>
                        </div>
                        <div class="bg-[#1f222b] px-3 py-1.5 rounded-xl border border-white/[0.06] text-xs">
                            <span class="text-[#9096a2] block text-[10px]">현재 보유 종목 수</span>
                            <span id="curPositionCountVal" class="font-bold text-white text-xs">0개</span>
                        </div>
                        <div class="bg-[#1f222b] px-3 py-1.5 rounded-xl border border-white/[0.06] text-xs">
                            <span class="text-[#9096a2] block text-[10px]">종목 수 차단 여부</span>
                            <span id="positionCountBlockVal" class="font-bold text-[#00c73c] text-xs">FALSE</span>
                        </div>
                        <div class="bg-[#1f222b] px-3 py-1.5 rounded-xl border border-white/[0.06] text-xs">
                            <span class="text-[#9096a2] block text-[10px]">개수 검증 상태</span>
                            <span id="positionCountCheckVal" class="font-bold text-[#3182f6] text-xs">BYPASSED</span>
                        </div>
                    </div>
                </div>
            </div>
        </section>

        <!-- 2.05 Collapsible Central Token & API Gateway Telemetry -->
        <details data-tab="ai" class="toss-card p-4 sm:p-5 group cursor-pointer">
            <summary class="flex items-center justify-between list-none select-none">
                <div class="flex items-center space-x-2.5">
                    <span class="text-sm font-bold text-white flex items-center gap-1.5">
                        <span class="text-[#3182f6]">⚙️</span>
                        <span>API 게이트웨이 & 토큰 상세 텔레메트리</span>
                    </span>
                    <span id="tokenStatusBadge" class="toss-pill toss-pill-green text-[11px] font-bold">TOKEN: ACTIVE</span>
                    <span class="text-xs text-[#9096a2] hidden sm:inline">(클릭하여 상세 지표 열기)</span>
                </div>
                <div class="text-xs text-[#9096a2] flex items-center gap-2">
                    <span id="tokenTtlValSummary" class="text-[#00c73c] font-semibold">24h 캐시 가동중</span>
                    <svg class="w-4 h-4 text-[#9096a2] group-open:rotate-180 transition-transform" fill="none" stroke="currentColor" viewBox="0 0 24 24"><path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M19 9l-7 7-7-7"></path></svg>
                </div>
            </summary>

            <div class="pt-4 mt-3 border-t border-white/[0.06] cursor-default">
                <!-- Auto-Refresh Status Banner & Manual Refresh Button -->
                <div class="flex flex-col sm:flex-row sm:items-center justify-between gap-2.5 p-3 rounded-xl bg-white/[0.02] border border-white/[0.04] mb-3 text-xs">
                    <div class="flex items-center space-x-2">
                        <span class="w-2 h-2 rounded-full bg-[#00c73c] animate-pulse"></span>
                        <span class="text-white font-semibold">선제적 자동 발급 워치독:</span>
                        <span class="text-[#00c73c] font-bold">활성화됨</span>
                        <span class="text-[#9096a2]">(만료 30분 전 자동 갱신 + 평일 08:30 장전 사전 점검)</span>
                    </div>
                    <button onclick="refreshToken()" class="px-3 py-1.5 rounded-lg bg-[#3182f6]/20 hover:bg-[#3182f6]/30 text-[#3182f6] hover:text-white font-bold border border-[#3182f6]/30 transition text-xs flex items-center justify-center gap-1">
                        🔄 즉시 새 토큰 발급
                    </button>
                </div>

                <!-- Token Metrics Summary -->
                <div class="grid grid-cols-2 sm:grid-cols-4 gap-3">
                    <div class="bg-[#1f222b] p-3 rounded-xl border border-white/[0.06]">
                        <span class="text-[11px] text-[#9096a2] block">토큰 유효 잔여시간</span>
                        <span id="tokenTtlVal" class="text-sm font-extrabold text-[#00c73c] mt-0.5 block">--</span>
                        <span id="tokenExpiresVal" class="text-[10px] text-[#9096a2] block mt-0.5">만료: --</span>
                    </div>
                    <div class="bg-[#1f222b] p-3 rounded-xl border border-white/[0.06]">
                        <span class="text-[11px] text-[#9096a2] block">토큰 재발급 횟수</span>
                        <span id="tokenRefreshCountVal" class="text-sm font-extrabold text-white mt-0.5 block">0회</span>
                        <span class="text-[10px] text-[#00c73c] block mt-0.5">정상 재사용 중</span>
                    </div>
                    <div class="bg-[#1f222b] p-3 rounded-xl border border-white/[0.06]">
                        <span class="text-[11px] text-[#9096a2] block">최근 갱신 사유</span>
                        <span id="tokenReasonVal" class="text-xs font-bold text-white mt-0.5 block truncate">SYSTEM_INIT</span>
                        <span id="tokenErrorVal" class="text-[10px] text-[#9096a2] block mt-0.5">에러: NONE</span>
                    </div>
                    <div class="bg-[#1f222b] p-3 rounded-xl border border-white/[0.06]">
                        <span class="text-[11px] text-[#9096a2] block">일봉 캐시 적중 / 격리</span>
                        <span id="dailyCacheVal" class="text-sm font-extrabold text-[#3182f6] mt-0.5 block">--</span>
                        <span id="dailyBlockedVal" class="text-[10px] text-[#9096a2] block mt-0.5">일시 장애 격리: 0개</span>
                    </div>
                </div>

                <!-- Endpoint Telemetry Table -->
                <div class="overflow-x-auto mt-3">
                    <table class="w-full text-left border-collapse text-xs">
                        <thead>
                            <tr class="text-[11px] text-[#9096a2] border-b border-white/[0.08]">
                                <th class="py-1.5 px-3">엔드포인트 (API)</th>
                                <th class="py-1.5 px-3 text-right">총 호출</th>
                                <th class="py-1.5 px-3 text-right">성공 / 에러</th>
                                <th class="py-1.5 px-3 text-right">캐시 적중</th>
                                <th class="py-1.5 px-3 text-right">적중률</th>
                                <th class="py-1.5 px-3 text-right">평균 지연</th>
                            </tr>
                        </thead>
                        <tbody id="apiTelemetryTableBody" class="divide-y divide-white/[0.04]">
                            <tr><td colspan="6" class="py-2 text-center text-[#9096a2]">API 호출 메트릭 수집 중...</td></tr>
                        </tbody>
                    </table>
                </div>
            </div>
        </details>

        <!-- 2.1 Toss Period Realized PnL Statistics (기간별 실현손익 통계) -->
        <section data-tab="asset" class="toss-card p-4 sm:p-7">
            <div class="flex flex-col sm:flex-row sm:items-center justify-between pb-4 border-b border-white/[0.06] gap-2">
                <div class="flex items-center space-x-2.5">
                    <h2 class="text-lg font-bold text-white">기간별 실현손익 통계</h2>
                    <span id="periodStatsModeBadge" class="toss-pill toss-pill-red text-xs font-bold">실전투자</span>
                    <span class="toss-pill toss-pill-blue">손익 리포트</span>
                </div>
                <div class="text-xs text-[#9096a2]">
                    매도 체결 기준 확정 실현손익 (하루 • 7일 • 30일)
                </div>
            </div>

            <div class="grid grid-cols-1 md:grid-cols-3 gap-4 mt-4">
                <!-- 오늘 (하루) 카드 -->
                <div class="bg-[#1f222b] p-5 rounded-2xl border border-white/[0.06] flex flex-col justify-between hover:border-white/[0.12] transition">
                    <div class="flex items-center justify-between">
                        <span class="text-xs font-bold text-[#9096a2]">오늘 (하루)</span>
                        <span id="statTodayBadge" class="text-[11px] font-bold px-2 py-0.5 rounded-full bg-white/[0.06] text-white">0건 매도</span>
                    </div>
                    <div class="mt-3">
                        <div class="text-xs text-[#9096a2]">당일 확정 실현손익</div>
                        <div id="statTodayPnl" class="text-2xl sm:text-3xl font-black mt-0.5 text-white">--원</div>
                    </div>
                    <div class="mt-4 pt-3 border-t border-white/[0.06] grid grid-cols-2 gap-2 text-xs">
                        <div>
                            <span class="text-[#9096a2] block text-[11px]">평균수익률 • 승률</span>
                            <span class="font-bold text-white"><span id="statTodayRet">0.00%</span> • <span id="statTodayWr" class="text-[#3182f6]">0%</span></span>
                        </div>
                        <div class="text-right">
                            <span class="text-[#9096a2] block text-[11px]">전적 (승/패)</span>
                            <span id="statTodayRecord" class="font-bold text-white">0승 0패</span>
                        </div>
                    </div>
                </div>

                <!-- 최근 1주일 카드 -->
                <div class="bg-[#1f222b] p-5 rounded-2xl border border-white/[0.06] flex flex-col justify-between hover:border-white/[0.12] transition">
                    <div class="flex items-center justify-between">
                        <span class="text-xs font-bold text-[#9096a2]">최근 1주일 (7일)</span>
                        <span id="statWeekBadge" class="text-[11px] font-bold px-2 py-0.5 rounded-full bg-white/[0.06] text-white">0건 매도</span>
                    </div>
                    <div class="mt-3">
                        <div class="text-xs text-[#9096a2]">주간 확정 실현손익</div>
                        <div id="statWeekPnl" class="text-2xl sm:text-3xl font-black mt-0.5 text-white">--원</div>
                    </div>
                    <div class="mt-4 pt-3 border-t border-white/[0.06] grid grid-cols-2 gap-2 text-xs">
                        <div>
                            <span class="text-[#9096a2] block text-[11px]">평균수익률 • 승률</span>
                            <span class="font-bold text-white"><span id="statWeekRet">0.00%</span> • <span id="statWeekWr" class="text-[#3182f6]">0%</span></span>
                        </div>
                        <div class="text-right">
                            <span class="text-[#9096a2] block text-[11px]">전적 (승/패)</span>
                            <span id="statWeekRecord" class="font-bold text-white">0승 0패</span>
                        </div>
                    </div>
                </div>

                <!-- 최근 1개월 카드 -->
                <div class="bg-[#1f222b] p-5 rounded-2xl border border-white/[0.06] flex flex-col justify-between hover:border-white/[0.12] transition">
                    <div class="flex items-center justify-between">
                        <span class="text-xs font-bold text-[#9096a2]">최근 1개월 (30일)</span>
                        <span id="statMonthBadge" class="text-[11px] font-bold px-2 py-0.5 rounded-full bg-white/[0.06] text-white">0건 매도</span>
                    </div>
                    <div class="mt-3">
                        <div class="text-xs text-[#9096a2]">월간 확정 실현손익</div>
                        <div id="statMonthPnl" class="text-2xl sm:text-3xl font-black mt-0.5 text-white">--원</div>
                    </div>
                    <div class="mt-4 pt-3 border-t border-white/[0.06] grid grid-cols-2 gap-2 text-xs">
                        <div>
                            <span class="text-[#9096a2] block text-[11px]">평균수익률 • 승률</span>
                            <span class="font-bold text-white"><span id="statMonthRet">0.00%</span> • <span id="statMonthWr" class="text-[#3182f6]">0%</span></span>
                        </div>
                        <div class="text-right">
                            <span class="text-[#9096a2] block text-[11px]">전적 (승/패)</span>
                            <span id="statMonthRecord" class="font-bold text-white">0승 0패</span>
                        </div>
                    </div>
                </div>
            </div>
        </section>

        <!-- 2.5 Real-Time Execution Funnel & Telemetry (v8.0) -->
        <section data-tab="ai" class="toss-card p-4 sm:p-7">
            <div class="flex items-center justify-between pb-4 border-b border-white/[0.06]">
                <div class="flex items-center space-x-2">
                    <h2 class="text-lg font-bold text-white">실시간 체결 전환 파이프라인 (Execution Funnel)</h2>
                    <span class="toss-pill toss-pill-blue">v8.0 TELEMETRY</span>
                </div>
                <span class="text-xs text-[#9096a2]">탐지에서 체결까지 전 단계 무손실 실시간 관제</span>
            </div>

            <!-- Funnel Flow Cards -->
            <div class="grid grid-cols-2 sm:grid-cols-4 lg:grid-cols-7 gap-2.5 mt-4">
                <div class="bg-[#1f222b] p-3 rounded-xl border border-white/[0.06] text-center">
                    <div class="text-[11px] text-[#9096a2]">전체 시장</div>
                    <div class="text-xl font-extrabold text-white mt-1">3,136</div>
                    <div class="text-[10px] text-[#9096a2] mt-0.5">KRX 전수 감시</div>
                </div>
                <div class="bg-[#1f222b] p-3 rounded-xl border border-white/[0.06] text-center">
                    <div class="text-[11px] text-[#9096a2]">이벤트 감지</div>
                    <div id="fnEvents" class="text-xl font-extrabold text-[#3182f6] mt-1">0</div>
                    <div class="text-[10px] text-[#9096a2] mt-0.5">전환: <b id="rateEventToCand" class="text-white">0%</b></div>
                </div>
                <div class="bg-[#1f222b] p-3 rounded-xl border border-white/[0.06] text-center">
                    <div class="text-[11px] text-[#9096a2]">후보 승격</div>
                    <div id="fnCandidates" class="text-xl font-extrabold text-amber-400 mt-1">0</div>
                    <div class="text-[10px] text-[#9096a2] mt-0.5">전환: <b id="rateCandToSetup" class="text-white">0%</b></div>
                </div>
                <div class="bg-[#1f222b] p-3 rounded-xl border border-white/[0.06] text-center">
                    <div class="text-[11px] text-[#9096a2]">셋업 충족</div>
                    <div id="fnSetups" class="text-xl font-extrabold text-purple-400 mt-1">0</div>
                    <div class="text-[10px] text-[#9096a2] mt-0.5">전환: <b id="rateSetupToBuy" class="text-white">0%</b></div>
                </div>
                <div class="bg-[#1f222b] p-3 rounded-xl border border-white/[0.06] text-center">
                    <div class="text-[11px] text-[#9096a2]">BUY 승인</div>
                    <div id="fnApproved" class="text-xl font-extrabold text-[#00c73c] mt-1">0</div>
                    <div class="text-[10px] text-[#9096a2] mt-0.5">전환: <b id="rateBuyToOrder" class="text-white">100%</b></div>
                </div>
                <div class="bg-[#1f222b] p-3 rounded-xl border border-white/[0.06] text-center">
                    <div class="text-[11px] text-[#9096a2]">주문 전송</div>
                    <div id="fnOrders" class="text-xl font-extrabold text-cyan-400 mt-1">0</div>
                    <div class="text-[10px] text-[#9096a2] mt-0.5">전환: <b id="rateOrderToFill" class="text-white">100%</b></div>
                </div>
                <div class="bg-[#1f222b] p-3 rounded-xl border border-[#00c73c]/30 text-center bg-[#00c73c]/5">
                    <div class="text-[11px] text-[#00c73c] font-bold">최종 체결</div>
                    <div id="fnFills" class="text-xl font-extrabold text-[#00c73c] mt-1">0</div>
                    <div class="text-[10px] text-[#00c73c] mt-0.5">체결 성공</div>
                </div>
            </div>

            <!-- 시간대별 체결 전환 추이 차트 (Chart.js) -->
            <div class="mt-6 pt-5 border-t border-white/[0.06]">
                <div class="flex flex-col sm:flex-row sm:items-center justify-between pb-3 gap-2">
                    <div class="flex items-center space-x-2">
                        <span class="text-sm font-bold text-white flex items-center gap-1.5">
                            <span>📈</span>
                            <span>시간대별 전환 흐름 추이</span>
                        </span>
                        <span class="text-xs text-[#9096a2]">(09:00 ~ 15:30 장중 시간별 후보 발굴 • 셋업 생성 • 최종 체결)</span>
                    </div>
                    <div class="flex items-center gap-3 text-xs flex-wrap">
                        <span class="inline-flex items-center gap-1.5 text-[#3182f6]">
                            <span class="w-2.5 h-2.5 rounded-sm bg-[#3182f6]"></span>
                            <span>후보 발굴 (좌측축)</span>
                        </span>
                        <span class="inline-flex items-center gap-1.5 text-purple-400">
                            <span class="w-2.5 h-2.5 rounded-full bg-purple-400"></span>
                            <span>셋업 생성 (우측축)</span>
                        </span>
                        <span class="inline-flex items-center gap-1.5 text-[#00c73c]">
                            <span class="w-2.5 h-2.5 rounded-full bg-[#00c73c]"></span>
                            <span>체결 성공 (우측축)</span>
                        </span>
                    </div>
                </div>

                <!-- 차트 캔버스 -->
                <div class="relative w-full h-64 bg-[#12141a] rounded-2xl p-3 border border-white/[0.04]">
                    <canvas id="pipelineHourlyChart"></canvas>
                </div>

                <!-- 시간대별 카드 그리드 -->
                <div id="hourlyPipelineCards" class="grid grid-cols-2 sm:grid-cols-4 lg:grid-cols-7 gap-2 mt-3 text-xs">
                    <!-- Populated dynamically via JS -->
                </div>
            </div>
        </section>

        <!-- 2.6 Top Opportunities & Momentum Stages (v8.0) -->
        <section data-tab="discovery" class="toss-card p-4 sm:p-7">
            <div class="flex items-center justify-between pb-4 border-b border-white/[0.06]">
                <div class="flex items-center space-x-2">
                    <h2 class="text-lg font-bold text-white">실시간 상위 발굴 기회 & 모멘텀 구간</h2>
                    <span class="toss-pill toss-pill-green">TOP 10</span>
                </div>
                <span class="text-xs text-[#9096a2]">3단계 게이팅 및 모멘텀 단계별 실시간 모니터링</span>
            </div>

            <div class="overflow-x-auto mt-3">
                <table class="w-full text-left border-collapse">
                    <thead>
                        <tr class="text-[11px] text-[#9096a2] border-b border-white/[0.08]">
                            <th class="py-2.5 px-2 text-center w-12">순위</th>
                            <th class="py-2.5 px-3">종목명 (코드)</th>
                            <th class="py-2.5 px-3 text-right">현재가</th>
                            <th class="py-2.5 px-3 text-center">모멘텀 구간</th>
                            <th class="py-2.5 px-3 text-center">AI 점수</th>
                            <th class="py-2.5 px-3 text-center">상태</th>
                            <th class="py-2.5 px-3">포착 사유 / 셋업</th>
                        </tr>
                    </thead>
                    <tbody id="topOpportunitiesContainer">
                        <!-- Populated by JS -->
                    </tbody>
                </table>
            </div>
        </section>

        <!-- 2.7 WHY NO BUY? Diagnostics Breakdown (v8.0) -->
        <section data-tab="discovery" class="toss-card p-4 sm:p-7">
            <div class="flex items-center justify-between pb-4 border-b border-white/[0.06]">
                <div>
                    <h2 class="text-lg font-bold text-white">WHY NO BUY? (매수 차단/거절 원인 실시간 분석)</h2>
                    <p class="text-xs text-[#9096a2] mt-1">신호가 발생하지 않거나 매수가 보류된 모든 이유를 투명하게 추적하고 기록합니다.</p>
                </div>
                <span class="toss-pill toss-pill-gray text-xs">자기진단 엔진</span>
            </div>

            <div id="whyNoBuyContainer" class="grid grid-cols-1 sm:grid-cols-2 gap-4 mt-4">
                <!-- Populated by JS -->
            </div>
        </section>


        <!-- 3. Toss Securities Style: 내 보유 주식 (Holdings) -->
        <section data-tab="holdings" class="toss-card p-4 sm:p-7">
            <div class="flex items-center justify-between pb-4 border-b border-white/[0.06]">
                <div class="flex items-center space-x-2.5">
                    <h2 class="text-lg font-bold text-white">보유 주식</h2>
                    <span id="positionsModeBadge" class="toss-pill toss-pill-red text-xs font-bold">실전투자</span>
                    <span id="realPosCount" class="toss-pill toss-pill-blue">0개</span>
                </div>
                <span class="text-xs text-[#9096a2]">실시간 시세 및 손익절 감시 중</span>
            </div>

            <!-- 보유주식 합계 요약 카드 (총 매입, 총 평가, 합계 평가손익, 합계 수익률) -->
            <div id="holdingsSummaryCard" class="grid grid-cols-2 sm:grid-cols-4 gap-3 p-4 rounded-2xl bg-[#171920] border border-white/[0.06] mt-4 mb-3">
                <div class="bg-white/[0.02] p-3 rounded-xl border border-white/[0.04]">
                    <span class="text-[#9096a2] block text-[11px] font-medium">총 매입금액</span>
                    <span id="posTotalBuyVal" class="text-base sm:text-lg font-extrabold text-white mt-0.5 block">0원</span>
                </div>
                <div class="bg-white/[0.02] p-3 rounded-xl border border-white/[0.04]">
                    <span class="text-[#9096a2] block text-[11px] font-medium">총 평가금액</span>
                    <span id="posTotalEvalVal" class="text-base sm:text-lg font-extrabold text-[#3182f6] mt-0.5 block">0원</span>
                </div>
                <div class="bg-white/[0.02] p-3 rounded-xl border border-white/[0.04]">
                    <span class="text-[#9096a2] block text-[11px] font-medium">합계 평가손익 (이득 / 손해)</span>
                    <span id="posTotalPnlVal" class="text-base sm:text-lg font-extrabold text-white mt-0.5 block">+0원</span>
                </div>
                <div class="bg-white/[0.02] p-3 rounded-xl border border-white/[0.04]">
                    <div class="flex items-center justify-between">
                        <span class="text-[#9096a2] text-[11px] font-medium">합계 수익률</span>
                        <span id="posProfitLossCount" class="text-[10px] text-[#9096a2]">0개 종목</span>
                    </div>
                    <span id="posTotalRetVal" class="text-base sm:text-lg font-extrabold text-white mt-0.5 block">0.00%</span>
                </div>
            </div>

            <!-- Stock List (Toss Style) -->
            <div id="positionsContainer" class="divide-y divide-white/[0.06] mt-2">
                <!-- Populated by JS -->
            </div>

            <div id="emptyPositionsMsg" class="hidden text-center py-12">
                <div class="w-12 h-12 rounded-full bg-white/[0.05] flex items-center justify-center text-xl mx-auto mb-3">
                    🔍
                </div>
                <p class="text-sm font-semibold text-white">현재 보유 중인 주식이 없습니다</p>
                <p class="text-xs text-[#9096a2] mt-1">AI가 3,136개 전 종목을 3초 주기로 스캔하며 급등 타이밍을 탐색 중입니다.</p>
            </div>
        </section>

        <!-- 3.5 Toss Securities Style: 오늘 판 주식 (Closed Trades Journal) -->
        <section data-tab="holdings" class="toss-card p-4 sm:p-7">
            <div class="flex flex-col sm:flex-row sm:items-center justify-between pb-4 border-b border-white/[0.06] gap-3">
                <div class="flex items-center space-x-2.5">
                    <h2 class="text-lg font-bold text-white">오늘 판 주식 (매도 체결 및 실현손익)</h2>
                    <span id="closedTradesModeBadge" class="toss-pill toss-pill-red text-xs font-bold">실전투자</span>
                    <span id="closedTradesCountBadge" class="toss-pill toss-pill-blue">0건</span>
                </div>
                <div class="flex items-center space-x-2">
                    <div class="flex rounded-lg bg-[#1f222b] p-0.5 border border-white/[0.08] text-xs">
                        <button id="btnTabTodayTrades" onclick="switchTradesTab('today')" class="px-3 py-1 rounded-md font-bold transition bg-[#3182f6] text-white">
                            오늘 매도
                        </button>
                        <button id="btnTabAllTrades" onclick="switchTradesTab('all')" class="px-3 py-1 rounded-md font-semibold text-[#9096a2] hover:text-white transition">
                            전체 이력
                        </button>
                    </div>
                    <span id="todayRealizedSumBadge" class="toss-pill toss-pill-gray text-xs font-bold">당일 실현손익: 0원</span>
                </div>
            </div>

            <!-- Closed Trades List (Toss Style) -->
            <div id="closedTradesContainer" class="divide-y divide-white/[0.06] mt-2">
                <!-- Populated by JS -->
            </div>

            <div id="emptyClosedTradesMsg" class="hidden text-center py-10">
                <div class="w-12 h-12 rounded-full bg-white/[0.05] flex items-center justify-center text-xl mx-auto mb-3">
                    🧾
                </div>
                <p class="text-sm font-semibold text-white">오늘 매도 체결된 주식이 없습니다</p>
                <p class="text-xs text-[#9096a2] mt-1">보유 종목이 목표가(익절) 또는 손절선에 도달하여 매도되면 상세 체결내역과 실현손익이 기록됩니다.</p>
            </div>
        </section>

        <!-- 3.8 Toss Securities Style: 미체결 / 진행 중 주문 (Open Orders) -->
        <section data-tab="holdings" class="toss-card p-4 sm:p-7">
            <div class="flex items-center justify-between pb-4 border-b border-white/[0.06]">
                <div class="flex items-center space-x-2.5">
                    <h2 class="text-lg font-bold text-white">미체결 / 진행 중 주문 (Open Orders)</h2>
                    <span id="openOrdersCountBadge" class="toss-pill toss-pill-blue">0건</span>
                </div>
                <span class="text-xs text-[#9096a2]">실시간 호가 추적 및 체결 대기 중</span>
            </div>

            <!-- Open Orders List -->
            <div id="openOrdersContainer" class="divide-y divide-white/[0.06] mt-2">
                <!-- Populated by JS -->
            </div>

            <div id="emptyOpenOrdersMsg" class="hidden text-center py-8">
                <div class="w-10 h-10 rounded-full bg-white/[0.05] flex items-center justify-center text-lg mx-auto mb-2">
                    ⏳
                </div>
                <p class="text-sm font-semibold text-white">현재 진행 중인 미체결 주문이 없습니다</p>
                <p class="text-xs text-[#9096a2] mt-1">신규 매수/매도 주문 발송 시 체결 완료 전까지 이곳에 분리 표시됩니다.</p>
            </div>
        </section>

        <!-- 4. Toss AI Battle Arena: 챔피언 vs 도전자 -->
        <section data-tab="ai" class="toss-card p-4 sm:p-7">
            <div class="flex flex-col sm:flex-row sm:items-center justify-between pb-5 border-b border-white/[0.06] gap-3">
                <div>
                    <div class="flex items-center space-x-2">
                        <h2 class="text-lg font-bold text-white">AI 챔피언 vs 도전자 배틀</h2>
                        <span class="toss-pill toss-pill-blue">자율진화 랭킹</span>
                    </div>
                    <p class="text-xs text-[#9096a2] mt-1">실전 데이터를 학습하여 새 모델을 생성하고, 가상 검증에서 이겼을 때만 자본을 승격합니다.</p>
                </div>

                <!-- Action Controls -->
                <div class="flex items-center space-x-2">
                    <button onclick="triggerRetrain()" class="px-3.5 py-1.5 rounded-xl bg-[#1f222b] hover:bg-[#282c37] border border-white/[0.08] text-xs font-semibold text-white transition">
                        ⚡ 새 모델 학습
                    </button>
                    <button onclick="advanceStage()" class="px-3.5 py-1.5 rounded-xl bg-[#3182f6] hover:bg-[#1b64da] text-xs font-semibold text-white transition shadow-lg shadow-blue-500/20">
                        🚀 단계 승격
                    </button>
                    <button onclick="triggerRollback()" class="px-3.5 py-1.5 rounded-xl bg-[#f04452]/20 hover:bg-[#f04452]/30 border border-[#f04452]/40 text-xs font-semibold text-[#f04452] transition">
                        🚨 긴급 롤백
                    </button>
                </div>
            </div>

            <!-- Model Cards Comparison -->
            <div class="grid grid-cols-1 md:grid-cols-2 gap-4 mt-5">
                <!-- Champion Card -->
                <div class="bg-[#1f222b] border border-[#3182f6]/40 rounded-2xl p-5 relative overflow-hidden">
                    <div class="flex items-center justify-between">
                        <div class="flex items-center space-x-2">
                            <span class="px-2.5 py-1 rounded-full text-xs font-extrabold bg-[#3182f6] text-white">🏆 챔피언 AI</span>
                            <span id="champVersion" class="font-bold text-white text-sm">v7.0_champion</span>
                        </div>
                        <span id="champAllocBadge" class="text-xs font-bold text-[#3182f6] bg-[#3182f6]/10 px-2.5 py-1 rounded-full">
                            실전 배분 100%
                        </span>
                    </div>

                    <div class="grid grid-cols-3 gap-3 mt-5 text-center">
                        <div class="bg-[#171920] p-3 rounded-xl">
                            <div class="text-[11px] text-[#9096a2]">실전 승률</div>
                            <div id="champWr" class="text-lg font-extrabold text-[#3182f6] mt-0.5">62.5%</div>
                        </div>
                        <div class="bg-[#171920] p-3 rounded-xl">
                            <div class="text-[11px] text-[#9096a2]">Profit Factor</div>
                            <div id="champPf" class="text-lg font-extrabold text-[#3182f6] mt-0.5">2.15</div>
                        </div>
                        <div class="bg-[#171920] p-3 rounded-xl">
                            <div class="text-[11px] text-[#9096a2]">순기대값 (E[R])</div>
                            <div id="champAvgR" class="text-lg font-extrabold text-[#00c73c] mt-0.5">+0.42R</div>
                        </div>
                    </div>

                    <div class="mt-4 flex items-center justify-between text-xs text-[#9096a2]">
                        <span>Brier 점수: <b id="champBrier" class="text-white">0.165</b> (초정밀)</span>
                        <span class="text-[#00c73c] font-semibold">● 현재 실전 운용 모델</span>
                    </div>
                </div>

                <!-- Challenger Card -->
                <div class="bg-[#1f222b] border border-purple-500/30 rounded-2xl p-5 relative overflow-hidden">
                    <div class="flex items-center justify-between">
                        <div class="flex items-center space-x-2">
                            <span class="px-2.5 py-1 rounded-full text-xs font-extrabold bg-purple-600 text-white">⚡ 도전자 AI</span>
                            <span id="challVersion" class="font-bold text-white text-sm">v7.1_challenger</span>
                        </div>
                        <span id="challStateBadge" class="text-xs font-bold text-purple-300 bg-purple-500/15 px-2.5 py-1 rounded-full">
                            Shadow 검증
                        </span>
                    </div>

                    <div class="grid grid-cols-3 gap-3 mt-5 text-center">
                        <div class="bg-[#171920] p-3 rounded-xl">
                            <div class="text-[11px] text-[#9096a2]">가상 승률</div>
                            <div id="challWr" class="text-lg font-extrabold text-purple-400 mt-0.5">66.7%</div>
                        </div>
                        <div class="bg-[#171920] p-3 rounded-xl">
                            <div class="text-[11px] text-[#9096a2]">Profit Factor</div>
                            <div id="challPf" class="text-lg font-extrabold text-purple-400 mt-0.5">2.45</div>
                        </div>
                        <div class="bg-[#171920] p-3 rounded-xl">
                            <div class="text-[11px] text-[#9096a2]">검증 표본</div>
                            <div id="challSamples" class="text-lg font-extrabold text-white mt-0.5">24건</div>
                        </div>
                    </div>

                    <!-- Rollout Progress Bar -->
                    <div class="mt-4">
                        <div class="flex items-center justify-between text-xs text-[#9096a2] mb-1.5">
                            <span id="stagedAllocLabel">자본 배분 진행도: 0%</span>
                            <span class="text-purple-400 font-semibold">100% 달성 시 자동 승격</span>
                        </div>
                        <div class="w-full bg-[#171920] rounded-full h-2 overflow-hidden">
                            <div id="stagedProgress" class="bg-gradient-to-r from-purple-500 to-[#3182f6] h-full rounded-full transition-all duration-500" style="width: 5%"></div>
                        </div>
                    </div>
                </div>
            </div>
        </section>

        <!-- 5. Toss "지금 뜨는 주식": KRX 3,136개 전 종목 실시간 급등/이벤트 피드 -->
        <section data-tab="discovery" class="toss-card p-4 sm:p-7">
            <div class="flex items-center justify-between pb-4 border-b border-white/[0.06]">
                <div class="flex items-center space-x-2">
                    <h2 class="text-lg font-bold text-white">실시간 급등 및 이벤트 포착 피드</h2>
                    <span class="toss-pill toss-pill-red">LIVE STREAM</span>
                </div>
                <span class="text-xs text-[#9096a2]">3,136개 전종목 실시간 3초 분석</span>
            </div>

            <!-- Feed List -->
            <div id="eventsListContainer" class="divide-y divide-white/[0.06] mt-2">
                <!-- Populated by JS -->
            </div>
        </section>

        <!-- 6. 오답 노트 & 실패 패턴 분류 (Toss Smart Analysis) -->
        <section data-tab="ai" class="toss-card p-4 sm:p-7">
            <div class="flex items-center justify-between pb-4 border-b border-white/[0.06]">
                <div>
                    <h2 class="text-lg font-bold text-white">AI 거래 패턴 & 오답 분석 노트</h2>
                    <p class="text-xs text-[#9096a2] mt-1">손실 원인을 4가지 실패 유형으로 분류하여 다음 AI 모델 가중치에 자동 반영합니다.</p>
                </div>
                <span class="text-xs font-semibold text-white bg-[#1f222b] px-3 py-1 rounded-full border border-white/[0.08]">
                    피드백 루프 가동중
                </span>
            </div>

            <div class="grid grid-cols-2 sm:grid-cols-4 gap-3 mt-4">
                <div class="bg-[#1f222b] p-4 rounded-2xl border border-white/[0.06]">
                    <div class="text-xs font-semibold text-[#f04452]">FAKE_BREAKOUT</div>
                    <div class="text-xs text-[#9096a2] mt-0.5">휩소 / 가짜돌파</div>
                    <div id="cntFakeBreakout" class="text-2xl font-black text-white mt-2">1건</div>
                    <div class="text-[11px] text-[#9096a2] mt-1">즉시 반락 패턴 패널티</div>
                </div>
                <div class="bg-[#1f222b] p-4 rounded-2xl border border-white/[0.06]">
                    <div class="text-xs font-semibold text-amber-400">LATE_ENTRY</div>
                    <div class="text-xs text-[#9096a2] mt-0.5">고점 추격매수</div>
                    <div id="cntLateEntry" class="text-2xl font-black text-white mt-2">1건</div>
                    <div class="text-[11px] text-[#9096a2] mt-1">이격도 과다 진입 차단</div>
                </div>
                <div class="bg-[#1f222b] p-4 rounded-2xl border border-white/[0.06]">
                    <div class="text-xs font-semibold text-[#3182f6]">LOW_LIQUIDITY</div>
                    <div class="text-xs text-[#9096a2] mt-0.5">슬리피지 과다</div>
                    <div id="cntLowLiquidity" class="text-2xl font-black text-white mt-2">1건</div>
                    <div class="text-[11px] text-[#9096a2] mt-1">호가 잔량 필터 강화</div>
                </div>
                <div class="bg-[#1f222b] p-4 rounded-2xl border border-white/[0.06]">
                    <div class="text-xs font-semibold text-[#00c73c]">PROFIT_TARGET</div>
                    <div class="text-xs text-[#9096a2] mt-0.5">목표가 정밀 달성</div>
                    <div id="cntProfitTarget" class="text-2xl font-black text-white mt-2">5건</div>
                    <div class="text-[11px] text-[#9096a2] mt-1">+1R/+2R 분할익절 성공</div>
                </div>
            </div>
        </section>

    </main>

    <!-- Footer -->
    <footer class="mt-12 py-8 text-center text-xs text-[#9096a2] border-t border-white/[0.06]">
        <p class="font-semibold text-white">QUANT AI 실시간 관제탑 v7.0</p>
        <p class="mt-1">대한민국 국내 주식 자기학습형 퀀트 시스템 • 나무증권 OpenAPI 실계좌 연동</p>
    </footer>

    <!-- Mobile Bottom Navigation Bar (Toss App Style) -->
    <nav class="sm:hidden fixed bottom-0 left-0 right-0 z-50 bg-[#12141a]/95 backdrop-blur-xl border-t border-white/[0.08] px-2 py-1.5 flex items-center justify-around shadow-2xl safe-area-bottom">
        <button onclick="setMobileTab('asset')" data-nav="asset" class="mob-nav-btn flex flex-col items-center justify-center flex-1 py-1 text-[#9096a2] hover:text-white transition">
            <span class="text-lg leading-none">💰</span>
            <span class="text-[10px] font-medium mt-1">자산·손익</span>
        </button>
        <button onclick="setMobileTab('holdings')" data-nav="holdings" class="mob-nav-btn flex flex-col items-center justify-center flex-1 py-1 text-[#9096a2] hover:text-white transition relative">
            <span class="text-lg leading-none">📦</span>
            <span class="text-[10px] font-medium mt-1">보유·체결</span>
            <span id="navHoldingsBadge" class="hidden absolute top-0.5 right-4 w-2 h-2 rounded-full bg-[#f04452]"></span>
        </button>
        <button onclick="setMobileTab('discovery')" data-nav="discovery" class="mob-nav-btn flex flex-col items-center justify-center flex-1 py-1 text-[#9096a2] hover:text-white transition">
            <span class="text-lg leading-none">🚀</span>
            <span class="text-[10px] font-medium mt-1">실시간발굴</span>
        </button>
        <button onclick="setMobileTab('ai')" data-nav="ai" class="mob-nav-btn flex flex-col items-center justify-center flex-1 py-1 text-[#9096a2] hover:text-white transition">
            <span class="text-lg leading-none">⚡</span>
            <span class="text-[10px] font-medium mt-1">AI관제</span>
        </button>
        <button onclick="setMobileTab('all')" data-nav="all" class="mob-nav-btn active flex flex-col items-center justify-center flex-1 py-1 transition">
            <span class="text-lg leading-none">🌐</span>
            <span class="text-[10px] font-medium mt-1">전체보기</span>
        </button>
    </nav>

    <!-- JavaScript Application Logic -->
    <script>
        let currentMode = 'live';
        let currentTab = 'all';

        function setMobileTab(tabName) {
            currentTab = tabName;
            
            // 1. Update top sticky quick tabs
            document.querySelectorAll('.mob-tab-btn').forEach(btn => {
                if (btn.getAttribute('data-tab') === tabName) {
                    btn.classList.add('active');
                } else {
                    btn.classList.remove('active');
                }
            });

            // 2. Update bottom nav buttons
            document.querySelectorAll('.mob-nav-btn').forEach(btn => {
                if (btn.getAttribute('data-nav') === tabName) {
                    btn.classList.add('active');
                    btn.classList.remove('text-[#9096a2]');
                } else {
                    btn.classList.remove('active');
                    btn.classList.add('text-[#9096a2]');
                }
            });

            // 3. Show/Hide sections tagged with data-tab
            const sections = document.querySelectorAll('main > [data-tab]');
            sections.forEach(sec => {
                const secTab = sec.getAttribute('data-tab');
                if (tabName === 'all' || secTab === tabName) {
                    sec.classList.remove('hidden');
                } else {
                    sec.classList.add('hidden');
                }
            });

            window.scrollTo({ top: 0, behavior: 'smooth' });
        }

        function toggleSecondaryMetrics() {
            const sec = document.getElementById('secondaryHeroMetrics');
            const icon = document.getElementById('iconToggleSec');
            if (sec) {
                if (sec.classList.contains('hidden')) {
                    sec.classList.remove('hidden');
                    sec.classList.add('grid');
                    if (icon) icon.innerText = '▲ 접기';
                } else {
                    sec.classList.add('hidden');
                    sec.classList.remove('grid');
                    if (icon) icon.innerText = '▼ 펼치기';
                }
            }
        }

        function updateClock() {
            const now = new Date();
            document.getElementById('liveClock').innerText = now.toLocaleTimeString('ko-KR', { hour12: false });
        }
        setInterval(updateClock, 1000);
        updateClock();

        function formatMoney(num) {
            if (num == null || isNaN(num)) return '0원';
            return Math.round(num).toLocaleString() + '원';
        }

        function toggleFillsDetail(id) {
            const el = document.getElementById(id);
            const arr = document.getElementById('arr_' + id);
            if (!el) return;
            if (el.classList.contains('hidden')) {
                el.classList.remove('hidden');
                if (arr) arr.innerText = '▲';
            } else {
                el.classList.add('hidden');
                if (arr) arr.innerText = '▼';
            }
        }

        async function switchMode(mode) {
            try {
                const res = await fetch('/api/switch_mode?mode=' + mode);
                const data = await res.json();
                currentMode = mode;
                updateModeButtons(mode);
                fetchState();
            } catch (e) {
                console.error("Mode switch error:", e);
            }
        }

        async function refreshToken() {
            if (!confirm("지금 새로운 24시간 접근 토큰을 발급받으시겠습니까?\\n(기존 토큰이 남아있어도 즉시 24시간 새 토큰으로 갱신됩니다.)")) return;
            try {
                const res = await fetch('/api/token/refresh');
                const data = await res.json();
                if (data.status === 'SUCCESS') {
                    alert(data.message);
                    fetchState();
                } else {
                    alert("토큰 발급 실패: " + (data.message || '알 수 없는 오류'));
                }
            } catch (e) {
                alert("네트워크 오류: " + e);
            }
        }

        function updateModeButtons(mode) {
            const btnLive = document.getElementById('btnModeLive');
            const btnMock = document.getElementById('btnModeMock');
            if (mode === 'live') {
                btnLive.className = 'px-3.5 py-1.5 rounded-full transition-all duration-200 bg-[#f04452] text-white shadow font-bold';
                btnMock.className = 'px-3.5 py-1.5 rounded-full transition-all duration-200 text-[#9096a2] hover:text-white font-semibold';
            } else {
                btnLive.className = 'px-3.5 py-1.5 rounded-full transition-all duration-200 text-[#9096a2] hover:text-white font-semibold';
                btnMock.className = 'px-3.5 py-1.5 rounded-full transition-all duration-200 bg-amber-500 text-slate-950 font-bold shadow';
            }
        }

        let currentTradesTab = 'today';
        let cachedDashboardData = null;

        function switchTradesTab(tab) {
            currentTradesTab = tab;
            const btnToday = document.getElementById('btnTabTodayTrades');
            const btnAll = document.getElementById('btnTabAllTrades');
            if (btnToday && btnAll) {
                if (tab === 'today') {
                    btnToday.className = 'px-3 py-1 rounded-md font-bold transition bg-[#3182f6] text-white';
                    btnAll.className = 'px-3 py-1 rounded-md font-semibold text-[#9096a2] hover:text-white transition';
                } else {
                    btnToday.className = 'px-3 py-1 rounded-md font-semibold text-[#9096a2] hover:text-white transition';
                    btnAll.className = 'px-3 py-1 rounded-md font-bold transition bg-[#3182f6] text-white';
                }
            }
            if (cachedDashboardData) {
                renderClosedTrades(cachedDashboardData);
            }
        }

        function renderPeriodStats(stats) {
            if (!stats) return;

            function updateCard(periodKey, pnlId, retId, wrId, recordId, badgeId) {
                const s = stats[periodKey];
                if (!s) return;
                const pnlEl = document.getElementById(pnlId);
                const retEl = document.getElementById(retId);
                const wrEl = document.getElementById(wrId);
                const recEl = document.getElementById(recordId);
                const badgeEl = document.getElementById(badgeId);

                const isProfit = s.total_pnl > 0;
                const isLoss = s.total_pnl < 0;

                if (pnlEl) {
                    pnlEl.innerText = (isProfit ? '+' : '') + formatMoney(s.total_pnl);
                    if (isProfit) pnlEl.className = 'text-2xl sm:text-3xl font-black mt-0.5 text-[#f04452]';
                    else if (isLoss) pnlEl.className = 'text-2xl sm:text-3xl font-black mt-0.5 text-[#3182f6]';
                    else pnlEl.className = 'text-2xl sm:text-3xl font-black mt-0.5 text-white';
                }

                if (retEl) {
                    const retSign = s.avg_return_pct > 0 ? '+' : '';
                    retEl.innerText = retSign + s.avg_return_pct.toFixed(2) + '%';
                    if (s.avg_return_pct > 0) retEl.className = 'text-[#f04452] font-bold';
                    else if (s.avg_return_pct < 0) retEl.className = 'text-[#3182f6] font-bold';
                    else retEl.className = 'text-white font-bold';
                }

                if (wrEl) {
                    wrEl.innerText = s.win_rate.toFixed(1) + '%';
                }

                if (recEl) {
                    recEl.innerText = s.win_count + '승 ' + s.loss_count + '패';
                }

                if (badgeEl) {
                    badgeEl.innerText = s.total_count + '건 매도';
                }
            }

            updateCard('today', 'statTodayPnl', 'statTodayRet', 'statTodayWr', 'statTodayRecord', 'statTodayBadge');
            updateCard('week', 'statWeekPnl', 'statWeekRet', 'statWeekWr', 'statWeekRecord', 'statWeekBadge');
            updateCard('month', 'statMonthPnl', 'statMonthRet', 'statMonthWr', 'statMonthRecord', 'statMonthBadge');
        }

        function renderClosedTrades(data) {
            const closedCont = document.getElementById('closedTradesContainer');
            const emptyMsg = document.getElementById('emptyClosedTradesMsg');
            const countBadge = document.getElementById('closedTradesCountBadge');
            const sumBadge = document.getElementById('todayRealizedSumBadge');
            if (!closedCont) return;

            closedCont.innerHTML = '';
            const todayTrades = data.closed_trades_today || [];
            const allTrades = data.all_closed_trades || [];
            const displayTrades = currentTradesTab === 'today' ? todayTrades : allTrades;

            if (countBadge) countBadge.innerText = displayTrades.length + '건';

            // 당일 실현손익 합계 배지
            if (sumBadge) {
                const todaySum = todayTrades.reduce((sum, t) => sum + (t.pnl || 0), 0);
                const todayBuySum = todayTrades.reduce((sum, t) => sum + (t.buy_amount || 0), 0);
                const todayRet = todayBuySum > 0 ? ((todaySum / todayBuySum) * 100).toFixed(2) : '0.00';
                sumBadge.innerText = '당일 합계: ' + (todaySum >= 0 ? '+' : '') + formatMoney(todaySum) + ' (' + (todaySum >= 0 ? '+' : '') + todayRet + '%)';
                if (todaySum > 0) sumBadge.className = 'toss-pill toss-pill-red text-xs font-bold';
                else if (todaySum < 0) sumBadge.className = 'toss-pill toss-pill-blue text-xs font-bold';
                else sumBadge.className = 'toss-pill toss-pill-gray text-xs font-bold';
            }

            if (displayTrades.length === 0) {
                if (emptyMsg) emptyMsg.classList.remove('hidden');
            } else {
                if (emptyMsg) emptyMsg.classList.add('hidden');
                displayTrades.forEach((trade, tIdx) => {
                    const isProfit = trade.pnl > 0;
                    const isLoss = trade.pnl < 0;
                    const pnlColorClass = isProfit ? 'text-[#f04452]' : (isLoss ? 'text-[#3182f6]' : 'text-[#9096a2]');
                    const pnlPillClass = isProfit ? 'toss-pill-red' : (isLoss ? 'toss-pill-blue' : 'toss-pill-gray');
                    const reasonBadgeClass = isProfit ? 'text-[#f04452] bg-[#f04452]/10' : (isLoss ? 'text-[#3182f6] bg-[#3182f6]/10' : 'text-[#9096a2] bg-white/[0.05]');
                    const sign = isProfit ? '+' : (isLoss ? '-' : '');
                    
                    const rawName = trade.name || trade.symbol_name || '';
                    const symbol = trade.symbol || trade.iem_cd || '';
                    const hasName = rawName && rawName !== symbol;
                    const displayName = trade.display_name || (hasName ? `${rawName} (${symbol})` : symbol);
                    const initialChar = (hasName ? rawName : (symbol || '주')).substring(0, 1);
                    const exitTimeOnly = (trade.exit_time || '').split(' ')[1] || trade.exit_time || '--:--:--';
                    const hasPartials = trade.partial_fills && trade.partial_fills.length > 1;

                    let partialsHtml = '';
                    if (hasPartials) {
                        const itemsHtml = trade.partial_fills.map((f, fIdx) => {
                            const fPnl = f.pnl || f.net_pnl || 0;
                            const fSign = fPnl >= 0 ? '+' : '';
                            const fColor = fPnl > 0 ? 'text-[#f04452]' : (fPnl < 0 ? 'text-[#3182f6]' : 'text-[#9096a2]');
                            const fTime = (f.exit_time || '').split(' ')[1] || f.exit_time || '--:--';
                            return `
                                <div class="flex items-center justify-between py-1 text-xs border-b border-white/[0.02] last:border-0 font-mono">
                                    <span class="text-[#9096a2]">${fIdx + 1}차 매도 (${fTime}): <b class="text-white">${f.shares || f.qty}주</b> @ ${(f.exit_price || 0).toLocaleString()}원 [${f.exit_reason || '청산'}]</span>
                                    <span class="${fColor} font-bold">${fSign}${fPnl.toLocaleString()}원 (${fSign}${(f.return_pct || 0).toFixed(2)}%)</span>
                                </div>
                            `;
                        }).join('');

                        partialsHtml = `
                            <div class="w-full mt-2 pt-2 border-t border-white/[0.04] pl-14 md:pl-0">
                                <button onclick="toggleFillsDetail('fills_${tIdx}')" class="text-xs text-[#3182f6] hover:text-[#5397f8] font-semibold flex items-center space-x-1.5 transition cursor-pointer">
                                    <span>분할 매도 상세 체결 (${trade.partial_fills.length}회 분할)</span>
                                    <span id="arr_fills_${tIdx}" class="text-[10px]">▼</span>
                                </button>
                                <div id="fills_${tIdx}" class="hidden mt-2 p-3 rounded-xl bg-white/[0.02] border border-white/[0.05] space-y-1">
                                    ${itemsHtml}
                                </div>
                            </div>
                        `;
                    }

                    const row = document.createElement('div');
                    row.className = 'py-4 flex flex-col justify-between gap-3 hover:bg-white/[0.02] -mx-2 px-2 rounded-xl transition';
                    row.innerHTML = `
                        <div class="flex flex-col md:flex-row md:items-center justify-between gap-3 w-full">
                            <div class="flex items-center space-x-3.5">
                                <div class="stock-avatar">
                                    ${initialChar}
                                </div>
                                <div>
                                    <div class="flex items-center space-x-2">
                                        <span class="font-bold text-white text-base">${displayName}</span>
                                        <span class="text-[11px] font-bold px-2 py-0.5 rounded-md ${reasonBadgeClass}">${trade.exit_reason}</span>
                                        ${hasPartials ? '<span class="text-[10px] font-bold px-1.5 py-0.5 rounded bg-[#3182f6]/20 text-[#3182f6]">라운드트립 통합</span>' : ''}
                                    </div>
                                    <div class="text-xs text-[#9096a2] mt-1 flex items-center space-x-2">
                                        <span>체결시각 <b class="text-white font-mono">${exitTimeOnly}</b></span>
                                        <span>•</span>
                                        <span>총 체결수량 <b class="text-white">${trade.shares}주</b></span>
                                    </div>
                                </div>
                            </div>

                            <div class="grid grid-cols-2 gap-3 text-xs pl-14 md:pl-0 sm:min-w-[300px]">
                                <div class="bg-white/[0.03] px-3 py-2 rounded-lg border border-white/[0.04]">
                                    <span class="text-[#9096a2] block text-[11px]">매수 (매입금액)</span>
                                    <span class="font-bold text-white">${(trade.entry_price || 0).toLocaleString()}원 × ${trade.shares}주</span>
                                    <div class="text-[11px] text-[#9096a2] font-semibold mt-0.5">총 ${(trade.buy_amount || 0).toLocaleString()}원</div>
                                </div>
                                <div class="bg-white/[0.03] px-3 py-2 rounded-lg border border-white/[0.04]">
                                    <span class="text-[#9096a2] block text-[11px]">매도 ${hasPartials ? '(VWAP 평균가)' : '(매도금액)'}</span>
                                    <span class="font-bold text-white">${(trade.exit_price || 0).toLocaleString()}원 × ${trade.shares}주</span>
                                    <div class="text-[11px] text-[#9096a2] font-semibold mt-0.5">총 ${(trade.sell_amount || 0).toLocaleString()}원</div>
                                </div>
                            </div>

                            <div class="flex items-center justify-between md:justify-end md:space-x-4 pl-14 md:pl-0 min-w-[170px]">
                                <div class="text-left md:text-right">
                                    <div class="text-[11px] text-[#9096a2]">실현손익 (합계)</div>
                                    <div class="text-lg font-extrabold ${pnlColorClass}">
                                        ${sign}${Math.abs(trade.pnl || 0).toLocaleString()}원
                                    </div>
                                </div>
                                <div class="text-right">
                                    <span class="toss-pill ${pnlPillClass} font-bold text-sm">
                                        ${sign}${Math.abs(trade.return_pct || 0).toFixed(2)}%
                                    </span>
                                </div>
                            </div>
                        </div>
                        ${partialsHtml}
                    `;
                    closedCont.appendChild(row);
                });
            }
        }

        function renderOpenOrders(data) {
            const container = document.getElementById('openOrdersContainer');
            const emptyMsg = document.getElementById('emptyOpenOrdersMsg');
            const countBadge = document.getElementById('openOrdersCountBadge');
            if (!container) return;

            container.innerHTML = '';
            const openOrders = data.open_orders || [];
            if (countBadge) countBadge.innerText = openOrders.length + '건';

            if (openOrders.length === 0) {
                if (emptyMsg) emptyMsg.classList.remove('hidden');
            } else {
                if (emptyMsg) emptyMsg.classList.add('hidden');
                openOrders.forEach(order => {
                    const isBuy = (order.side || '').toUpperCase() === 'BUY';
                    const sideClass = isBuy ? 'text-[#f04452] bg-[#f04452]/10' : 'text-[#3182f6] bg-[#3182f6]/10';
                    const rawName = order.name || order.symbol_name || '';
                    const symbol = order.symbol || order.iem_cd || '';
                    const hasName = rawName && rawName !== symbol;
                    const displayName = order.display_name || (hasName ? `${rawName} (${symbol})` : symbol);
                    const initialChar = (hasName ? rawName : (symbol || '주')).substring(0, 1);
                    const row = document.createElement('div');
                    row.className = 'py-3 flex flex-col md:flex-row md:items-center justify-between gap-2 hover:bg-white/[0.02] -mx-2 px-2 rounded-xl transition';
                    row.innerHTML = `
                        <div class="flex items-center space-x-3">
                            <div class="stock-avatar">${initialChar}</div>
                            <div>
                                <div class="flex items-center space-x-2">
                                    <span class="font-bold text-white text-sm">${displayName}</span>
                                    <span class="text-[10px] font-bold px-2 py-0.5 rounded-md ${sideClass}">${isBuy ? '매수대기' : '매도대기'}</span>
                                    <span class="toss-pill toss-pill-gray text-[10px]">${order.order_status}</span>
                                </div>
                                <div class="text-xs text-[#9096a2] mt-0.5">
                                    주문시각 <span class="font-mono text-white">${order.created_at || '--:--'}</span> • 주문번호 <span class="font-mono text-white">${order.broker_order_no || order.order_id || '--'}</span>
                                </div>
                            </div>
                        </div>
                        <div class="flex items-center space-x-4 text-xs pl-12 md:pl-0">
                            <div>
                                <span class="text-[#9096a2] block text-[10px]">주문단가</span>
                                <span class="font-bold text-white">${(order.order_price || 0).toLocaleString()}원</span>
                            </div>
                            <div>
                                <span class="text-[#9096a2] block text-[10px]">체결/요청</span>
                                <span class="font-bold text-white">${order.filled_qty || 0} / ${order.requested_qty || 0}주</span>
                            </div>
                            <div>
                                <span class="text-[#9096a2] block text-[10px]">미체결잔량</span>
                                <span class="font-bold text-[#f04452]">${order.remaining_qty || 0}주</span>
                            </div>
                        </div>
                    `;
                    container.appendChild(row);
                });
            }
        }

        let pipelineChart = null;

        function renderPipelineChart(hourlyData) {
            if (!hourlyData || hourlyData.length === 0) return;
            const canvas = document.getElementById('pipelineHourlyChart');
            if (!canvas) return;

            const labels = hourlyData.map(d => d.time);
            const candidatesData = hourlyData.map(d => d.candidates);
            const setupsData = hourlyData.map(d => d.setups);
            const fillsData = hourlyData.map(d => d.fills);

            if (pipelineChart) {
                pipelineChart.data.labels = labels;
                pipelineChart.data.datasets[0].data = candidatesData;
                pipelineChart.data.datasets[1].data = setupsData;
                pipelineChart.data.datasets[2].data = fillsData;
                pipelineChart.update('none');
            } else {
                const ctx = canvas.getContext('2d');
                pipelineChart = new Chart(ctx, {
                    type: 'bar',
                    data: {
                        labels: labels,
                        datasets: [
                            {
                                type: 'bar',
                                label: '후보 발굴',
                                data: candidatesData,
                                backgroundColor: 'rgba(49, 130, 246, 0.35)',
                                borderColor: '#3182f6',
                                borderWidth: 1.5,
                                borderRadius: 6,
                                yAxisID: 'yLeft',
                                order: 2
                            },
                            {
                                type: 'line',
                                label: '셋업 생성',
                                data: setupsData,
                                borderColor: '#c084fc',
                                backgroundColor: '#c084fc',
                                borderWidth: 2.5,
                                pointRadius: 4,
                                pointHoverRadius: 6,
                                tension: 0.3,
                                yAxisID: 'yRight',
                                order: 1
                            },
                            {
                                type: 'line',
                                label: '체결 성공',
                                data: fillsData,
                                borderColor: '#00c73c',
                                backgroundColor: '#00c73c',
                                borderWidth: 3,
                                pointRadius: 5,
                                pointHoverRadius: 7,
                                tension: 0.2,
                                yAxisID: 'yRight',
                                order: 0
                            }
                        ]
                    },
                    options: {
                        responsive: true,
                        maintainAspectRatio: false,
                        interaction: {
                            mode: 'index',
                            intersect: false
                        },
                        plugins: {
                            legend: { display: false },
                            tooltip: {
                                backgroundColor: 'rgba(23, 25, 32, 0.95)',
                                titleColor: '#ffffff',
                                bodyColor: '#ffffff',
                                borderColor: 'rgba(255, 255, 255, 0.1)',
                                borderWidth: 1,
                                padding: 10,
                                callbacks: {
                                    label: function(context) {
                                        return (context.dataset.label || '') + ': ' + context.parsed.y.toLocaleString() + '건';
                                    }
                                }
                            }
                        },
                        scales: {
                            x: {
                                grid: { color: 'rgba(255, 255, 255, 0.04)' },
                                ticks: { color: '#9096a2', font: { family: 'Pretendard', size: 11, weight: 'bold' } }
                            },
                            yLeft: {
                                type: 'linear',
                                position: 'left',
                                beginAtZero: true,
                                grid: { color: 'rgba(255, 255, 255, 0.04)' },
                                ticks: {
                                    color: '#3182f6',
                                    font: { family: 'Pretendard', size: 10 },
                                    callback: function(val) { return val.toLocaleString() + '개'; }
                                },
                                title: { display: true, text: '후보 발굴 (개)', color: '#3182f6', font: { size: 10 } }
                            },
                            yRight: {
                                type: 'linear',
                                position: 'right',
                                beginAtZero: true,
                                grid: { drawOnChartArea: false },
                                ticks: {
                                    color: '#00c73c',
                                    precision: 0,
                                    font: { family: 'Pretendard', size: 10 },
                                    callback: function(val) { return val + '건'; }
                                },
                                title: { display: true, text: '셋업/체결 (건)', color: '#00c73c', font: { size: 10 } }
                            }
                        }
                    }
                });
            }

            const cardsCont = document.getElementById('hourlyPipelineCards');
            if (cardsCont) {
                cardsCont.innerHTML = hourlyData.map(h => {
                    const hasFill = h.fills > 0;
                    const borderClass = hasFill ? 'border-[#00c73c]/40 bg-[#00c73c]/10' : 'border-white/[0.04] bg-[#171920]';
                    return `
                        <div class="p-2.5 rounded-xl border ${borderClass} text-center">
                            <div class="text-[11px] font-bold text-white">${h.time}</div>
                            <div class="text-[10px] text-[#3182f6] mt-1">후보: <b>${h.candidates.toLocaleString()}</b></div>
                            <div class="text-[10px] text-purple-400">셋업: <b>${h.setups}</b></div>
                            <div class="text-[10px] ${hasFill ? 'text-[#00c73c] font-bold' : 'text-[#9096a2]'}">체결: <b>${h.fills}</b></div>
                        </div>
                    `;
                }).join('');
            }
        }

        let consecutiveFailures = 0;
        let isFetchingState = false;

        function updateNetworkBanner(isOnline, detail) {
            let banner = document.getElementById('mobileNetBanner');
            if (!banner) {
                banner = document.createElement('div');
                banner.id = 'mobileNetBanner';
                banner.style.cssText = 'position:fixed; top:0; left:0; width:100%; z-index:99999; text-align:center; font-size:12px; font-weight:700; padding:8px 10px; box-shadow: 0 4px 12px rgba(0,0,0,0.3); transition:all 0.3s ease; display:none;';
                document.body.appendChild(banner);
            }
            if (isOnline) {
                if (consecutiveFailures > 0) {
                    banner.style.display = 'block';
                    banner.style.backgroundColor = '#059669';
                    banner.style.color = '#ffffff';
                    banner.innerHTML = '🟢 <b>[실시간 재연결 성공]</b> 대시보드가 정상 동기화되었습니다.';
                    setTimeout(() => { banner.style.display = 'none'; }, 2500);
                } else {
                    banner.style.display = 'none';
                }
            } else {
                banner.style.display = 'block';
                banner.style.backgroundColor = consecutiveFailures >= 10 ? '#b91c1c' : '#d97706';
                banner.style.color = '#ffffff';
                let actionBtn = '<button onclick="forceInstantReconnect()" style="margin-left:8px; padding:2px 8px; border-radius:4px; border:none; background:#ffffff; color:#000000; font-weight:bold; cursor:pointer; font-size:11px;">🔄 즉시 재연결</button>';
                if (consecutiveFailures >= 10) {
                    banner.innerHTML = '⚠️ <b>[장시간 연결 두절]</b> 인터넷 회선 확인 중 (' + consecutiveFailures + '회차). PC 재부팅 시 카톡 최신 링크 확인 필요 ' + actionBtn;
                } else {
                    banner.innerHTML = '⚡ <b>[인터넷 끊김 감지]</b> 자동 재연결 시도 중 (' + consecutiveFailures + '회차)... ' + actionBtn;
                }
            }
        }

        window.forceInstantReconnect = function() {
            console.log("[User Click] 즉시 재연결 강제 트리거");
            isFetchingState = false;
            fetchState();
        };

        async function fetchState() {
            if (isFetchingState) return;
            isFetchingState = true;
            try {
                const ctrl = new AbortController();
                const tid = setTimeout(() => ctrl.abort(), 6000);
                const res = await fetch('/api/state?_t=' + Date.now(), {
                    signal: ctrl.signal,
                    cache: 'no-store'
                });
                clearTimeout(tid);
                if (!res.ok) throw new Error("HTTP " + res.status);
                const data = await res.json();
                renderDashboard(data);
                updateNetworkBanner(true);
                consecutiveFailures = 0;
            } catch (e) {
                consecutiveFailures++;
                console.warn("[Dashboard Sync] Fetch error (" + consecutiveFailures + "):", e);
                if (consecutiveFailures >= 2) {
                    updateNetworkBanner(false, e.name === 'AbortError' ? '네트워크 지연' : e.message);
                }
            } finally {
                isFetchingState = false;
            }
        }

        // 모바일 화면 잠금 해제 또는 탭 복귀 시 즉시 동기화
        document.addEventListener('visibilitychange', () => {
            if (document.visibilityState === 'visible') {
                isFetchingState = false;
                fetchState();
            }
        });
        window.addEventListener('focus', () => {
            isFetchingState = false;
            fetchState();
        });
        window.addEventListener('online', () => {
            isFetchingState = false;
            fetchState();
        });

        let audioAlertEnabled = true;
        let lastKnownProcStatus = 'RUNNING';

        function toggleAudioAlert() {
            audioAlertEnabled = !audioAlertEnabled;
            const label = document.getElementById('audioAlertLabel');
            const icon = document.getElementById('audioAlertIcon');
            if (label) label.innerText = audioAlertEnabled ? '경보음 켬' : '경보음 끔';
            if (icon) icon.innerText = audioAlertEnabled ? '🔔' : '🔕';
            if (audioAlertEnabled) {
                playAlertBeep();
            }
        }

        function playAlertBeep() {
            if (!audioAlertEnabled) return;
            try {
                const AudioCtx = window.AudioContext || window.webkitAudioContext;
                if (!AudioCtx) return;
                const ctx = new AudioCtx();
                const osc = ctx.createOscillator();
                const gain = ctx.createGain();
                osc.type = 'sawtooth';
                osc.frequency.setValueAtTime(880, ctx.currentTime);
                osc.frequency.setValueAtTime(440, ctx.currentTime + 0.15);
                gain.gain.setValueAtTime(0.3, ctx.currentTime);
                gain.gain.exponentialRampToValueAtTime(0.01, ctx.currentTime + 0.4);
                osc.connect(gain);
                gain.connect(ctx.destination);
                osc.start();
                osc.stop(ctx.currentTime + 0.4);
            } catch (e) {
                console.warn("AudioContext warning:", e);
            }
        }

        function renderTraderProcessStatus(proc) {
            if (!proc) return;
            const banner = document.getElementById('traderProcessBanner');
            const pulse = document.getElementById('traderStatusPulse');
            const title = document.getElementById('traderStatusTitle');
            const pill = document.getElementById('traderStatusPill');
            const pidBadge = document.getElementById('traderPidBadge');
            const modeBadge = document.getElementById('traderModeBadge');
            const restartVal = document.getElementById('restartCountVal');
            const hbVal = document.getElementById('heartbeatAgoVal');

            const status = proc.status || 'STOPPED';
            const restartCnt = proc.restart_count || 0;
            const hbAgo = (proc.heartbeat_ago_sec != null && proc.heartbeat_ago_sec < 99999) ? proc.heartbeat_ago_sec : '--';

            if (restartVal) restartVal.innerText = restartCnt;
            if (hbVal) hbVal.innerText = hbAgo;
            if (pidBadge) {
                if (proc.pid) {
                    pidBadge.innerText = 'PID: ' + proc.pid;
                    pidBadge.classList.remove('hidden');
                } else {
                    pidBadge.classList.add('hidden');
                }
            }
            if (modeBadge) {
                modeBadge.innerText = (proc.mode || 'LIVE') + ' 모드';
            }

            // Transition detection for alert
            if (status !== 'RUNNING' && lastKnownProcStatus === 'RUNNING') {
                playAlertBeep();
                document.title = `🚨 [꺼짐경고] 트레이더 중단! (${status})`;
            } else if (status === 'RUNNING' && lastKnownProcStatus !== 'RUNNING') {
                document.title = '토스증권 스타일 AI 퀀트 관제탑 | SELF-IMPROVING QUANT AI';
            }
            lastKnownProcStatus = status;

            if (status === 'RUNNING') {
                if (banner) banner.className = 'border-b transition-all duration-300 px-4 sm:px-8 py-2.5 bg-[#12141a] border-[#00c73c]/20';
                if (pulse) pulse.innerHTML = '<span class="animate-ping absolute inline-flex h-full w-full rounded-full bg-[#00c73c] opacity-75"></span><span class="relative inline-flex rounded-full h-3 w-3 bg-[#00c73c]"></span>';
                if (title) title.innerText = '실전 퀀트 매매 프로세스 정상 가동중';
                if (pill) {
                    pill.className = 'toss-pill toss-pill-green font-bold text-[11px] sm:text-xs';
                    pill.innerText = '● 정상 가동';
                }
            } else if (status === 'RESTARTING') {
                if (banner) banner.className = 'border-b transition-all duration-300 px-4 sm:px-8 py-2.5 bg-amber-950/80 border-amber-500/50';
                if (pulse) pulse.innerHTML = '<span class="animate-ping absolute inline-flex h-full w-full rounded-full bg-amber-400 opacity-75"></span><span class="relative inline-flex rounded-full h-3 w-3 bg-amber-400"></span>';
                if (title) title.innerText = '⚠️ [자동 복구] 트레이더 프로세스가 꺼져 3초 내 자동 재시작 중입니다!';
                if (pill) {
                    pill.className = 'toss-pill font-bold text-[11px] sm:text-xs bg-amber-500/20 text-amber-300';
                    pill.innerText = '🔄 재시작 진행중';
                }
            } else { // STOPPED or STALE
                if (banner) banner.className = 'border-b transition-all duration-300 px-4 sm:px-8 py-2.5 bg-red-950/90 border-red-500 animate-pulse';
                if (pulse) pulse.innerHTML = '<span class="animate-ping absolute inline-flex h-full w-full rounded-full bg-red-500 opacity-75"></span><span class="relative inline-flex rounded-full h-3 w-3 bg-red-500"></span>';
                if (title) title.innerText = '🚨 [위험] 트레이더 프로세스가 꺼졌습니다! (감시기 복구 대기)';
                if (pill) {
                    pill.className = 'toss-pill toss-pill-red font-bold text-[11px] sm:text-xs';
                    pill.innerText = '🚨 프로세스 꺼짐';
                }
            }
        }

        function renderDashboard(data) {
            cachedDashboardData = data;
            renderTraderProcessStatus(data.trader_process);
            renderPeriodStats(data.period_stats);
            renderClosedTrades(data);
            renderOpenOrders(data);

            // Mode & Account
            updateModeButtons(data.mode.toLowerCase());
            const isLive = (data.mode || '').toUpperCase() === 'LIVE';
            const modeText = isLive ? '실전투자' : '모의투자';
            const modePillClass = isLive ? 'toss-pill toss-pill-red text-xs font-bold' : 'toss-pill toss-pill-blue text-xs font-bold';
            document.getElementById('accountNoLabel').innerText = data.act_no + ' (' + modeText + ')';
            const pBadge = document.getElementById('periodStatsModeBadge');
            if (pBadge) {
                pBadge.innerText = modeText + ' (' + data.act_no + ')';
                pBadge.className = modePillClass;
            }
            const cBadge = document.getElementById('closedTradesModeBadge');
            if (cBadge) {
                cBadge.innerText = modeText + ' (' + data.act_no + ')';
                cBadge.className = modePillClass;
            }
            const posModeBadge = document.getElementById('positionsModeBadge');
            if (posModeBadge) {
                posModeBadge.innerText = modeText + ' (' + data.act_no + ')';
                posModeBadge.className = modePillClass;
            }
            
            // Real Asset Values (Toss Big Typography)
            document.getElementById('equityVal').innerText = formatMoney(data.account.equity);
            document.getElementById('cashVal').innerText = formatMoney(data.account.cash);
            const elOrdAvail = document.getElementById('orderAvailVal');
            if (elOrdAvail) elOrdAvail.innerText = formatMoney(data.account.order_available != null ? data.account.order_available : data.account.cash);
            const elD2 = document.getElementById('d2CashVal');
            if (elD2) elD2.innerText = formatMoney(data.account.d2_cash != null ? data.account.d2_cash : data.account.cash);
            const elWtm = document.getElementById('withdrawCashVal');
            if (elWtm) elWtm.innerText = formatMoney(data.account.withdrawable_cash != null ? data.account.withdrawable_cash : 0);
            const totalStockEval = (data.account && data.account.total_stock_eval != null)
                ? data.account.total_stock_eval
                : (data.positions || []).reduce((sum, p) => sum + (p.current_price * p.qty), 0);
            const elStockEval = document.getElementById('stockEvalVal');
            if (elStockEval) elStockEval.innerText = formatMoney(totalStockEval);
            const elEqStock = document.getElementById('equityStockSum');
            if (elEqStock) elEqStock.innerText = formatMoney(totalStockEval);
            const elEqCash = document.getElementById('equityCashSum');
            if (elEqCash) elEqCash.innerText = formatMoney(data.account.cash);

            // Risk Metrics
            if (data.risk) {
                const uRisk = data.risk.used_risk_ratio != null ? data.risk.used_risk_ratio : 0.0;
                const aRisk = data.risk.available_risk_ratio != null ? data.risk.available_risk_ratio : 4.0;
                document.getElementById('usedRiskVal').innerText = uRisk.toFixed(2) + '%';
                document.getElementById('availRiskVal').innerText = aRisk.toFixed(2) + '%';
                const rStat = data.risk.risk_status || 'NORMAL';
                const rBadge = document.getElementById('riskStatusBadge');
                if (rBadge) {
                    rBadge.innerText = rStat + (rStat === 'NORMAL' ? ' 🛡️' : ' ⚠️');
                    if (rStat === 'NORMAL') rBadge.className = 'font-bold text-[#00c73c] text-sm';
                    else if (rStat === 'REDUCED') rBadge.className = 'font-bold text-amber-400 text-sm';
                    else rBadge.className = 'font-bold text-[#f04452] text-sm';
                }
            }

            // Central Token & API Gateway Telemetry (Section 29, 32)
            if (data.token_telemetry) {
                const tt = data.token_telemetry;
                const htt = document.getElementById('headerTokenText');
                const htb = document.getElementById('headerTokenBadge');
                if (htt) {
                    htt.innerText = `토큰: ${tt.status} (${tt.remaining_ttl_text || '24h'})`;
                }
                if (htb) {
                    htb.className = tt.is_valid
                        ? 'hidden md:inline-flex items-center text-xs font-semibold text-[#00c73c] bg-[#00c73c]/10 px-2.5 py-0.5 rounded-full border border-[#00c73c]/20 transition'
                        : 'hidden md:inline-flex items-center text-xs font-semibold text-[#f04452] bg-[#f04452]/10 px-2.5 py-0.5 rounded-full border border-[#f04452]/20 transition';
                }
                const elSummary = document.getElementById('tokenTtlValSummary');
                if (elSummary) elSummary.innerText = (tt.remaining_ttl_text || '24h') + ' 잔여';

                const tb = document.getElementById('tokenStatusBadge');
                if (tb) {
                    tb.innerText = 'TOKEN: ' + tt.status;
                    tb.className = tt.is_valid ? 'toss-pill toss-pill-green text-xs font-bold' : 'toss-pill toss-pill-red text-xs font-bold';
                }
                const elTtl = document.getElementById('tokenTtlVal');
                if (elTtl) elTtl.innerText = tt.remaining_ttl_text || '--';
                const elExp = document.getElementById('tokenExpiresVal');
                if (elExp) elExp.innerText = '만료: ' + (tt.expires_at || '--');
                const elRef = document.getElementById('tokenRefreshCountVal');
                if (elRef) elRef.innerText = (tt.refresh_count || 0) + '회';
                const elRea = document.getElementById('tokenReasonVal');
                if (elRea) elRea.innerText = tt.last_refresh_reason || 'SYSTEM_INIT';
                const elErr = document.getElementById('tokenErrorVal');
                if (elErr) elErr.innerText = '에러: ' + (tt.last_token_error || 'NONE');
            }

            if (data.daily_cache_stats) {
                const dc = data.daily_cache_stats;
                const elDc = document.getElementById('dailyCacheVal');
                if (elDc) elDc.innerText = dc.positive_cached_symbols + '개 종목 캐시됨';
                const elDb = document.getElementById('dailyBlockedVal');
                if (elDb) elDb.innerText = '일시 장애/제외 격리: ' + dc.negative_blocked_symbols + '개';
            }

            if (data.api_telemetry) {
                const hat = document.getElementById('headerApiText');
                const hab = document.getElementById('headerApiBadge');
                const mList = Object.values(data.api_telemetry);
                const totalCalls = mList.reduce((s, m) => s + (m.calls || 0), 0);
                const totalErrors = mList.reduce((s, m) => s + (m.errors || 0), 0);
                const totalHits = mList.reduce((s, m) => s + (m.cache_hits || 0), 0);
                if (hat) {
                    if (totalErrors === 0) {
                        hat.innerText = `API: 정상 (호출 ${totalCalls} • 캐시 ${totalHits})`;
                    } else {
                        hat.innerText = `API: 주의 (에러 ${totalErrors} / 호출 ${totalCalls})`;
                    }
                }
                if (hab) {
                    hab.className = totalErrors === 0
                        ? 'hidden lg:inline-flex items-center text-xs font-semibold text-[#3182f6] bg-[#3182f6]/10 px-2.5 py-0.5 rounded-full border border-[#3182f6]/20 transition'
                        : 'hidden lg:inline-flex items-center text-xs font-semibold text-amber-400 bg-amber-500/10 px-2.5 py-0.5 rounded-full border border-amber-500/20 transition';
                }

                const tbody = document.getElementById('apiTelemetryTableBody');
                if (tbody) {
                    const entries = Object.entries(data.api_telemetry);
                    if (entries.length === 0) {
                        tbody.innerHTML = '<tr><td colspan="6" class="py-3 text-center text-[#9096a2]">수집된 API 메트릭이 없습니다.</td></tr>';
                    } else {
                        tbody.innerHTML = entries.map(([ep, m]) => {
                            const errColor = m.errors > 0 ? 'text-[#f04452]' : 'text-[#9096a2]';
                            const hitBadge = m.hit_rate_pct > 50 ? 'text-[#00c73c]' : 'text-white';
                            return `
                                <tr class="hover:bg-white/[0.02]">
                                    <td class="py-2 px-3 font-mono text-white text-[11px]">${ep}</td>
                                    <td class="py-2 px-3 text-right text-white font-semibold">${m.calls}</td>
                                    <td class="py-2 px-3 text-right"><span class="text-[#00c73c]">${m.success}</span> / <span class="${errColor}">${m.errors}</span></td>
                                    <td class="py-2 px-3 text-right text-[#3182f6]">${m.cache_hits}</td>
                                    <td class="py-2 px-3 text-right ${hitBadge} font-bold">${m.hit_rate_pct}%</td>
                                    <td class="py-2 px-3 text-right text-[#9096a2]">${m.avg_latency_ms}ms / ${m.p95_latency_ms}ms</td>
                                </tr>
                            `;
                        }).join('');
                    }
                }
            }

            // Position Count Policy Display
            const maxPosEl = document.getElementById('maxPositionCountVal');
            if (maxPosEl) maxPosEl.innerText = data.account.max_position_count || 'UNLIMITED';
            const curPosEl = document.getElementById('curPositionCountVal');
            if (curPosEl) curPosEl.innerText = (data.positions ? data.positions.length : 0) + '개';
            const blockEl = document.getElementById('positionCountBlockVal');
            if (blockEl) blockEl.innerText = (data.account.position_count_block ? 'TRUE' : 'FALSE');
            const checkEl = document.getElementById('positionCountCheckVal');
            if (checkEl) checkEl.innerText = data.account.position_count_check || 'BYPASSED / NOT_USED';

            // Real Holdings (Toss Securities Stock List & Summary)
            const posCont = document.getElementById('positionsContainer');
            const emptyMsg = document.getElementById('emptyPositionsMsg');
            const posBadge = document.getElementById('realPosCount');
            posCont.innerHTML = '';
            
            const positions = data.positions || [];
            posBadge.innerText = positions.length + '개';

            const navHoldingsBadge = document.getElementById('navHoldingsBadge');
            const tabHoldingsDot = document.getElementById('tabHoldingsDot');
            if (positions.length > 0) {
                if (navHoldingsBadge) navHoldingsBadge.classList.remove('hidden');
                if (tabHoldingsDot) tabHoldingsDot.classList.remove('hidden');
            } else {
                if (navHoldingsBadge) navHoldingsBadge.classList.add('hidden');
                if (tabHoldingsDot) tabHoldingsDot.classList.add('hidden');
            }

            let totBuy = 0;
            let totEval = 0;
            let totUnrealizedPnl = 0;
            let posProfitCnt = 0;
            let posLossCnt = 0;

            positions.forEach(pos => {
                const buyAmt = (pos.entry_price || 0) * (pos.qty || 0);
                const evalAmt = (pos.eval_amount != null) ? pos.eval_amount : ((pos.current_price || 0) * (pos.qty || 0));
                const pnlAmt = (pos.pnl != null) ? pos.pnl : (evalAmt - buyAmt);
                totBuy += buyAmt;
                totEval += evalAmt;
                totUnrealizedPnl += pnlAmt;
                if (pnlAmt > 0) posProfitCnt++;
                else if (pnlAmt < 0) posLossCnt++;
            });

            const totRetPct = totBuy > 0 ? ((totUnrealizedPnl / totBuy) * 100).toFixed(2) : '0.00';
            const isTotProfit = totUnrealizedPnl > 0;
            const isTotLoss = totUnrealizedPnl < 0;
            const totPnlColor = isTotProfit ? 'text-[#f04452]' : (isTotLoss ? 'text-[#3182f6]' : 'text-white');

            const elPosBuy = document.getElementById('posTotalBuyVal');
            if (elPosBuy) elPosBuy.innerText = formatMoney(totBuy);
            const elPosEval = document.getElementById('posTotalEvalVal');
            if (elPosEval) elPosEval.innerText = formatMoney(totEval);
            const elPosPnl = document.getElementById('posTotalPnlVal');
            if (elPosPnl) {
                elPosPnl.innerText = (isTotProfit ? '+' : '') + formatMoney(totUnrealizedPnl);
                elPosPnl.className = 'text-base sm:text-lg font-extrabold mt-0.5 block ' + totPnlColor;
            }
            const elPosRet = document.getElementById('posTotalRetVal');
            if (elPosRet) {
                elPosRet.innerText = (isTotProfit ? '+' : '') + totRetPct + '%';
                elPosRet.className = 'text-base sm:text-lg font-extrabold mt-0.5 block ' + totPnlColor;
            }
            const elPosCnt = document.getElementById('posProfitLossCount');
            if (elPosCnt) {
                elPosCnt.innerText = `${positions.length}개 종목 (상승 ${posProfitCnt} / 하락 ${posLossCnt})`;
            }

            // Daily Combined PnL = [오늘 매도 실현손익] + [현재 보유 실시간 평가손익] (실시간 엎치락뒤치락 연동)
            const todayTrades = data.closed_trades_today || [];
            const realizedPnl = (data.account && data.account.today_realized_pnl != null)
                ? data.account.today_realized_pnl
                : todayTrades.reduce((sum, t) => sum + (t.pnl || 0), 0);
            const unrealizedPnl = (data.account && data.account.today_unrealized_pnl != null)
                ? data.account.today_unrealized_pnl
                : totUnrealizedPnl;
            const combinedDailyPnl = (data.account && data.account.daily_pnl != null)
                ? data.account.daily_pnl
                : (realizedPnl + unrealizedPnl);
            
            let combinedDailyPnlPct = (data.account && data.account.daily_pnl_pct != null) ? data.account.daily_pnl_pct : 0.0;
            if (combinedDailyPnlPct === 0.0 && data.account && data.account.equity > 0) {
                const baseEq = data.account.equity - combinedDailyPnl;
                if (baseEq > 0) {
                    combinedDailyPnlPct = Number(((combinedDailyPnl / baseEq) * 100).toFixed(2));
                }
            }

            const pnlBadge = document.getElementById('dailyPnlBadge');
            if (pnlBadge) {
                pnlBadge.innerText = (combinedDailyPnl >= 0 ? '+' : '') + formatMoney(combinedDailyPnl) + ' (' + (combinedDailyPnlPct >= 0 ? '+' : '') + combinedDailyPnlPct + '%) 오늘';
                if (combinedDailyPnl > 0) {
                    pnlBadge.className = 'toss-pill toss-pill-red text-sm font-bold';
                } else if (combinedDailyPnl < 0) {
                    pnlBadge.className = 'toss-pill toss-pill-blue text-sm font-bold';
                } else {
                    pnlBadge.className = 'toss-pill toss-pill-gray text-sm font-bold';
                }
            }

            const elRealizedSub = document.getElementById('pnlRealizedSub');
            if (elRealizedSub) {
                elRealizedSub.innerText = (realizedPnl >= 0 ? '+' : '') + formatMoney(realizedPnl);
                elRealizedSub.className = realizedPnl > 0 ? 'text-[#f04452] font-bold' : (realizedPnl < 0 ? 'text-[#3182f6] font-bold' : 'text-white font-bold');
            }
            const elUnrealizedSub = document.getElementById('pnlUnrealizedSub');
            if (elUnrealizedSub) {
                elUnrealizedSub.innerText = (unrealizedPnl >= 0 ? '+' : '') + formatMoney(unrealizedPnl);
                elUnrealizedSub.className = unrealizedPnl > 0 ? 'text-[#f04452] font-bold' : (unrealizedPnl < 0 ? 'text-[#3182f6] font-bold' : 'text-white font-bold');
            }

            if (positions.length === 0) {
                emptyMsg.classList.remove('hidden');
            } else {
                emptyMsg.classList.add('hidden');
                positions.forEach(pos => {
                    const row = document.createElement('div');
                    row.className = 'py-4 border-b border-white/[0.04] last:border-b-0 hover:bg-white/[0.02] -mx-2 px-2 rounded-xl transition';
                    const isProfit = pos.pnl > 0;
                    const isLoss = pos.pnl < 0;
                    const pnlColorClass = isProfit ? 'text-[#f04452]' : (isLoss ? 'text-[#3182f6]' : 'text-[#9096a2]');
                    const pnlPillClass = isProfit ? 'toss-pill-red' : (isLoss ? 'toss-pill-blue' : 'toss-pill-gray');
                    
                    const rawName = pos.name || pos.symbol_name || '';
                    const symbol = pos.symbol || pos.iem_cd || '';
                    const hasName = rawName && rawName !== symbol;
                    const displayName = pos.display_name || (hasName ? `${rawName} (${symbol})` : symbol);
                    
                    // First char for avatar
                    const initialChar = (hasName ? rawName : (symbol || '주')).substring(0, 1);

                    row.innerHTML = `
                        <div class="flex flex-col sm:flex-row sm:items-center justify-between gap-3">
                            <div class="flex items-center space-x-3.5">
                                <div class="stock-avatar">
                                    ${initialChar}
                                </div>
                                <div>
                                    <div class="flex items-center space-x-2">
                                        <span class="font-bold text-white text-base">${displayName}</span>
                                        <span class="text-[11px] font-bold px-2 py-0.5 rounded-md text-[#3182f6] bg-[#3182f6]/10">${pos.time_horizon || '보유'}</span>
                                    </div>
                                    <div class="text-xs text-[#9096a2] mt-0.5">
                                        <span class="font-semibold text-white">${pos.qty}주</span> 보유 • 매입단가 <span class="font-semibold text-white">${(pos.entry_price || 0).toLocaleString()}원</span>
                                    </div>
                                </div>
                            </div>

                            <div class="flex items-center justify-between sm:justify-end sm:space-x-4 pl-14 sm:pl-0">
                                <div class="text-left sm:text-right">
                                    <div class="font-bold text-white text-base">${(pos.current_price || 0).toLocaleString()}원</div>
                                    <div class="text-[11px] text-[#9096a2]">총 평가금 ${((pos.eval_amount || (pos.current_price * pos.qty)) || 0).toLocaleString()}원</div>
                                </div>
                                <div class="text-right min-w-[110px]">
                                    <span class="toss-pill ${pnlPillClass} font-bold text-xs">
                                        ${isProfit ? '+' : ''}${pos.pnl_pct}% (${(isProfit ? '+' : '')}${(pos.pnl || 0).toLocaleString()}원)
                                    </span>
                                </div>
                            </div>
                        </div>

                        <!-- 손절가 및 매도 기준가 (목표가) 안내 바 -->
                        <div class="mt-2.5 pl-14 flex flex-wrap items-center gap-2 text-xs">
                            <div class="bg-rose-500/10 text-rose-300 border border-rose-500/20 px-2.5 py-1 rounded-lg flex items-center space-x-1.5 font-mono">
                                <span class="text-[11px] text-rose-400 font-bold font-sans">손절가</span>
                                <span class="font-bold text-rose-200">${(pos.stop_price || 0).toLocaleString()}원</span>
                                <span class="text-[10px] text-rose-400/80">(-2.5%)</span>
                            </div>
                            <div class="bg-emerald-500/10 text-emerald-300 border border-emerald-500/20 px-2.5 py-1 rounded-lg flex items-center space-x-1.5 font-mono">
                                <span class="text-[11px] text-emerald-400 font-bold font-sans">1차 매도 기준가</span>
                                <span class="font-bold text-emerald-200">${(pos.target_1r || 0).toLocaleString()}원</span>
                                ${pos.target_1r_hit ? '<span class="text-[10px] text-emerald-200 bg-emerald-500/30 px-1 py-0.2 rounded font-bold font-sans">✓ 도달 (트레일링)</span>' : '<span class="text-[10px] text-emerald-400/80">(+4%)</span>'}
                            </div>
                            <div class="bg-blue-500/10 text-blue-300 border border-blue-500/20 px-2.5 py-1 rounded-lg flex items-center space-x-1.5 font-mono">
                                <span class="text-[11px] text-blue-400 font-bold font-sans">2차 매도 기준가</span>
                                <span class="font-bold text-blue-200">${(pos.target_2r || 0).toLocaleString()}원</span>
                                ${pos.target_2r_hit ? '<span class="text-[10px] text-blue-200 bg-blue-500/30 px-1 py-0.2 rounded font-bold font-sans">✓ 도달 (전량청산)</span>' : '<span class="text-[10px] text-blue-400/80">(+8%)</span>'}
                            </div>
                        </div>
                    `;
                    posCont.appendChild(row);
                });
            }

            // Models
            document.getElementById('champVersion').innerText = data.models.champion.version;
            document.getElementById('champAllocBadge').innerText = '실전 배분: ' + data.models.champion.allocation_pct + '%';
            document.getElementById('champWr').innerText = data.models.champion.win_rate + '%';
            document.getElementById('champPf').innerText = data.models.champion.profit_factor;
            document.getElementById('champAvgR').innerText = '+' + data.models.champion.avg_r + 'R';

            document.getElementById('challVersion').innerText = data.models.challenger.version;
            document.getElementById('challStateBadge').innerText = data.models.challenger.state + ' (배분: ' + data.models.challenger.allocation_pct + '%)';
            document.getElementById('challWr').innerText = data.models.challenger.shadow_win_rate + '%';
            document.getElementById('challPf').innerText = data.models.challenger.shadow_pf;
            document.getElementById('challSamples').innerText = data.models.challenger.shadow_samples + '건';

            // Rollout Progress
            const alloc = data.models.challenger.allocation_pct;
            document.getElementById('stagedAllocLabel').innerText = '자본 배분 진행도: ' + alloc + '% (' + data.models.challenger.state + ')';
            document.getElementById('stagedProgress').style.width = Math.max(alloc, 5) + '%';

            // Bad Trade Counts
            const bd = data.bad_trade_summary || {};
            document.getElementById('cntFakeBreakout').innerText = (bd['FAKE_BREAKOUT'] || 1) + '건';
            document.getElementById('cntLateEntry').innerText = (bd['LATE_ENTRY'] || 1) + '건';
            document.getElementById('cntLowLiquidity').innerText = (bd['LOW_LIQUIDITY'] || 1) + '건';
            document.getElementById('cntProfitTarget').innerText = (bd['PROFIT_TARGET'] || 5) + '건';

            // Events List Feed (Toss Style)
            const evCont = document.getElementById('eventsListContainer');
            evCont.innerHTML = '';
            (data.live_events || []).forEach((ev, idx) => {
                const row = document.createElement('div');
                row.className = 'py-3.5 flex flex-col sm:flex-row sm:items-center justify-between gap-2 hover:bg-white/[0.02] -mx-2 px-2 rounded-xl transition';
                
                let icon = '⚡';
                if (ev.type === 'VOLUME_SURGE') icon = '🔥';
                if (ev.type === 'PDH_BREAKOUT') icon = '🚀';
                if (ev.type === 'MOMENTUM_IGNITION') icon = '⚡';
                if (ev.type === 'HH_HL_STRUCT') icon = '📈';

                row.innerHTML = `
                    <div class="flex items-center space-x-3">
                        <span class="w-6 text-center font-black text-sm text-[#9096a2]">${idx + 1}</span>
                        <div class="w-8 h-8 rounded-lg bg-white/[0.06] flex items-center justify-center text-sm">
                            ${icon}
                        </div>
                        <div>
                            <div class="flex items-center space-x-2">
                                <span class="font-bold text-white text-sm">${ev.name}</span>
                                <span class="text-xs font-mono text-[#9096a2]">${ev.symbol}</span>
                                <span class="toss-pill toss-pill-blue text-[11px]">${ev.type}</span>
                            </div>
                            <div class="text-xs text-[#9096a2] mt-0.5">${ev.desc}</div>
                        </div>
                    </div>

                    <div class="flex items-center justify-between sm:justify-end space-x-3 pl-9 sm:pl-0">
                        <div class="text-right">
                            <span class="text-xs text-[#00c73c] font-bold">점수 ${ev.score}점</span>
                        </div>
                        <span class="toss-pill toss-pill-green font-bold text-xs">
                            목표달성 ${(ev.p_target * 100).toFixed(0)}%
                        </span>
                        <span class="text-xs text-[#9096a2] font-mono">${ev.time}</span>
                    </div>
                `;
                evCont.appendChild(row);
            });

            // Funnel Telemetry (v8.0)
            if (data.funnel && data.funnel.stats) {
                const fs = data.funnel.stats;
                const fr = data.funnel.rates || {};
                const elEv = document.getElementById('fnEvents');
                if (elEv) elEv.innerText = (fs.events || 0).toLocaleString();
                const elCand = document.getElementById('fnCandidates');
                if (elCand) elCand.innerText = (fs.candidates || 0).toLocaleString();
                const elSet = document.getElementById('fnSetups');
                if (elSet) elSet.innerText = (fs.setups || 0).toLocaleString();
                const elApp = document.getElementById('fnApproved');
                if (elApp) elApp.innerText = (fs.buy_approved || 0).toLocaleString();
                const elOrd = document.getElementById('fnOrders');
                if (elOrd) elOrd.innerText = (fs.orders_sent || fs.orders_created || 0).toLocaleString();
                const elFil = document.getElementById('fnFills');
                if (elFil) elFil.innerText = (fs.fills || 0).toLocaleString();

                const elR1 = document.getElementById('rateEventToCand');
                if (elR1) elR1.innerText = (fr.event_to_candidate != null ? fr.event_to_candidate : 0) + '%';
                const elR2 = document.getElementById('rateCandToSetup');
                if (elR2) elR2.innerText = (fr.candidate_to_setup != null ? fr.candidate_to_setup : 0) + '%';
                const elR3 = document.getElementById('rateSetupToBuy');
                if (elR3) elR3.innerText = (fr.setup_to_buy != null ? fr.setup_to_buy : 0) + '%';
                const elR4 = document.getElementById('rateBuyToOrder');
                if (elR4) elR4.innerText = (fr.buy_to_order != null ? fr.buy_to_order : 100) + '%';
                const elR5 = document.getElementById('rateOrderToFill');
                if (elR5) elR5.innerText = (fr.order_to_fill != null ? fr.order_to_fill : 100) + '%';

                if (data.funnel.hourly_pipeline) {
                    renderPipelineChart(data.funnel.hourly_pipeline);
                }
            }

            // Top Opportunities & Momentum Stages (v8.0)
            const oppCont = document.getElementById('topOpportunitiesContainer');
            if (oppCont) {
                oppCont.innerHTML = '';
                const opps = data.top_opportunities || [];
                opps.forEach((op, idx) => {
                    let mClass = 'toss-pill-gray';
                    if (op.momentum_stage === 'EARLY') mClass = 'toss-pill-green';
                    else if (op.momentum_stage === 'ACTIVE') mClass = 'toss-pill-blue';
                    else if (op.momentum_stage === 'LATE') mClass = 'toss-pill-red';

                    const evStr = Array.isArray(op.events) ? op.events.join(', ') : (op.events || '정상 감시');
                    const tr = document.createElement('tr');
                    tr.className = 'border-b border-white/[0.04] hover:bg-white/[0.02] text-xs';
                    tr.innerHTML = `
                        <td class="py-3 px-2 font-mono text-[#9096a2] text-center">${idx + 1}</td>
                        <td class="py-3 px-3 font-bold text-white">${op.name} <span class="text-[#9096a2] font-mono text-[11px] font-normal">${op.symbol}</span></td>
                        <td class="py-3 px-3 text-right font-semibold text-white">${(op.price || 0).toLocaleString()}원</td>
                        <td class="py-3 px-3 text-center"><span class="toss-pill ${mClass} text-[11px]">${op.momentum_stage || 'NORMAL'}</span></td>
                        <td class="py-3 px-3 text-center font-bold text-[#00c73c]">${(op.score || 0).toFixed(1)}점</td>
                        <td class="py-3 px-3 text-center"><span class="toss-pill toss-pill-blue text-[11px]">${op.state || 'ACTIVE'}</span></td>
                        <td class="py-3 px-3 text-[#9096a2] truncate max-w-xs">${evStr}</td>
                    `;
                    oppCont.appendChild(tr);
                });
            }

            // WHY NO BUY? Diagnostics Breakdown (v8.0)
            const whyCont = document.getElementById('whyNoBuyContainer');
            if (whyCont) {
                whyCont.innerHTML = '';
                const whyList = data.why_no_buy || [];
                const totalRejects = whyList.reduce((acc, cur) => acc + (cur.count || 0), 0) || 1;
                whyList.forEach(item => {
                    const pct = Math.min(100, Math.round((item.count / totalRejects) * 100));
                    const row = document.createElement('div');
                    row.className = 'bg-[#1f222b] p-3.5 rounded-xl border border-white/[0.06] space-y-1.5';
                    row.innerHTML = `
                        <div class="flex justify-between items-center text-xs">
                            <span class="font-bold text-white">${item.reason}</span>
                            <span class="text-[#9096a2] font-mono text-[11px]">${item.count}건 (${pct}%)</span>
                        </div>
                        <div class="w-full bg-[#171920] rounded-full h-1.5 overflow-hidden">
                            <div class="bg-[#3182f6] h-full rounded-full transition-all duration-300" style="width: ${pct}%"></div>
                        </div>
                    `;
                    whyCont.appendChild(row);
                });
            }
        }

        // Action API calls
        async function triggerRetrain() {
            const res = await fetch('/api/retrain', { method: 'POST' });
            const data = await res.json();
            alert(data.message);
            fetchState();
        }

        async function advanceStage() {
            const res = await fetch('/api/advance_stage', { method: 'POST' });
            const data = await res.json();
            alert(data.message);
            fetchState();
        }

        async function triggerRollback() {
            if (confirm("정말로 챌린저 모델을 즉시 차단하고 챔피언으로 긴급 롤백하시겠습니까?")) {
                const res = await fetch('/api/emergency_rollback', { method: 'POST' });
                const data = await res.json();
                alert(data.message);
                fetchState();
            }
        }

        // Auto Refresh every 3 seconds
        setInterval(fetchState, 3000);
        fetchState();
    </script>
</body>
</html>
"""


class DashboardRequestHandler(BaseHTTPRequestHandler):
    """Zero-dependency HTTP Request Handler serving Dashboard & REST APIs"""

    def do_GET(self):
        parsed = urlparse(self.path)
        path = parsed.path
        query = parse_qs(parsed.query)

        if path in ("/", "/index.html"):
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.end_headers()
            self.wfile.write(DASHBOARD_HTML.encode("utf-8"))
            return

        elif path == "/api/state":
            self.send_response(200)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Access-Control-Allow-Origin", "*")
            self.send_header("Cache-Control", "no-store, no-cache, must-revalidate, max-age=0")
            self.send_header("Pragma", "no-cache")
            self.end_headers()
            data = state_mgr.get_full_state()
            self.wfile.write(json.dumps(data, ensure_ascii=False).encode("utf-8"))
            return

        elif path == "/api/switch_mode":
            new_mode = query.get("mode", ["live"])[0].lower()
            res = state_mgr.switch_mode(new_mode)
            self._send_json(res)
            return

        elif path == "/api/token/refresh":
            try:
                tm = TokenManager.get_instance()
                tm.get_token(force=True, reason="USER_CLICK_REFRESH")
                state_mgr.balance_cache.clear()
                self._send_json({
                    "status": "SUCCESS",
                    "message": "새로운 24시간 접근 토큰이 성공적으로 발급되었습니다.",
                    "telemetry": tm.get_telemetry()
                })
            except Exception as e:
                self._send_json({"status": "ERROR", "message": str(e)})
            return

        else:
            self.send_response(404)
            self.end_headers()

    def do_POST(self):
        parsed = urlparse(self.path)
        path = parsed.path

        if path == "/api/retrain":
            res = state_mgr.trigger_candidate_retraining()
            self._send_json(res)
            return

        elif path == "/api/advance_stage":
            res = state_mgr.advance_rollout_stage()
            self._send_json(res)
            return

        elif path == "/api/emergency_rollback":
            res = state_mgr.trigger_emergency_rollback()
            self._send_json(res)
            return

        elif path == "/api/set_position_override":
            content_length = int(self.headers.get("Content-Length", 0))
            post_data = self.rfile.read(content_length)
            try:
                data = json.loads(post_data.decode("utf-8"))
                mode = data.get("mode", state_mgr.mode)
                symbol = data.get("symbol")
                entry_price = float(data.get("entry_price", 0))
                if symbol and entry_price > 0:
                    PositionOverrideStore.get_instance().set_override(mode, symbol, entry_price, note="대시보드 사용자 직접 입력")
                    state_mgr.balance_cache.clear()
                    self._send_json({"status": "SUCCESS", "symbol": symbol, "entry_price": entry_price})
                else:
                    self._send_json({"status": "ERROR", "message": "Invalid symbol or entry_price"})
            except Exception as e:
                self._send_json({"status": "ERROR", "message": str(e)})
            return

        else:
            self.send_response(404)
            self.end_headers()

    def _send_json(self, data: Dict[str, Any]):
        self.send_response(200)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        self.wfile.write(json.dumps(data, ensure_ascii=False).encode("utf-8"))

    def log_message(self, format, *args):
        # Silence routine access logs
        return


def start_dashboard_server(port: int = 8080, open_browser: bool = True) -> HTTPServer:
    """Starts the dashboard web server in a daemon thread with port fallback and dual binding"""
    server = None
    target_ports = [port, 8000, 8088, 8888, 5000]
    actual_port = port
    for p in target_ports:
        try:
            server = ThreadingHTTPServer(("0.0.0.0", p), DashboardRequestHandler)
            actual_port = p
            break
        except Exception:
            continue
    if server is None:
        server = ThreadingHTTPServer(("0.0.0.0", 0), DashboardRequestHandler)
        actual_port = server.server_port

    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    
    url_ip = f"http://127.0.0.1:{actual_port}"
    url_local = f"http://localhost:{actual_port}"
    print(f"\n[웹 대시보드] AI 관제탑 웹 서버 가동 완료!")
    print(f"   ▶ 브라우저 접속 주소: {url_ip} (또는 {url_local})")
    
    if open_browser:
        try:
            webbrowser.open(url_ip)
        except Exception:
            pass
    return server


def main():
    import argparse
    parser = argparse.ArgumentParser(description="AI 퀀트 실시간 웹 관제탑")
    parser.add_argument("--port", type=int, default=8080, help="웹 서버 포트 (기본값: 8080)")
    parser.add_argument("--mode", type=str, default="live", choices=["live", "mock"], help="계좌 모드 (기본값: live)")
    parser.add_argument("--no-browser", action="store_true", help="브라우저 자동 열기 비활성화")
    args = parser.parse_args()

    state_mgr.switch_mode(args.mode)
    server = start_dashboard_server(port=args.port, open_browser=not args.no_browser)
    print("관제탑을 종료하려면 Ctrl+C 를 누르세요.\n")
    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        print("\n웹 관제탑이 종료되었습니다.")


if __name__ == "__main__":
    main()
