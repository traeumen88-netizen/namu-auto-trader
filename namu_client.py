"""나무증권 (NH투자증권 나무 플러그) 전용 API 통신 클라이언트 모듈
- 중앙 집중식 TokenManager, CentralAPIGateway, DailyDataService, BalanceService 연동
- Section 1-40 Architecture 완벽 준수
"""

import os
import time
from datetime import datetime
import logging
from typing import Dict, Any, List, Optional
import nhplug
from nhplug.errors import NhplugError
import config

from core.token_manager import TokenManager
from core.api_gateway import CentralAPIGateway, RequestPriority
from core.daily_data_service import DailyDataService
from core.balance_service import BalanceService

logging.basicConfig(level=logging.INFO, format="[%(asctime)s] %(levelname)s: %(message)s")
logger = logging.getLogger("NamuClient")


class NamuClient:
    QUOTE_BASE_URL = "https://api.nhplug.com:8443"  # 시세 전용 정본 서버

    def __init__(self, mode: str = None, act_no: str = None):
        self.mode = mode or config.TRADING_MODE
        if self.mode == "live":
            self.trade_base_url = "https://api.nhplug.com:8443"
            self.act_no = act_no or os.getenv("ACCOUNT_LIVE", "20201549311")
        else:
            self.trade_base_url = "https://moapi.nhplug.com:8443"
            self.act_no = act_no or os.getenv("ACCOUNT_MOCK", "50001003032")
        self.dry_run = config.DRY_RUN
        
        # 키 설정
        os.environ["NHPLUG_APP_KEY"] = config.APP_KEY
        os.environ["NHPLUG_APP_SECRET"] = config.APP_SECRET
        os.environ["NHPLUG_BASE_URL"] = self.QUOTE_BASE_URL
        os.environ["NHPLUG_SUCCESS_CODES"] = "00000,00166,00221,13578,XA109,00001,00167,00218,00219,00220,00168"

        # 중앙 집중식 서비스 인스턴스 참조 (Section 3-7: 인스턴스 생성 시 토큰 발급 금지)
        self.token_manager = TokenManager.get_instance()
        self.gateway = CentralAPIGateway.get_instance()
        self.daily_service = DailyDataService.get_instance()
        self.balance_service = BalanceService.get_instance()

        mode_label = "실전투자 (LIVE)" if self.mode == "live" else "모의투자 (MOCK)"
        logger.info(f"나무증권 API 클라이언트 초기화 완료 [모드: {mode_label}, 계좌: {self.act_no}]")

    def _safe_call(self, path: str, input_data: dict, target_url: str, retries: int = 3, delay: float = 0.25, timeout: int = 15, priority: RequestPriority = RequestPriority.REALTIME_QUOTE):
        """중앙 APIGateway를 통한 표준 API 호출 (Rate Limit, 토큰 갱신, 지터 백오프 일괄 처리)"""
        return self.gateway.call_api(
            path=path,
            input_data=input_data,
            target_url=target_url,
            priority=priority,
            max_retries=retries,
            timeout=timeout
        )

    def is_unsupported(self, iem_cd: str) -> bool:
        """실제 미지원 종목 여부 확인 (DailyDataService 연동)"""
        hit, data = self.daily_service.is_cached_or_blocked(iem_cd)
        return hit and data == []

    def is_in_transient_cooldown(self, iem_cd: str) -> bool:
        """일시 장애로 인한 쿨다운 상태인지 확인"""
        hit, data = self.daily_service.is_cached_or_blocked(iem_cd)
        return hit and data == []

    def get_current_price(self, iem_cd: str) -> dict:
        """종목 현재가 및 시세 상세 조회"""
        iem_cd = str(iem_cd).strip()
        if not iem_cd or len(iem_cd) != 6 or not iem_cd.isalnum():
            return {
                "iem_cd": iem_cd,
                "name": config.TARGET_STOCKS.get(iem_cd, iem_cd),
                "price": 0, "prev_close": 0, "open": 0, "high": 0, "low": 0,
                "rate": 0.0, "volume": 0, "is_valid": False
            }

        try:
            data = self._safe_call("/krstock/quote/v1/currentPrice", {"iem_cd": iem_cd, "market_cd": "KRX"}, self.QUOTE_BASE_URL, priority=RequestPriority.REALTIME_QUOTE)
            out0 = data.get("Output_0", {})
            out1 = data.get("Output_1", [])
            
            curr_price = out1[0].get("stck_prpr", 0) if out1 else out0.get("stck_prpr", 0)
            prev_close = out0.get("stck_prdy_clpr", 0)
            
            # 시간 및 최우선 호가 추출 (hoga_bsop_hour, bsop_hour, askp, bidp)
            has_out1 = bool(isinstance(out1, list) and len(out1) > 0 and isinstance(out1[0], dict))
            bsop_hour = out1[0].get("bsop_hour", "") if has_out1 else ""
            hoga_hour = out0.get("hoga_bsop_hour", "") if isinstance(out0, dict) else ""
            
            raw_ask = (out1[0].get("askp") if has_out1 else None) or (out0.get("askp1") or out0.get("askp") if isinstance(out0, dict) else None) or curr_price
            raw_bid = (out1[0].get("bidp") if has_out1 else None) or (out0.get("bidp1") or out0.get("bidp") if isinstance(out0, dict) else None) or curr_price
            ask = int(float(raw_ask or curr_price))
            bid = int(float(raw_bid or curr_price))
            quote_time_str = bsop_hour or hoga_hour or ""

            # ISO timestamp 생성
            now_dt = datetime.now()
            quote_dt = now_dt
            if quote_time_str:
                s = str(quote_time_str).strip().replace(":", "")
                if len(s) >= 6:
                    try:
                        quote_dt = now_dt.replace(hour=int(s[0:2]), minute=int(s[2:4]), second=int(s[4:6]), microsecond=0)
                    except Exception:
                        quote_dt = now_dt
                elif len(s) >= 4:
                    try:
                        quote_dt = now_dt.replace(hour=int(s[0:2]), minute=int(s[2:4]), second=0, microsecond=0)
                    except Exception:
                        quote_dt = now_dt

            return {
                "iem_cd": iem_cd,
                "name": config.TARGET_STOCKS.get(iem_cd, out0.get("iem_nm", iem_cd)),
                "price": int(curr_price),
                "prev_close": int(prev_close),
                "open": int(out0.get("stck_oprc", curr_price)),
                "high": int(out0.get("stck_hgpr", curr_price)),
                "low": int(out0.get("stck_lwpr", curr_price)),
                "rate": float(out0.get("prdy_ctrt", 0.0)),
                "volume": int(out0.get("acml_vol", 0)),
                "ask": ask,
                "bid": bid,
                "quote_time": quote_time_str,
                "timestamp": quote_dt.isoformat(),
                "is_valid": True
            }
        except Exception as e:
            if "00200" in str(e) or "입력정보" in str(e):
                logger.warning(f"존재하지 않거나 조회 불가 종목코드 ({iem_cd}): {e}")
            else:
                logger.error(f"현재가 조회 실패 ({iem_cd}): {e}")
            return {
                "iem_cd": iem_cd,
                "name": config.TARGET_STOCKS.get(iem_cd, iem_cd),
                "price": 0, "prev_close": 0, "open": 0, "high": 0, "low": 0,
                "rate": 0.0, "volume": 0, "is_valid": False
            }

    def get_buyable_quantity(self, iem_cd: str, price: int = 0, order_type: str = "05") -> dict:
        """
        종목별 매수가능수량 조회 (/krstock/inquiry/v1/buyableQuantity)
        :param iem_cd: 종목코드 (6자리)
        :param price: 주문단가 (시장가일 경우 0)
        :param order_type: "05" (시장가) 또는 "01" (지정가)
        :return: {
            "iem_cd": iem_cd,
            "csh_orr_pbl_qty": int,
            "csh_orr_pbl_amt": int,
            "max_pbl_qty": int,
            "max_pbl_amt": int,
            "dca": int,
            "nxt2_dd_dca": int,
            "is_valid": bool
        }
        """
        iem_cd = str(iem_cd).strip()
        is_market = str(order_type).upper() in ("05", "MARKET", "ORDERTYPE.MARKET")
        nmn_pr_tp_cd = "05" if is_market else "01"
        orr_pr = 0 if is_market else int(price)

        if getattr(self, "dry_run", False):
            bal = self.get_balance()
            avail_amt = int(bal.get("order_available", bal.get("cash", 0)))
            ref_pr = max(1, orr_pr if orr_pr > 0 else 10000)
            approx_qty = int(avail_amt / (ref_pr * (1.25 if is_market else 1.00015)))
            return {
                "iem_cd": iem_cd,
                "csh_orr_pbl_qty": approx_qty,
                "csh_orr_pbl_amt": avail_amt,
                "max_pbl_qty": approx_qty,
                "max_pbl_amt": avail_amt,
                "dca": avail_amt,
                "nxt2_dd_dca": avail_amt,
                "is_valid": True
            }

        input_0 = {
            "ost_dit_cd": "1",  # 1.현금
            "act_no": self.act_no,
            "iem_cd": iem_cd,
            "nmn_pr_tp_cd": nmn_pr_tp_cd,
            "orr_pr": orr_pr
        }

        try:
            res = self._safe_call(
                "/krstock/inquiry/v1/buyableQuantity",
                input_0,
                self.trade_base_url,
                priority=RequestPriority.ENTRY_ORDER,
                retries=2,
                timeout=5
            )
            out0 = res.get("Output_0", {}) if isinstance(res, dict) else {}
            csh_qty = int(float(out0.get("csh_orr_pbl_qty", 0) or 0))
            csh_amt = int(float(out0.get("csh_orr_pbl_amt", 0) or 0))
            max_qty = int(float(out0.get("max_pbl_qty", csh_qty) or csh_qty))
            max_amt = int(float(out0.get("max_pbl_amt", csh_amt) or csh_amt))
            dca = int(float(out0.get("dca", 0) or 0))
            nxt2 = int(float(out0.get("nxt2_dd_dca", 0) or 0))
            return {
                "iem_cd": iem_cd,
                "csh_orr_pbl_qty": csh_qty,
                "csh_orr_pbl_amt": csh_amt,
                "max_pbl_qty": max_qty,
                "max_pbl_amt": max_amt,
                "dca": dca,
                "nxt2_dd_dca": nxt2,
                "is_valid": True
            }
        except Exception as e:
            logger.warning(f"[{self.mode.upper()}] 매수가능수량 조회 실패 ({iem_cd}): {e}")
            return {
                "iem_cd": iem_cd,
                "csh_orr_pbl_qty": 0,
                "csh_orr_pbl_amt": 0,
                "max_pbl_qty": 0,
                "max_pbl_amt": 0,
                "dca": 0,
                "nxt2_dd_dca": 0,
                "is_valid": False,
                "error": str(e)
            }

    def get_daily_candles(self, iem_cd: str, count: int = 20) -> list[dict]:
        """일자별 시세 조회 (중앙 DailyDataService 위임: Positive/Negative 캐시 및 SingleFlight 적용)"""
        return self.daily_service.get_daily_candles(self, iem_cd, count=count)

    def get_balance(self) -> dict:
        """계좌 총 평가금액, 예수금 및 보유 종목 상세 조회 (중앙 BalanceService 위임: 연속조회 취합 및 캐시 적용)"""
        return self.balance_service.get_balance(self.act_no, self.trade_base_url)

    def buy_market(self, iem_cd: str, qty: int, dry_run: bool = None) -> dict:
        is_dry = self.dry_run if dry_run is None else dry_run
        input_0 = {
            "act_no": self.act_no,
            "iem_cd": iem_cd,
            "orr_qty": qty,
            "nmn_pr_tp_cd": "05",  # 시장가
            "orr_cnd_dit_cd": "00",
            "ssl_nmn_pr_dit_cd": "00",
            "rmt_mkt_cd": "KRX",
            "sor_mkt_sli_yn": "N",
        }
        if is_dry:
            logger.info(f"[시뮬레이션 매수] {iem_cd} {qty}주 (시장가) - 실제 주문 미전송")
            return {"dry_run": True, "iem_cd": iem_cd, "qty": qty}

        logger.info(f"[주문 전송] {iem_cd} {qty}주 시장가 매수...")
        return self._safe_call("/krstock/order/v1/cashBuy", input_0, self.trade_base_url, priority=RequestPriority.ENTRY_ORDER)

    def sell_market(self, iem_cd: str, qty: int, dry_run: bool = None, is_emergency: bool = False) -> dict:
        is_dry = self.dry_run if dry_run is None else dry_run
        input_0 = {
            "act_no": self.act_no,
            "iem_cd": iem_cd,
            "orr_qty": qty,
            "nmn_pr_tp_cd": "05",  # 시장가
            "orr_cnd_dit_cd": "00",
            "ssl_nmn_pr_dit_cd": "00",
            "rmt_mkt_cd": "KRX",
            "sor_mkt_sli_yn": "N",
        }
        if is_dry:
            logger.info(f"[시뮬레이션 매도] {iem_cd} {qty}주 (시장가) - 실제 주문 미전송")
            return {"dry_run": True, "iem_cd": iem_cd, "qty": qty}

        priority = RequestPriority.EMERGENCY_STOP if is_emergency else RequestPriority.EXIT_ORDER
        logger.info(f"[주문 전송] {iem_cd} {qty}주 시장가 매도... (우선순위: {priority.name})")
        return self._safe_call("/krstock/order/v1/cashSell", input_0, self.trade_base_url, priority=priority)

    def buy_limit(self, iem_cd: str, qty: int, price: int, dry_run: bool = None) -> dict:
        is_dry = self.dry_run if dry_run is None else dry_run
        input_0 = {
            "act_no": self.act_no,
            "iem_cd": iem_cd,
            "orr_qty": qty,
            "orr_pr": price,
            "nmn_pr_tp_cd": "01",  # 보통가(지정가)
            "orr_cnd_dit_cd": "00",
            "ssl_nmn_pr_dit_cd": "00",
            "rmt_mkt_cd": "KRX",
            "sor_mkt_sli_yn": "N",
        }
        if is_dry:
            logger.info(f"[시뮬레이션 매수] {iem_cd} {qty}주 @ {price:,}원 (지정가)")
            return {"dry_run": True, "iem_cd": iem_cd, "qty": qty, "price": price}

        logger.info(f"[주문 전송] {iem_cd} {qty}주 @ {price:,}원 지정가 매수...")
        return self._safe_call("/krstock/order/v1/cashBuy", input_0, self.trade_base_url, priority=RequestPriority.ENTRY_ORDER)

    def sell_limit(self, iem_cd: str, qty: int, price: int, dry_run: bool = None) -> dict:
        is_dry = self.dry_run if dry_run is None else dry_run
        input_0 = {
            "act_no": self.act_no,
            "iem_cd": iem_cd,
            "orr_qty": qty,
            "orr_pr": price,
            "nmn_pr_tp_cd": "01",  # 보통가(지정가)
            "orr_cnd_dit_cd": "00",
            "ssl_nmn_pr_dit_cd": "00",
            "rmt_mkt_cd": "KRX",
            "sor_mkt_sli_yn": "N",
        }
        if is_dry:
            logger.info(f"[시뮬레이션 매도] {iem_cd} {qty}주 @ {price:,}원 (지정가)")
            return {"dry_run": True, "iem_cd": iem_cd, "qty": qty, "price": price}

        logger.info(f"[주문 전송] {iem_cd} {qty}주 @ {price:,}원 지정가 매도...")
        return self._safe_call("/krstock/order/v1/cashSell", input_0, self.trade_base_url, priority=RequestPriority.EXIT_ORDER)

    def get_sellable_quantity(self, iem_cd: str) -> dict:
        """
        종목별 매도가능수량 조회 (/krstock/inquiry/v1/sellableQuantity)
        :param iem_cd: 종목코드 (6자리)
        :return: {
            "iem_cd": iem_cd,
            "holding_qty": int,
            "unfilled_sell_qty": int,
            "sellable_qty": int,
            "is_valid": bool
        }
        """
        iem_cd = str(iem_cd).strip()
        if getattr(self, "dry_run", False):
            bal = self.get_balance()
            holdings = {h.get("iem_cd"): h for h in bal.get("holdings", [])}
            h = holdings.get(iem_cd, {})
            qty = int(h.get("qty", 0))
            return {
                "iem_cd": iem_cd,
                "holding_qty": qty,
                "unfilled_sell_qty": 0,
                "sellable_qty": qty,
                "is_valid": True
            }

        input_0 = {
            "act_no": self.act_no,
            "iem_cd": iem_cd,
            "cfd_lon_cd": "00"
        }
        try:
            res = self._safe_call(
                "/krstock/inquiry/v1/sellableQuantity",
                input_0,
                self.trade_base_url,
                priority=RequestPriority.EXIT_ORDER,
                retries=2,
                timeout=5
            )
            out0 = res.get("Output_0", {}) if isinstance(res, dict) else {}
            bnc_qty = int(float(out0.get("bnc_qty", 0) or 0))
            unfilled = int(float(out0.get("tdt_sll_ny_cns_qty", 0) or 0))
            psbl_qty = int(float(out0.get("sll_pbl_qty", max(0, bnc_qty - unfilled)) or 0))
            return {
                "iem_cd": iem_cd,
                "holding_qty": bnc_qty,
                "unfilled_sell_qty": unfilled,
                "sellable_qty": psbl_qty,
                "bnc_qty": bnc_qty,
                "tdt_sll_ny_cns_qty": unfilled,
                "sll_pbl_qty": psbl_qty,
                "is_valid": True
            }
        except Exception as e:
            logger.warning(f"[{self.mode.upper()}] 매도가능수량 조회 실패 ({iem_cd}): {e}")
            return {
                "iem_cd": iem_cd,
                "holding_qty": 0,
                "unfilled_sell_qty": 0,
                "sellable_qty": 0,
                "is_valid": False,
                "error": str(e)
            }

    def get_daily_order_execution(self, dt: str = None, ost_cns_dit: str = "0") -> list[dict]:
        """
        주식일별주문체결조회 (/krstock/inquiry/v1/dailyOrderExecution)
        :param dt: 조회일자 (YYYYMMDD, None시 오늘)
        :param ost_cns_dit: "0"(전체), "1"(체결), "2"(미체결)
        :return: list of order dicts
        """
        if getattr(self, "dry_run", False):
            return []

        orr_dt = dt or datetime.now().strftime("%Y%m%d")
        input_0 = {
            "orr_dt": orr_dt,
            "act_no": self.act_no,
            "ost_cns_dit": str(ost_cns_dit),
            "orr_mkt_cd": "00"
        }
        try:
            res = self._safe_call(
                "/krstock/inquiry/v1/dailyOrderExecution",
                input_0,
                self.trade_base_url,
                priority=RequestPriority.RECONCILIATION,
                retries=2,
                timeout=5
            )
            return res.get("Output_0", []) if isinstance(res, dict) and isinstance(res.get("Output_0"), list) else (res.get("Output_1", []) if isinstance(res, dict) else [])
        except Exception as e:
            logger.warning(f"[{self.mode.upper()}] 주식일별주문체결조회 실패: {e}")
            return []

    def cancel_order(self, orr_no: str, iem_cd: str, qty: int = 0, dry_run: bool = None) -> dict:
        """
        주식 주문 취소 (/krstock/order/v1/cancel)
        """
        is_dry = self.dry_run if dry_run is None else dry_run
        all_pat = "1" if qty <= 0 else "2"
        input_0 = {
            "act_no": self.act_no,
            "org_mkt_orr_no": int(orr_no) if str(orr_no).isdigit() else 0,
            "all_pat_dit_cd": all_pat,
            "iem_cd": iem_cd,
            "cor_qty": int(qty)
        }
        if is_dry:
            logger.info(f"[시뮬레이션 주문 취소] {iem_cd} 원주문번호 {orr_no} ({qty}주)")
            return {"dry_run": True, "orr_no": orr_no, "iem_cd": iem_cd}

        logger.info(f"[주문 취소 전송] {iem_cd} 원주문번호 {orr_no} ({qty}주)...")
        return self._safe_call("/krstock/order/v1/cancel", input_0, self.trade_base_url, priority=RequestPriority.EXIT_ORDER)

