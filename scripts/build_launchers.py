import os
import win32com.client

PROJECT_DIR = r"C:\Users\DAPCHC-071\namu-auto-trader"
PYTHON_EXE = r"C:\Users\DAPCHC-071\AppData\Local\Python\bin\python.exe"
DESKTOP_DIR = r"C:\Users\DAPCHC-071\Desktop"

dashboard_bat = f"""@echo off
chcp 65001 >nul
title [AI 관제탑] AI Quant 실시간 웹 관제탑 v8.0
cd /d "{PROJECT_DIR}"
echo ======================================================================
echo    AI QUANT 실시간 웹 관제탑 v8.0 실행 중...
echo ======================================================================
echo.
echo * 브라우저 접속 주소: http://127.0.0.1:8080
echo * 종료하려면 이 콘솔 창에서 Ctrl + C 를 누르거나 창을 닫으세요.
echo.
"{PYTHON_EXE}" "{os.path.join(PROJECT_DIR, 'web_dashboard.py')}" --port 8080
if errorlevel 1 (
    echo.
    echo [오류 발생] 웹 관제탑 실행 중 문제가 발생했습니다.
    pause
)
"""

mock_bat = f"""@echo off
chcp 65001 >nul
title [모의투자] AI Quant 실시간 자동매매 v8.0
cd /d "{PROJECT_DIR}"
echo ======================================================================
echo    [모의투자] AI QUANT 실시간 자동매매 시스템 v8.0
echo ======================================================================
echo.
echo * 모의 계좌: 50001003032 (NH투자증권 모의투자)
echo * 감시시스템: KRX 전체 3,136개 상장종목 실시간 스캔 & 3단계 게이팅
echo * 안전장치: 모의 주문 및 실시간 체결 시뮬레이션
echo.
echo [안내] 모의투자를 시작합니다... (종료: Ctrl + C)
echo.
"{PYTHON_EXE}" "{os.path.join(PROJECT_DIR, 'execution', 'live_quant_trader.py')}" --mock
if errorlevel 1 (
    echo.
    echo [오류 발생] 모의투자 실행 중 문제가 발생했습니다.
    pause
)
"""

live_bat = f"""@echo off
chcp 65001 >nul
title [실전투자] AI Quant 실시간 자동매매 v8.0
cd /d "{PROJECT_DIR}"
echo ======================================================================
echo    [실전투자] AI QUANT 실시간 자동매매 시스템 v8.0
echo ======================================================================
echo.
echo * 실전 계좌: 20201549311 (NH투자증권 실전투자)
echo * 감시시스템: KRX 전체 3,136개 상장종목 실시간 스캔 & 3단계 게이팅
echo * 안전장치: 일일 손실 한도, 서킷브레이커, 10단계 주문점검
echo.
echo [안내] 실전투자를 시작합니다... (종료: Ctrl + C)
echo.
"{PYTHON_EXE}" "{os.path.join(PROJECT_DIR, 'execution', 'live_quant_trader.py')}" --live
if errorlevel 1 (
    echo.
    echo [오류 발생] 실전투자 실행 중 문제가 발생했습니다.
    pause
)
"""

launcher_bat = f"""@echo off
chcp 65001 >nul
title AI Quant 자동매매 통합 런처 v8.0
cd /d "{PROJECT_DIR}"

:MENU
cls
echo ======================================================================
echo         AI QUANT 국내 주식 실시간 자동매매 통합 런처 v8.0
echo ======================================================================
echo.
echo   [1] 실시간 웹 관제탑 (웹 대시보드 http://127.0.0.1:8080)
echo   [2] 모의투자 실시간 자동매매 (계좌: 50001003032)
echo   [3] 실전투자 실시간 자동매매 (계좌: 20201549311)
echo   [4] 5대 매수 시나리오 강제 검증 (FORCE_SIGNAL_TEST)
echo   [5] 전체 시스템 단위 테스트 실행 (61개 검증)
echo   [Q] 종료
echo.
echo ======================================================================
set /p opt=실행할 번호를 입력하세요 (1-5, Q): 

if "%opt%"=="1" (
    start "AI Quant 웹 대시보드" "{os.path.join(PROJECT_DIR, 'run_dashboard.bat')}"
    goto MENU
)
if "%opt%"=="2" (
    start "AI Quant 모의투자" "{os.path.join(PROJECT_DIR, 'run_mock_trader.bat')}"
    goto MENU
)
if "%opt%"=="3" (
    start "AI Quant 실전투자" "{os.path.join(PROJECT_DIR, 'run_live_trader.bat')}"
    goto MENU
)
if "%opt%"=="4" (
    cls
    echo [검증] 5대 매수 시나리오 강제 검증을 실행합니다...
    echo.
    "{PYTHON_EXE}" "{os.path.join(PROJECT_DIR, 'execution', 'live_quant_trader.py')}" --mock --test-signal
    echo.
    pause
    goto MENU
)
if "%opt%"=="5" (
    cls
    echo [검증] 전체 시스템 단위 테스트를 실행합니다...
    echo.
    "{PYTHON_EXE}" -m unittest discover -s "{os.path.join(PROJECT_DIR, 'tests')}" -p "test_*.py"
    echo.
    pause
    goto MENU
)
if /i "%opt%"=="q" exit /b
goto MENU
"""

files = {
    "run_dashboard.bat": dashboard_bat,
    "run_mock_trader.bat": mock_bat,
    "run_live_trader.bat": live_bat,
    "run_launcher.bat": launcher_bat
}

for name, content in files.items():
    fpath = os.path.join(PROJECT_DIR, name)
    crlf_content = content.replace("\r\n", "\n").replace("\n", "\r\n")
    with open(fpath, "wb") as f:
        f.write(crlf_content.encode("utf-8"))
    print("Wrote UTF-8 with CRLF:", name)

# Update Desktop Shortcuts
shell = win32com.client.Dispatch("WScript.Shell")
shortcuts = [
    ("AI_Quant_Launcher.lnk", "run_launcher.bat", "AI QUANT 국내 주식 실시간 자동매매 통합 런처 v8.0"),
    ("AI_Quant_Dashboard.lnk", "run_dashboard.bat", "AI QUANT 실시간 웹 관제탑 (http://127.0.0.1:8080)"),
    ("AI_Quant_Mock_Trader.lnk", "run_mock_trader.bat", "AI QUANT 실시간 모의투자 자동매매 v8.0"),
    ("AI_Quant_Live_Trader.lnk", "run_live_trader.bat", "AI QUANT 실시간 실전투자 자동매매 v8.0"),
]

for name, target_bat, desc in shortcuts:
    lnk_path = os.path.join(DESKTOP_DIR, name)
    sc = shell.CreateShortcut(lnk_path)
    sc.TargetPath = os.path.join(PROJECT_DIR, target_bat)
    sc.WorkingDirectory = PROJECT_DIR
    sc.Description = desc
    sc.Save()
    print("Created Desktop Shortcut:", name, "->", target_bat)

print("\nAll launchers and desktop shortcuts generated successfully!")
