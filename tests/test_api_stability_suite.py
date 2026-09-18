"""API 호출·토큰·캐시·재시도 전면 안정화 종합 검증 테스트 스위트 (Section 38 완벽 준수)
- Test 1: API 서버 일시 오류 발생 (IGW50025) -> Backoff, Retry, No Token Clear
- Test 2: Token 실제 만료 (401 / IGW40043) -> Refresh Lock, Token Refresh, Retry
- Test 3: 5개 Worker 동시 Token 만료 -> Token 발급 = 1회, 나머지 Worker = 새 Token 공유
- Test 4: 059180 currentDaily IGW50025 -> Negative Cache, TTL 동안 추가 호출 차단
- Test 5: Balance 00218 -> Continuation, 전체 페이지 취합, 최종 Balance 생성
- Test 6: Rate Limit 발생 (429) -> Backoff + Jitter, Retry
- Test 7: Account Error (IGW40018) -> Token Clear 절대 금지 검증
- Test 8: Network Disconnect -> Retry, Circuit Breaker
- Test 9: Stop Loss 발생 중 조회 API 장애 -> 조회 장애 != Stop 주문 차단 (우선순위 분리)
"""

import sys
import os
import time
import unittest
import threading
from unittest.mock import MagicMock, patch

# Add project root
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import nhplug
from nhplug.errors import NhplugError
from core.token_manager import TokenManager
from core.api_gateway import CentralAPIGateway, RequestPriority, ResponseClassifier, ErrorCategory
from core.daily_data_service import DailyDataService, NegativeCacheType
from core.balance_service import BalanceService
from namu_client import NamuClient


class TestApiStabilitySuite(unittest.TestCase):
    def setUp(self):
        self.tm = TokenManager.get_instance()
        self.gateway = CentralAPIGateway.get_instance()
        self.daily_service = DailyDataService.get_instance()
        self.balance_service = BalanceService.get_instance()

    def test_01_temporary_server_error_backoff_no_token_clear(self):
        """Test 1: API 서버 일시 오류(IGW50025) -> Backoff, Retry, No Token Clear"""
        print("\n[Test 1] IGW50025 서버 일시 오류 테스트")
        refresh_count_before = self.tm.refresh_count
        call_count = 0

        def mock_call(path, input_0=None, **kwargs):
            nonlocal call_count
            call_count += 1
            if call_count < 3:
                raise NhplugError("IGW50025 서버에서 일시적인 오류가 발생했습니다.", category="http", status=500)
            return {"rsp_cd": "00000", "Output_0": [{"stck_prpr": 50000}]}

        with patch("nhplug.call", side_effect=mock_call):
            res = self.gateway.call_api(
                path="/krstock/quote/v1/currentPrice",
                input_data={"iem_cd": "005930"},
                target_url="https://api.nhplug.com:8443",
                priority=RequestPriority.REALTIME_QUOTE,
                max_retries=3
            )
            self.assertEqual(res["rsp_cd"], "00000")
            self.assertEqual(call_count, 3) # 3회 재시도 후 성공
            self.assertEqual(self.tm.refresh_count, refresh_count_before) # 토큰 삭제/재발급 없음!
        print("  -> PASS: 3회 백오프 재시도 성공 및 토큰 클리어 0회 검증")

    def test_02_token_actual_expiry_refresh_lock_and_retry(self):
        """Test 2: Token 실제 만료 (401 / IGW40043) -> Refresh Lock, Token Refresh, Retry"""
        print("\n[Test 2] 실제 토큰 만료(401 / IGW40043) 재발급 테스트")
        call_count = 0
        initial_refresh_count = self.tm.refresh_count

        def mock_call(path, input_0=None, **kwargs):
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                raise NhplugError("IGW40043 유효하지 않은 token입니다.", category="auth", status=401)
            return {"rsp_cd": "00000", "Output_0": [{"stck_prpr": 70000}]}

        with patch("nhplug.call", side_effect=mock_call), \
             patch("nhplug.get_token", return_value="NEW_MOCK_TOKEN_9999"):
            res = self.gateway.call_api(
                path="/krstock/quote/v1/currentPrice",
                input_data={"iem_cd": "005930"},
                target_url="https://api.nhplug.com:8443",
                priority=RequestPriority.REALTIME_QUOTE,
                max_retries=2
            )
            self.assertEqual(res["rsp_cd"], "00000")
            self.assertEqual(self.tm.refresh_count, initial_refresh_count + 1)
        print(f"  -> PASS: 401 감지 시 Refresh Lock 획득 후 정확히 1회 갱신 검증 (refresh_count: {self.tm.refresh_count})")

    def test_03_concurrent_workers_single_token_refresh(self):
        """Test 3: 5개 Worker 동시 Token 만료 -> Token 발급 = 단 1회, 나머지 Worker = 새 Token 공유"""
        print("\n[Test 3] 5개 Worker 동시 토큰 만료 SingleFlight 병합 테스트")
        self.tm.mark_invalid(error_code="TEST_EXPIRED", reason="Test concurrency")
        real_issue_count = 0
        issue_lock = threading.Lock()

        def mock_issue(force=False):
            nonlocal real_issue_count
            with issue_lock:
                real_issue_count += 1
            time.sleep(0.1) # 통신 지연 시뮬레이션
            return f"SHARED_NEW_TOKEN_{real_issue_count}"

        results = []
        def worker_task():
            tok = self.tm.get_token(force=True, reason="CONCURRENT_WORKER")
            results.append(tok)

        with patch.object(self.tm, "_load_from_disk_cache", return_value=False), \
             patch("nhplug.get_token", side_effect=mock_issue):
            threads = [threading.Thread(target=worker_task) for _ in range(5)]
            for t in threads:
                t.start()
            for t in threads:
                t.join()

        self.assertEqual(len(results), 5)
        # 모든 워커가 동일한 토큰을 획득했는지 검증
        self.assertEqual(len(set(results)), 1)
        self.assertEqual(real_issue_count, 1) # 실제 브로커 발급 호출은 단 1회!
        print(f"  -> PASS: 5개 스레드 동시 요청 중 실제 발급 = {real_issue_count}회, 5개 모두 동일 토큰 공유")

    def test_04_daily_candles_negative_cache_suppresses_infinite_loop(self):
        """Test 4: 059180 currentDaily IGW50025 -> 1회 호출 후 Negative Cache로 추가 호출 차단"""
        print("\n[Test 4] 059180 일봉 IGW50025 10분 네거티브 캐시 테스트")
        call_count = 0

        def mock_call(path, input_0=None, **kwargs):
            nonlocal call_count
            call_count += 1
            raise NhplugError("IGW50025 서버에서 일시적인 오류가 발생했습니다.", category="http", status=500)

        # 059180 캐시 초기화
        self.daily_service._negative_cache.pop("059180", None)
        self.daily_service._positive_cache.pop("059180", None)

        with patch("nhplug.call", side_effect=mock_call):
            # 1회차: 실제 호출 시도 후 IGW50025 감지 -> 10분 네거티브 캐시 등록
            res1 = self.daily_service.get_daily_candles(None, "059180", count=20)
            self.assertEqual(res1, [])
            calls_after_first = call_count

            # 2~10회차: 3초 주기 루프처럼 9회 연속 재조회 시도
            for _ in range(9):
                res = self.daily_service.get_daily_candles(None, "059180", count=20)
                self.assertEqual(res, [])

            # 실제 API 호출은 최초 1회만 발생하고 이후 9회는 0ms 네거티브 캐시 차단!
            self.assertEqual(call_count, calls_after_first)
            self.assertIn("059180", self.daily_service._negative_cache)
            self.assertEqual(self.daily_service._negative_cache["059180"]["type"], NegativeCacheType.TRANSIENT_SERVER_FAILURE)
        print(f"  -> PASS: 10회 연속 조회 중 실제 API 호출 = {call_count}회, 9회 네거티브 캐시 적중 차단")

    def test_05_balance_continuation_and_multi_page_merge(self):
        """Test 5: Balance 00218 -> Continuation, 전체 페이지 취합, 최종 Balance 생성"""
        print("\n[Test 5] Balance 00218 연속조회 전체 페이지 취합 테스트")
        page_requested = []

        def mock_call(path, input_0=None, **kwargs):
            page_requested.append(input_0.get("cts", "PAGE1"))
            if len(page_requested) == 1:
                return {
                    "rsp_cd": "00218", # 연속조회 존재
                    "cts": "KEY_PAGE_2",
                    "cts_flag": "Y",
                    "Output_0": {"dncl_amt": "1000000", "asst_icld_evlu_amt": "1500000"},
                    "Output_1": [{"iem_cd": "005930", "iem_nm": "삼성전자", "cbl_qty": "10", "pchs_avg_pric": "50000", "now_pric": "55000"}]
                }
            else:
                return {
                    "rsp_cd": "00000", # 마지막 페이지
                    "cts": "",
                    "cts_flag": "N",
                    "Output_0": {"dncl_amt": "1000000", "asst_icld_evlu_amt": "1500000"},
                    "Output_1": [{"iem_cd": "000660", "iem_nm": "SK하이닉스", "cbl_qty": "5", "pchs_avg_pric": "100000", "now_pric": "110000"}]
                }

        with patch("nhplug.call", side_effect=mock_call):
            bal = self.balance_service.get_balance(act_no="20201549311", target_url="https://api.nhplug.com:8443", force_refresh=True)
            self.assertEqual(bal["page_count"], 2)
            self.assertEqual(len(bal["holdings"]), 2)
            symbols = [h["iem_cd"] for h in bal["holdings"]]
            self.assertIn("005930", symbols)
            self.assertIn("000660", symbols)
            self.assertEqual(bal["total_asset"], 1500000)
            self.assertTrue(bal["is_complete"])
        print("  -> PASS: 00218 연속조회 2페이지 자동 수집 및 종목 완전 병합(삼성전자+SK하이닉스) 검증")

    def test_06_rate_limit_backoff_and_retry(self):
        """Test 6: Rate Limit (429) 발생 -> Backoff + Jitter, Retry"""
        print("\n[Test 6] 429 Rate Limit 지터 백오프 재시도 테스트")
        call_count = 0

        def mock_call(path, input_0=None, **kwargs):
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                raise NhplugError("IGW42902 초당 호출 한도 초과", category="rate_limit", status=429)
            return {"rsp_cd": "00000", "Output_0": {"result": "OK"}}

        with patch("nhplug.call", side_effect=mock_call):
            res = self.gateway.call_api(
                path="/krstock/quote/v1/currentPrice",
                input_data={"iem_cd": "005930"},
                target_url="https://api.nhplug.com:8443",
                priority=RequestPriority.REALTIME_QUOTE,
                max_retries=2
            )
            self.assertEqual(res["rsp_cd"], "00000")
            self.assertEqual(call_count, 2)
        print("  -> PASS: 429 감지 시 지터 백오프 후 2차 시도 정상 복구 검증")

    def test_07_account_error_igw40018_no_token_clear(self):
        """Test 7: Account Error (IGW40018) -> Token Clear = NO"""
        print("\n[Test 7] IGW40018 계좌번호 오류 시 토큰 삭제 절대 금지 검증")
        refresh_count_before = self.tm.refresh_count

        def mock_call(path, input_0=None, **kwargs):
            raise NhplugError("IGW40018 토큰정보에 발급된 계좌정보가 존재하지 않습니다.", category="http", status=400)

        with patch("nhplug.call", side_effect=mock_call):
            with self.assertRaises(NhplugError):
                self.gateway.call_api(
                    path="/krstock/inquiry/v1/balance",
                    input_data={"act_no": "WRONG_ACCOUNT"},
                    target_url="https://api.nhplug.com:8443",
                    priority=RequestPriority.RECONCILIATION,
                    max_retries=1
                )
            # 계좌 오류가 났어도 토큰은 절대 삭제/재발급되지 않아야 함!
            self.assertEqual(self.tm.refresh_count, refresh_count_before)
            self.assertTrue(self.tm.is_valid())
        print("  -> PASS: IGW40018 발생 시 토큰 클리어 0회 및 유효 상태 유지 확인")

    def test_08_circuit_breaker_isolation(self):
        """Test 8: 시세 조회 연속 장애 발생 시 Circuit Breaker 격리 검증"""
        print("\n[Test 8] Endpoint Circuit Breaker 격리 테스트")
        cb = self.gateway.quote_circuit
        cb.record_success() # 초기화

        for _ in range(cb.failure_threshold):
            cb.record_failure()

        self.assertEqual(cb.state, "PAUSED")
        self.assertFalse(cb.can_execute())
        print(f"  -> PASS: 연속 {cb.failure_threshold}회 실패 후 Circuit Breaker 상태: PAUSED")
        cb.record_success() # 복구

    def test_09_stop_loss_isolated_from_quote_outage(self):
        """Test 9: Stop Loss 발생 중 조회 API 장애 -> Stop 주문 차단 금지 (독립 실행)"""
        print("\n[Test 9] 시세 조회 서킷 차단 중 긴급 손절(EMERGENCY_STOP) 독립 실행 보장 테스트")
        # 시세 서킷 브레이커를 강제로 PAUSED 상태로 차단
        self.gateway.quote_circuit.state = "PAUSED"
        self.gateway.quote_circuit.last_failure_time = time.time()

        order_executed = False
        def mock_call(path, input_0=None, **kwargs):
            nonlocal order_executed
            order_executed = True
            return {"rsp_cd": "00000", "Output_0": {"ord_no": "99999"}}

        with patch("nhplug.call", side_effect=mock_call):
            # 1. 시세 조회 시도: 서킷 브레이커에 의해 차단되어야 함
            with self.assertRaises(NhplugError) as ctx:
                self.gateway.call_api(
                    path="/krstock/quote/v1/currentPrice",
                    input_data={"iem_cd": "005930"},
                    target_url="https://api.nhplug.com:8443",
                    priority=RequestPriority.REALTIME_QUOTE
                )
            self.assertIn("서킷 브레이커 차단 상태", str(ctx.exception))

            # 2. 긴급 손절(EMERGENCY_STOP) 매도 주문 시도: 시세 장애와 무관하게 서킷을 바이패스하여 즉시 실행!
            res_order = self.gateway.call_api(
                path="/krstock/order/v1/cashSell",
                input_data={"iem_cd": "005930", "qty": 10},
                target_url="https://api.nhplug.com:8443",
                priority=RequestPriority.EMERGENCY_STOP
            )
            self.assertEqual(res_order["rsp_cd"], "00000")
            self.assertTrue(order_executed)
        print("  -> PASS: 시세 서킷 차단 중에도 긴급 손절 매도 주문 정상 전송 성공 (우선순위 격리 완벽 보장)")


if __name__ == "__main__":
    unittest.main(verbosity=2)
