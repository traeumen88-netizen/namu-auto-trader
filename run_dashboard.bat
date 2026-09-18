@echo off
chcp 65001 >nul
title [AI 관제탑] AI Quant 실시간 웹 관제탑 v8.0
cd /d "C:\Users\DAPCHC-071\namu-auto-trader"
echo ======================================================================
echo    AI QUANT 실시간 웹 관제탑 v8.0 실행 중...
echo ======================================================================
echo.
echo * 브라우저 접속 주소: http://127.0.0.1:8080
echo * 종료하려면 이 콘솔 창에서 Ctrl + C 를 누르거나 창을 닫으세요.
echo.
"C:\Users\DAPCHC-071\AppData\Local\Python\bin\python.exe" "C:\Users\DAPCHC-071\namu-auto-trader\web_dashboard.py" --port 8080
if errorlevel 1 (
    echo.
    echo [오류 발생] 웹 관제탑 실행 중 문제가 발생했습니다.
    pause
)
