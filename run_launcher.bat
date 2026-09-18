@echo off
title AI Quant 자동매매 통합 런처 v9.3
cd /d "C:\Users\DAPCHC-071\namu-auto-trader"

:MENU
cls
echo ======================================================================
echo         AI QUANT 국내 주식 실시간 자동매매 통합 런처 v9.3
echo ======================================================================
echo.
echo   [1] 실시간 통합 듀얼 자동매매 (모의 + 실전 동시 가동) [추천]
echo   [2] 실시간 웹 관제탑 (웹 대시보드 http://127.0.0.1:8080)
echo   [3] 모의투자 실시간 자동매매 단독 (계좌: 50001003032)
echo   [4] 실전투자 실시간 자동매매 단독 (계좌: 20201549311)
echo   [5] 7대 매수 시나리오 강제 검증 (FORCE_SIGNAL_TEST)
echo   [6] 종합 테스트 하네스 검증 (56개 모듈 전수 검증)
echo   [7] 전체 시스템 단위 테스트 실행 (99개 전수 검증)
echo   [Q] 종료
echo.
echo ======================================================================
set /p opt=실행할 번호를 입력하세요 (1-7, Q): 

if "%opt%"=="1" (
    start "AI Quant 통합 듀얼 매매" "C:\Users\DAPCHC-071\namu-auto-trader\run_dual_trader.bat"
    goto MENU
)
if "%opt%"=="2" (
    start "AI Quant 웹 대시보드" "C:\Users\DAPCHC-071\namu-auto-trader\run_dashboard.bat"
    goto MENU
)
if "%opt%"=="3" (
    start "AI Quant 모의투자" "C:\Users\DAPCHC-071\namu-auto-trader\run_mock_trader.bat"
    goto MENU
)
if "%opt%"=="4" (
    start "AI Quant 실전투자" "C:\Users\DAPCHC-071\namu-auto-trader\run_live_trader.bat"
    goto MENU
)
if "%opt%"=="5" (
    cls
    echo [검증] 7대 매수 시나리오 강제 검증을 실행합니다...
    echo.
    "C:\Users\DAPCHC-071\AppData\Local\Python\bin\python.exe" "C:\Users\DAPCHC-071\namu-auto-trader\execution\live_quant_trader.py" --dual --test-signal
    echo.
    pause
    goto MENU
)
if "%opt%"=="6" (
    cls
    echo [검증] 종합 테스트 하네스(56개 전수 검증)를 실행합니다...
    echo.
    "C:\Users\DAPCHC-071\AppData\Local\Python\bin\python.exe" "C:\Users\DAPCHC-071\namu-auto-trader\tests\test_harness.py"
    echo.
    pause
    goto MENU
)
if "%opt%"=="7" (
    cls
    echo [검증] 전체 시스템 단위 테스트(99개 전수 검증)를 실행합니다...
    echo.
    "C:\Users\DAPCHC-071\AppData\Local\Python\bin\python.exe" -m unittest discover -s "C:\Users\DAPCHC-071\namu-auto-trader\tests" -p "test_*.py"
    echo.
    pause
    goto MENU
)
if /i "%opt%"=="q" exit /b
goto MENU
