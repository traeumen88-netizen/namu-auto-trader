"""나무증권 (NH투자증권) 주식 자동매매 시스템 메인 실행기
- 실시간 시세 및 계좌 잔고 모니터링
- 익절(+4%) / 손절(-2%) 자동 감시 및 즉시 매도 주문
- 변동성 돌파 목표가 도달 시 자동 매수 주문
"""

import sys
import time
import datetime
import os

# 윈도우 콘솔 한글/유니코드 출력 인코딩 보정
if sys.platform == "win32":
    try:
        sys.stdout.reconfigure(encoding="utf-8")
        sys.stderr.reconfigure(encoding="utf-8")
    except Exception:
        pass

# 현재 경로 추가
sys.path.append(os.path.dirname(os.path.abspath(__file__)))

import config
from namu_client import NamuClient
from strategy import StrategyEngine


def print_banner(client):
    print("=" * 70)
    print(f"      나무증권(NH투자증권) 알고리즘 자동매매 시스템")
    print(f"      실행 모드: {config.MODE_NAME}")
    print(f"      연동 계좌: {client.act_no}")
    print(f"      리스크 관리: 익절 {config.TAKE_PROFIT_RATE*100:+.1f}% | 손절 {config.STOP_LOSS_RATE*100:.1f}%")
    print(f"      1종목 최대 투자한도: {config.MAX_INVEST_PER_STOCK:,}원")
    print(f"      주문 시뮬레이션(Dry-Run): {'활성화(모의)' if config.DRY_RUN else '비활성화(실제전송)'}")
    print("=" * 70)


def format_money(val):
    try:
        return f"{int(val):,}원"
    except Exception:
        return f"{val}원"


_rolling_scan_idx = 0

def run_cycle(client, strategy):
    global _rolling_scan_idx
    now = datetime.datetime.now()
    time_str = now.strftime("%Y-%m-%d %H:%M:%S")
    
    print(f"\n[{time_str}] ─── [정기 모니터링 주기 시작] ───")

    # 1. 계좌 현황 및 보유 종목 조회
    try:
        balance = client.get_balance()
        print(f"[계좌 현황] 예수금: {format_money(balance['cash'])} | 총자산: {format_money(balance['total_asset'])} | 평가손익: {format_money(balance['total_profit'])} ({balance['total_profit_rate']:+.2f}%)")
        
        holdings = balance.get("holdings", [])
        if holdings:
            print(f"[보유 종목 현황] 총 {len(holdings)}개 종목 보유:")
            for h in holdings:
                p_str = f"{h['profit_rate']:+.2f}%"
                sign = "+" if h['profit_rate'] >= 0 else ""
                print(f"   * {h['iem_nm']}({h['iem_cd']}): {h['qty']}주 | 매입단가: {h['buy_price']:,.0f}원 | 현재가: {h['now_price']:,}원 | 수익률: {sign}{p_str}")
        else:
            print("[보유 종목 현황] 현재 보유 중인 주식이 없습니다.")

    except Exception as e:
        print(f"[오류] 계좌 잔고 조회 실패: {e}")
        return

    # 2. 보유 종목 익절 / 손절 감시 및 자동 매도
    try:
        sell_orders = strategy.check_stop_loss_and_take_profit(holdings)
        for order in sell_orders:
            print(f"⚡ [매도 실행] {order['name']}({order['iem_cd']}) {order['qty']}주 매도 주문 중... (사유: {order['reason']})")
            res = client.sell_market(order['iem_cd'], order['qty'])
            print(f"   ㄴ 결과: {res.get('rsp_msg', '주문 완료')}")
            if order['iem_cd'] in strategy.bought_today:
                strategy.bought_today.remove(order['iem_cd'])
    except Exception as e:
        print(f"[오류] 매도 주문 처리 실패: {e}")

    # 3. 전체 시장 유니버스 기반 실시간 매수 조건 탐색 (Section 69: DISPLAY LIMIT != SCANNER LIMIT)
    stock_items = list(config.TARGET_STOCKS.items())
    total_universe = len(stock_items)
    batch_size = 25

    start_idx = _rolling_scan_idx
    end_idx = (start_idx + batch_size) % total_universe if total_universe > 0 else 0
    if start_idx < end_idx:
        current_batch = stock_items[start_idx:end_idx]
    else:
        current_batch = stock_items[start_idx:] + stock_items[:end_idx]
    _rolling_scan_idx = end_idx

    print(f"\n[MARKET UNIVERSE] 전체 상장종목 실시간 감시 활성 (총 {total_universe:,}개 종목 순환 스캔 중, 화면 표시: 상위 10선)")
    
    breakout_count = 0
    displayed_items = 0
    display_limit = 10

    for idx, (code, name) in enumerate(current_batch, start=start_idx + 1):
        try:
            curr = client.get_current_price(code)
            if not curr or curr.get("price", 0) <= 0:
                continue
            vol_target = strategy.calculate_volatility_target(code, k=0.5)
            target_price = vol_target['target_price'] if vol_target else 0

            status_mark = "대기"
            is_breakout = False
            if target_price > 0 and curr['price'] >= target_price:
                status_mark = "⚡ 돌파완료"
                is_breakout = True
                breakout_count += 1
            elif target_price > 0 and curr['price'] >= target_price * 0.985:
                status_mark = "🔥 돌파임박"

            # 화면에는 상위 10개 및 돌파/임박 종목 우선 표출 (Section 69 준수)
            if is_breakout or "돌파임박" in status_mark or displayed_items < display_limit:
                print(f"   [{idx:04d}/{total_universe:,}] {name:12s} ({code}): 현재가 {curr['price']:,}원 ({curr['rate']:+.2f}%) | 돌파목표가 {target_price:,}원 [{status_mark}]")
                displayed_items += 1

            # 매수 시그널 점검: 화면 표시 여부와 무관하게 전체 시장 전수 점검 (Section 70 Test 7 준수)
            buy_signal = strategy.check_buy_signal(code, curr, v_info=vol_target)
            if buy_signal:
                print(f"   ⚡⚡ [매수 실행] {buy_signal['name']}({code}) {buy_signal['qty']}주 매수 주문 실행! (사유: {buy_signal['reason']})")
                res = client.buy_market(code, buy_signal['qty'])
                print(f"      ㄴ 주문 결과: {res.get('rsp_msg', '매수 접수 완료')}")
                strategy.bought_today.add(code)

        except Exception:
            pass


def main():
    import argparse
    parser = argparse.ArgumentParser(description="국내 주식 자기학습형 AI 자동매매 시스템 v7.0")
    parser.add_argument("--live", action="store_true", help="실전투자(LIVE) 모드로 실행")
    parser.add_argument("--mock", action="store_true", help="모의투자(MOCK) 모드로 실행")
    parser.add_argument("--v6", action="store_true", help="v6.0 전체 시장 이벤트 탐지 엔진으로 실행")
    parser.add_argument("--legacy", action="store_true", help="레거시 단순 변동성 돌파 모드로 실행")
    parser.add_argument("--web", action="store_true", help="웹 실시간 관제탑 대시보드(http://localhost:8080) 동시 실행")
    args = parser.parse_args()

    mode = "live" if args.live else "mock"
    act_no = config.ACCOUNT_LIVE if args.live else config.ACCOUNT_MOCK

    if args.web:
        try:
            from web_dashboard import start_dashboard_server
            start_dashboard_server(port=8080, open_browser=True)
        except Exception as e:
            print(f"[경고] 웹 대시보드 구동 실패 (독립 실행 가능: python web_dashboard.py): {e}")


    if args.legacy:
        # 레거시 모드
        client = NamuClient(mode=mode, act_no=act_no)
        strategy = StrategyEngine(client)
        print_banner(client)
        run_cycle(client, strategy)
        interval_seconds = 3
        print(f"\n[안내] 실시간 장중 감시 모드로 진입합니다. ({interval_seconds}초 간격 순환)")
        print("시스템을 종료하려면 Ctrl+C 를 누르세요.\n")
        try:
            while True:
                time.sleep(interval_seconds)
                run_cycle(client, strategy)
        except KeyboardInterrupt:
            print("\n\n사용자에 의해 자동매매 시스템이 안전하게 종료되었습니다.")
        return

    if args.v6:
        # Full Market Universe Event-Driven Quant Engine v6.0 가동
        from execution.live_quant_trader import LiveQuantTrader
        trader = LiveQuantTrader(mode=mode, act_no=act_no)
        trader.run_cycle()
        interval = 3
        print(f"\n[안내] 실시간 전체 시장(3,136종목) 이벤트 탐지 엔진이 가동되었습니다. ({interval}초 주기)")
        print("시스템을 종료하려면 Ctrl+C 를 누르세요.\n")
        try:
            while True:
                time.sleep(interval)
                trader.run_cycle()
        except KeyboardInterrupt:
            print("\n\n사용자에 의해 자동매매 시스템이 안전하게 종료되었습니다.")
        return

    # 기본 모드: v7.0 Self-Improving Quant AI Engine 가동
    from execution.ai_quant_trader import AIQuantTrader
    client = NamuClient(mode=mode, act_no=act_no)
    ai_trader = AIQuantTrader(namu_client=client, paper_trading=(mode == "mock"))
    ai_trader.run_cycle()

    interval = 3
    print(f"\n[안내] 국내 주식 자기학습형 AI 자동매매 시스템 v7.0이 가동되었습니다. ({interval}초 주기)")
    print("시스템을 종료하려면 Ctrl+C 를 누르세요.\n")
    try:
        while True:
            time.sleep(interval)
            ai_trader.run_cycle()
    except KeyboardInterrupt:
        print("\n\n사용자에 의해 자동매매 시스템이 안전하게 종료되었습니다.")
    return


if __name__ == "__main__":
    main()

