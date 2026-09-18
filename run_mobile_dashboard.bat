@echo off
chcp 65001 > nul
title 나무증권 AI 퀀트 - 모바일 대시보드 및 카카오톡 전송
cd /d "C:\Users\DAPCHC-071\namu-auto-trader"
"C:\Users\DAPCHC-071\AppData\Local\Python\bin\python.exe" scripts\start_mobile_dashboard.py
pause
