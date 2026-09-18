import os

python_exe = r"C:\Users\DAPCHC-071\AppData\Local\Python\bin\python.exe"
base_dir = r"C:\Users\DAPCHC-071\namu-auto-trader"

scripts = {
    "run_dual_trader.bat": f'''@echo off
title [통합 듀얼] AI Quant 실시간 자동매매 v9.3 (모의 + 실전 동시 가동)
cd /d "{base_dir}"
echo ======================================================================
echo    [통합 듀얼] AI QUANT 실시간 자동매매 시스템 v9.3 [모의 + 실전 동시]
echo ======================================================================
echo.
echo * 모의 계좌: 50001003032 (NH투자증권 모의투자)
echo * 실전 계좌: 20201549311 (NH투자증권 실전투자)
echo * 통합 감시: KRX 전체 3,136개 상장종목 단 1회 스캔으로 두 계좌 동시 타겟팅
echo * 자금 관리: 계좌별 독립 예수금 및 자산 비례 동적 포지션 사이징 적용
echo * 안전 장치: 실전/모의 독립 서킷브레이커, 일일 손실 한도, 10단계 주문검증
echo.
echo [안내] 통합 듀얼 실시간 자동매매를 시작합니다... (종료: Ctrl + C)
echo.
"{python_exe}" "{base_dir}\\execution\\live_quant_trader.py" --dual
if errorlevel 1 goto ON_ERR
goto ON_END

:ON_ERR
echo.
echo [오류 발생] 통합 듀얼 자동매매 실행 중 문제가 발생했습니다.
pause

:ON_END
''',

    "run_mock_trader.bat": f'''@echo off
title [모의투자] AI Quant 실시간 자동매매 v9.3
cd /d "{base_dir}"
echo ======================================================================
echo    [모의투자] AI QUANT 실시간 자동매매 시스템 v9.3
echo ======================================================================
echo.
echo * 모의 계좌: 50001003032 (NH투자증권 모의투자)
echo * 감시시스템: KRX 전체 3,136개 상장종목 실시간 스캔 및 3단계 게이팅
echo * 안전장치: 모의 주문 및 실시간 체결 시뮬레이션
echo.
echo [안내] 모의투자를 시작합니다... (종료: Ctrl + C)
echo.
"{python_exe}" "{base_dir}\\execution\\live_quant_trader.py" --mock
if errorlevel 1 goto ON_ERR
goto ON_END

:ON_ERR
echo.
echo [오류 발생] 모의투자 실행 중 문제가 발생했습니다.
pause

:ON_END
''',

    "run_live_trader.bat": f'''@echo off
title [실전투자] AI Quant 실시간 자동매매 v9.3
cd /d "{base_dir}"
echo ======================================================================
echo    [실전투자] AI QUANT 실시간 자동매매 시스템 v9.3
echo ======================================================================
echo.
echo * 실전 계좌: 20201549311 (NH투자증권 실전투자)
echo * 감시시스템: KRX 전체 3,136개 상장종목 실시간 스캔 및 3단계 게이팅
echo * 안전장치: 일일 손실 한도, 서킷브레이커, 10단계 주문점검
echo.
echo [안내] 실전투자를 시작합니다... (종료: Ctrl + C)
echo.
"{python_exe}" "{base_dir}\\execution\\live_quant_trader.py" --live
if errorlevel 1 goto ON_ERR
goto ON_END

:ON_ERR
echo.
echo [오류 발생] 실전투자 실행 중 문제가 발생했습니다.
pause

:ON_END
''',

    "run_launcher.bat": f'''@echo off
title AI Quant 자동매매 통합 런처 v9.3
cd /d "{base_dir}"

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
    start "AI Quant 통합 듀얼 매매" "{base_dir}\\run_dual_trader.bat"
    goto MENU
)
if "%opt%"=="2" (
    start "AI Quant 웹 대시보드" "{base_dir}\\run_dashboard.bat"
    goto MENU
)
if "%opt%"=="3" (
    start "AI Quant 모의투자" "{base_dir}\\run_mock_trader.bat"
    goto MENU
)
if "%opt%"=="4" (
    start "AI Quant 실전투자" "{base_dir}\\run_live_trader.bat"
    goto MENU
)
if "%opt%"=="5" (
    cls
    echo [검증] 7대 매수 시나리오 강제 검증을 실행합니다...
    echo.
    "{python_exe}" "{base_dir}\\execution\\live_quant_trader.py" --dual --test-signal
    echo.
    pause
    goto MENU
)
if "%opt%"=="6" (
    cls
    echo [검증] 종합 테스트 하네스(56개 전수 검증)를 실행합니다...
    echo.
    "{python_exe}" "{base_dir}\\tests\\test_harness.py"
    echo.
    pause
    goto MENU
)
if "%opt%"=="7" (
    cls
    echo [검증] 전체 시스템 단위 테스트(99개 전수 검증)를 실행합니다...
    echo.
    "{python_exe}" -m unittest discover -s "{base_dir}\\tests" -p "test_*.py"
    echo.
    pause
    goto MENU
)
if /i "%opt%"=="q" exit /b
goto MENU
'''
}

for fname, text in scripts.items():
    fp = os.path.join(base_dir, fname)
    crlf_text = text.replace("\r\n", "\n").replace("\n", "\r\n")
    with open(fp, "wb") as f:
        f.write(crlf_text.encode("cp949", errors="replace"))
    print(f"Successfully written {fname} in CP949 with CRLF")
