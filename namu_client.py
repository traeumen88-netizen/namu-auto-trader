"""나무증권 (NH투자증권 나무 플러그) 전용 API 통신 클라이언트 모듈
- 시세 조회 (실시간 시세 서버: https://api.nhplug.com:8443)
- 계좌 잔고 및 보유 종목 분석 (모의: moapi / 실전: api)
- 매수/매도 주문 (시장가, 지정가)
- 초당 호출 유량 제한(Rate Limit) 방어 및 재시도 로직 내장
"""

import os
import time
import logging
import nhplug
from nhplug.errors import NhplugError
import config

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

        # 토큰 사전 초기화
        self.token = nhplug.get_token()
        logger.info(f"나무증권 API 클라이언트 초기화 완료 [모드: {config.MODE_NAME}, 계좌: {self.act_no}]")

    def _safe_call(self, path: str, input_data: dict, target_url: str, retries: int = 3, delay: float = 0.25):
        """API 호출 시 대상 서버(시세 vs 주문) 설정 및 유량 제한 방어"""
        os.environ["NHPLUG_BASE_URL"] = target_url
        time.sleep(delay)
        for attempt in range(retries):
            try:
                res = nhplug.call(path, input_data)
                return res
            except NhplugError as e:
                if e.category == "rate_limit" or "429" in str(e):
                    wait_time = (attempt + 1) * 0.6
                    logger.warning(f"초당 호출 유량 제한 감지. {wait_time:.1f}초 대기 후 재시도... ({attempt+1}/{retries})")
                    time.sleep(wait_time)
                else:
                    logger.error(f"API 호출 오류 ({path}): {e}")
                    raise e
            except Exception as e:
                logger.error(f"예기치 못한 통신 오류 ({path}): {e}")
                time.sleep(0.5)
        raise RuntimeError(f"API 호출 {retries}회 실패: {path}")

    def get_current_price(self, iem_cd: str) -> dict:
        """
        종목 현재가 및 시세 상세 조회
        :param iem_cd: 종목코드 6자리 (예: 005930)
        :return: 종목 상세 시세 dict
        """
        data = self._safe_call("/krstock/quote/v1/currentPrice", {"iem_cd": iem_cd, "market_cd": "KRX"}, self.QUOTE_BASE_URL)
        out0 = data.get("Output_0", {})
        out1 = data.get("Output_1", [])
        
        curr_price = out1[0].get("stck_prpr", 0) if out1 else out0.get("stck_prpr", 0)
        prev_close = out0.get("stck_prdy_clpr", 0)
        
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
        }

    def get_daily_candles(self, iem_cd: str, count: int = 20) -> list[dict]:
        """
        일자별 시세 조회 (변동성 계산 및 이동평균선 산출용)
        """
        data = self._safe_call("/krstock/quote/v1/currentDaily", {
            "market_cd": "KRX",
            "iem_cd": iem_cd,
            "array_cnt": str(count),
        }, self.QUOTE_BASE_URL)
        candles = []
        for row in data.get("Output_0", []):
            candles.append({
                "date": row.get("bsop_date"),
                "open": int(row.get("stck_oprc", 0)),
                "high": int(row.get("stck_hgpr", 0)),
                "low": int(row.get("stck_lwpr", 0)),
                "close": int(row.get("stck_clpr", 0)),
                "volume": int(row.get("acml_vol", 0)),
                "rate": float(row.get("prdy_ctrt", 0.0)),
            })
        return candles

    def get_balance(self) -> dict:
        """
        계좌 총 평가금액, 예수금 및 보유 종목 상세 조회
        """
        data = self._safe_call("/krstock/inquiry/v1/balance", {
            "act_no": self.act_no,
            "bnc_bse_cd": "5",
            "ltg_aot_dit_cd": "9",
            "aet_bse": "2",
            "qut_dit_cd": "UNT",
        }, self.trade_base_url)
        
        summary = data.get("Output_0", {})
        holdings_raw = data.get("Output_1", [])
        
        holdings = []
        for item in holdings_raw:
            qty = int(item.get("itg_bnc_qty", 0))
            if qty <= 0:
                continue
            holdings.append({
                "iem_cd": item.get("iem_cd"),
                "iem_nm": item.get("iem_nm"),
                "qty": qty,
                "buy_price": float(item.get("phs_pr", 0)),
                "now_price": int(item.get("now_pr", 0)),
                "eval_amount": int(item.get("eal_amt", 0)),
                "profit_amount": int(item.get("eal_pls_amt", 0)),
                "profit_rate": float(item.get("pft_rt", 0.0)),
            })
            
        return {
            "cash": int(summary.get("dca", 0)),                      # 예수금
            "total_asset": int(summary.get("tot_aet_amt", 0)),        # 총자산
            "total_buy": int(summary.get("tot_byn_amt", 0)),          # 총매입금액
            "total_eval": int(summary.get("tot_eal_amt", 0)),         # 총평가금액
            "total_profit": int(summary.get("tot_eal_pls", 0)),       # 총평가손익
            "total_profit_rate": float(summary.get("pft_rt", 0.0)),   # 총수익률(%)
            "order_available": int(summary.get("orr_pbl_amt1", 0)),   # 주문가능금액
            "holdings": holdings,
        }

    def buy_market(self, iem_cd: str, qty: int, dry_run: bool = None) -> dict:
        """
        국내주식 시장가 매수 주문
        """
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
        return self._safe_call("/krstock/order/v1/cashBuy", input_0, self.trade_base_url)

    def sell_market(self, iem_cd: str, qty: int, dry_run: bool = None) -> dict:
        """
        국내주식 시장가 매도 주문
        """
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

        logger.info(f"[주문 전송] {iem_cd} {qty}주 시장가 매도...")
        return self._safe_call("/krstock/order/v1/cashSell", input_0, self.trade_base_url)

    def buy_limit(self, iem_cd: str, qty: int, price: int, dry_run: bool = None) -> dict:
        """
        국내주식 지정가(보통가) 매수 주문
        """
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
        return self._safe_call("/krstock/order/v1/cashBuy", input_0, self.trade_base_url)

    def sell_limit(self, iem_cd: str, qty: int, price: int, dry_run: bool = None) -> dict:
        """
        국내주식 지정가(보통가) 매도 주문
        """
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
        return self._safe_call("/krstock/order/v1/cashSell", input_0, self.trade_base_url)
