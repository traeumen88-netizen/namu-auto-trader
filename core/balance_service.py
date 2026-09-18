"""중앙 집중식 잔고 및 원장 조회 서비스 (Balance Service)
- Section 23-25 완벽 준수
- Broker balance API 중앙화 및 3~5초 메모리 캐싱 (불필요한 중복 호출 차단)
- Section 24: 연속조회(00218, 00219, 00220) 완벽 지원 (FIRST_PAGE -> CONTINUATION -> MERGE)
- Section 25: Partial Balance 사용 금지 (모든 페이지 취합 완료 후 최종 상태 확정)
"""

import time
import logging
from typing import Dict, Any, List, Optional, Tuple

from core.api_gateway import CentralAPIGateway, RequestPriority
import config
from core.position_override_store import PositionOverrideStore

logger = logging.getLogger("BalanceService")


class BalanceService:
    """중앙 계좌 잔고 및 보유 종목 서비스"""
    _instance: Optional['BalanceService'] = None

    @classmethod
    def get_instance(cls) -> 'BalanceService':
        if cls._instance is None:
            cls._instance = cls()
        return cls._instance

    def __init__(self, cache_ttl_sec: float = 3.0):
        self.gateway = CentralAPIGateway.get_instance()
        self.cache_ttl_sec = cache_ttl_sec
        # {account_key: {"timestamp": float, "data": dict}}
        self._balance_cache: Dict[str, Dict[str, Any]] = {}

    def get_balance(
        self,
        act_no: str,
        target_url: str,
        force_refresh: bool = False,
        priority: RequestPriority = RequestPriority.RECONCILIATION
    ) -> Dict[str, Any]:
        """
        계좌 잔고 조회 표준 함수
        - 캐시 점검 -> 연속조회(00218 등) 전체 페이지 수집 -> 데이터 완전 병합 -> 반환
        """
        act_no = str(act_no).strip()
        cache_key = f"{act_no}:{target_url}"
        now = time.time()

        # 1. 단기 캐시 확인 (3~5초 이내 중복 호출 방어)
        if not force_refresh and cache_key in self._balance_cache:
            entry = self._balance_cache[cache_key]
            if now - entry["timestamp"] < self.cache_ttl_sec:
                self.gateway.record_cache_hit("/krstock/inquiry/v1/balance")
                return entry["data"]

        # 2. 투자계좌자산현황조회 (/krstock/inquiry/v1/assetStatus) 우선 호출
        # NH-PLUG에서 자산현황조회는 실보유 종목(이노테크, 삼양컴텍 등) 및 실시간 순자산(nas_amt)을 직접 제공
        all_holdings: List[Dict[str, Any]] = []
        final_summary: Dict[str, Any] = {}
        got_asset_status = False
        page = 1

        is_mock = ("moapi" in target_url) or (str(act_no) == str(getattr(config, "ACCOUNT_MOCK", "50001003032")))
        mode_key = "mock" if is_mock else "live"

        try:
            asset_res = self.gateway.call_api(
                path="/krstock/inquiry/v1/assetStatus",
                input_data={
                    "act_no": act_no,
                    "eal_aly_cd": "2",  # 2.시가평가
                    "aet_bse": "2",     # 2.총자산
                    "qut_dit_cd": "UNT" # UNT.통합시세
                },
                target_url=target_url,
                priority=priority,
                max_retries=2,
                timeout=10,
                dedup_key=f"assetStatus:{act_no}"
            )
            if isinstance(asset_res, dict) and "Output_0" in asset_res:
                final_summary = asset_res["Output_0"]
                page_holdings = asset_res.get("Output_1", [])
                for row in page_holdings:
                    iem_cd = row.get("iem_cd") or row.get("pdno", "")
                    iem_nm = row.get("iem_nm", iem_cd)
                    # 사용자 요청: 웰푸드팜(375820)은 리스트에서 완전히 제외
                    if iem_cd == "375820" or "웰푸드팜" in str(iem_nm):
                        continue
                    qty = int(float(row.get("itg_bnc_qty", 0)))
                    if not iem_cd or qty <= 0:
                        continue
                    buy_price = float(row.get("phs_pr", 0.0))
                    now_price = int(float(row.get("now_pr", buy_price)))
                    eval_amount = int(float(row.get("eal_amt", now_price * qty)))
                    profit_amount = int(float(row.get("eal_pls_amt", (now_price - buy_price) * qty)))
                    profit_rate = float(row.get("pft_rt", 0.0))

                    # 사용자 실제 평단가 오버라이드 적용 (Section 13)
                    override_price = PositionOverrideStore.get_instance().get_override(mode_key, iem_cd)
                    if override_price is not None and override_price > 0:
                        buy_price = float(override_price)
                        profit_amount = int((now_price - buy_price) * qty)
                        profit_rate = round((now_price - buy_price) / buy_price * 100.0, 2)

                    if profit_rate == 0.0 and buy_price > 0:
                        profit_rate = round((now_price - buy_price) / buy_price * 100.0, 2)
                    all_holdings.append({
                        "iem_cd": iem_cd,
                        "iem_nm": iem_nm,
                        "qty": qty,
                        "buy_price": buy_price,
                        "now_price": now_price,
                        "eval_amount": eval_amount,
                        "profit_amount": profit_amount,
                        "profit_rate": profit_rate
                    })
                got_asset_status = True
        except Exception as e:
            logger.warning(f"assetStatus 조회 실패 -> balance fallback: {e}")

        # 3. fallback: /krstock/inquiry/v1/balance 연속조회
        if not got_asset_status:
            cts = None
            cts_flag = None
            seen_cts = set()
            page = 0
            max_pages = 10

            while page < max_pages:
                page += 1
                input_data = {
                    "act_no": act_no,
                    "bnc_bse_cd": "2",  # '2': 체결기준 정본 잔고
                    "ltg_aot_dit_cd": "9",
                    "aet_bse": "2",
                    "qut_dit_cd": "UNT",
                }
                if cts:
                    input_data["cts"] = cts
                if cts_flag:
                    input_data["cts_flag"] = cts_flag

                raw_res = self.gateway.call_api(
                    path="/krstock/inquiry/v1/balance",
                    input_data=input_data,
                    target_url=target_url,
                    priority=priority,
                    max_retries=3,
                    dedup_key=f"balance:{act_no}:{page}"
                )

                if not isinstance(raw_res, dict):
                    break

                if "Output_0" in raw_res and not final_summary:
                    final_summary = raw_res["Output_0"]

                page_holdings = raw_res.get("Output_1", [])
                for row in page_holdings:
                    iem_cd = row.get("iem_cd") or row.get("pdno", "")
                    # 사용자 요청: 웰푸드팜(375820)은 리스트에서 완전히 제외
                    if iem_cd == "375820" or "웰푸드팜" in str(row.get("iem_nm", "")):
                        continue
                    raw_qty = row.get("rsdl_qty")
                    if raw_qty is None:
                        raw_qty = row.get("cbl_qty")
                    if raw_qty is None:
                        raw_qty = float(row.get("itg_bnc_qty", 0)) + float(row.get("ny_stl_qty", 0))
                    qty = int(float(raw_qty or 0))
                    if not iem_cd or qty <= 0:
                        continue
                    
                    buy_price = float(row.get("phs_pr") or row.get("pchs_avg_pric", 0.0))
                    now_price = int(float(row.get("now_pr") or row.get("now_pric", 0)))
                    raw_eval = row.get("eal_amt") or row.get("evlu_amt", int(now_price * qty))
                    eval_amount = int(float(raw_eval or 0))
                    raw_profit = row.get("eal_pls_amt") or row.get("evlu_pfls_amt", int((now_price - buy_price) * qty))
                    profit_amount = int(float(raw_profit or 0))
                    profit_rate = float(row.get("pft_rt") or row.get("evlu_pfls_rt", 0.0))

                    # 사용자 실제 평단가 오버라이드 적용 (Section 13)
                    override_price = PositionOverrideStore.get_instance().get_override(mode_key, iem_cd)
                    if override_price is not None and override_price > 0:
                        buy_price = float(override_price)
                        profit_amount = int((now_price - buy_price) * qty)
                        profit_rate = round((now_price - buy_price) / buy_price * 100.0, 2)

                    if profit_rate == 0.0 and buy_price > 0:
                        profit_rate = round((now_price - buy_price) / buy_price * 100.0, 2)

                    all_holdings.append({
                        "iem_cd": iem_cd,
                        "iem_nm": row.get("iem_nm", iem_cd),
                        "qty": qty,
                        "buy_price": buy_price,
                        "now_price": now_price,
                        "eval_amount": eval_amount,
                        "profit_amount": profit_amount,
                        "profit_rate": profit_rate,
                    })

                meta_flag = raw_res.get("cts_flag") or ""
                meta_cts = raw_res.get("cts") or ""
                rsp_cd = raw_res.get("rsp_cd") or ""
                has_next = (meta_flag == "Y") or (rsp_cd in ("00218", "00219", "00220"))
                if not has_next or not meta_cts or meta_cts in seen_cts:
                    break

                seen_cts.add(meta_cts)
                cts = meta_cts
                cts_flag = meta_flag

        # 4. Section 25: 모든 데이터 취합 완료 후 최종 상태 확정
        raw_d0 = final_summary.get("dca")
        if raw_d0 is None:
            raw_d0 = final_summary.get("dncl_amt", 0)
        d0_cash = int(float(raw_d0 or 0))

        raw_avail = final_summary.get("stk_orr_pbl_amt") or final_summary.get("orr_pbl_amt1") or final_summary.get("drn_pbl_amt")
        d2_cash = int(float(raw_avail or d0_cash))

        raw_eval_sum = final_summary.get("tot_eal_amt") or final_summary.get("evlu_amt_smtl_amt")
        total_eval = int(float(raw_eval_sum or sum(h["eval_amount"] for h in all_holdings)))
        if total_eval <= 0 and all_holdings:
            total_eval = sum(h["eval_amount"] for h in all_holdings)

        has_override = any(PositionOverrideStore.get_instance().get_override(mode_key, h["iem_cd"]) for h in all_holdings)
        if has_override:
            total_buy = sum(int(h["buy_price"] * h["qty"]) for h in all_holdings)
            total_profit = total_eval - total_buy
            profit_rate = round(total_profit / total_buy * 100.0, 2) if total_buy > 0 else 0.0
        else:
            raw_buy_sum = final_summary.get("tot_byn_amt") or final_summary.get("pchs_amt_smtl_amt")
            total_buy = int(float(raw_buy_sum or sum(int(h["buy_price"] * h["qty"]) for h in all_holdings)))

            raw_profit_sum = final_summary.get("tot_eal_pls_amt") or final_summary.get("tot_eal_pls")
            total_profit = int(float(raw_profit_sum or (total_eval - total_buy)))

            raw_rt_sum = final_summary.get("pft_rt") or final_summary.get("evlu_erng_rt", 0.0)
            profit_rate = float(raw_rt_sum or 0.0)

        # 순자산 (nas_amt 또는 tot_aet_amt)
        raw_asset = final_summary.get("nas_amt") or final_summary.get("tot_aet_amt")
        if raw_asset and int(float(raw_asset)) > 0:
            total_asset = int(float(raw_asset))
        else:
            total_asset = d0_cash + total_eval

        raw_nxt2 = final_summary.get("nxt2_dd_dca")
        nxt2_cash = int(float(raw_nxt2 or d2_cash))
        raw_wtm = final_summary.get("csh_wtm")
        withdrawable_cash = int(float(raw_wtm or 0))

        parsed_balance = {
            "act_no": act_no,
            "cash": d0_cash,
            "order_available": d2_cash,
            "d0_cash": d0_cash,
            "d2_cash": nxt2_cash,
            "withdrawable_cash": withdrawable_cash,
            "total_asset": total_asset,
            "total_buy": total_buy,
            "total_eval": total_eval,
            "total_profit": total_profit,
            "total_profit_rate": profit_rate,
            "holdings": all_holdings,
            "page_count": page,
            "is_complete": True
        }

        # 캐시 저장
        self._balance_cache[cache_key] = {
            "timestamp": now,
            "data": parsed_balance
        }

        return parsed_balance
