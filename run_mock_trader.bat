@echo off
title [모의] AI Quant 실시간 무중단 자동매매 v16.2 (모의투자 전용)
cd /d "C:\Users\DAPCHC-071\namu-auto-trader"
echo ======================================================================
echo    [모의투자] AI QUANT 실시간 무중단 자동매매 엔진 v16.2
echo ======================================================================
echo.
echo * 모의 계좌: MOCK_****03032 (NH투자증권)
echo * 감시 대상: execution/live_quant_trader.py --mock
echo * 무중단 감시: 프로세스 강제 종료/크래시 발생 시 3초 내 자동 즉시 재시작
echo.
echo [안내] 모의 매매 감시기를 시작합니다... (안전 종료: Ctrl + C)
echo.
:SUPERVISOR_LOOP
"C:\Users\DAPCHC-071\AppData\Local\Python\bin\python.exe" "C:\Users\DAPCHC-071\namu-auto-trader\execution\auto_restart_supervisor.py" --mock
set EXIT_CODE=%ERRORLEVEL%
if %EXIT_CODE% equ 0 (
echo.
echo [안내] 사용자에 의해 정상 종료되었습니다.
goto ON_END
)
echo.
echo [경고] 감시기 프로세스가 예기치 않게 종료되었습니다 (Exit Code: %EXIT_CODE%)!
echo [자동 복구] 3초 후 감시기를 자동으로 재시작합니다... (완전 종료: Ctrl+C)
timeout /t 3 /nobreak > nul
goto SUPERVISOR_LOOP
:ON_END
pause
