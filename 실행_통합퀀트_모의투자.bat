@echo off
chcp 65001 > nul
title [모의투자] 국내 주식 통합 퀀트 자동매매 시스템 v5.0

echo ======================================================================
echo    [모의투자] 국내 주식 통합 퀀트 자동매매 시스템 (INTRADAY + SWING)
echo ======================================================================
echo.
echo * 모의 계좌: 50001003032 (예수금 5억원)
echo * 전략 엔진: 단타 5대(ORB, PDH, VWAP, EMA, Momentum) + 스윙 4대(Trend, HH60, MA20, MA60)
echo * 안전 장치: 10단계 주문검증, 15초 주기 감시, 서킷브레이커, 손익절/Trailing Stop
echo.
echo 퀀트 엔진을 시작합니다...
echo (종료하려면 언제든지 Ctrl + C 키를 누르세요)
echo.

python execution\live_quant_trader.py --mock

pause
