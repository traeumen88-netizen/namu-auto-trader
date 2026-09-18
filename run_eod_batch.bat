@echo off
chcp 65001 > nul
echo ==============================================================================
echo [NAMU AUTO TRADER] EOD Retrospective Batch (장 마감 반성 및 자기진화 학습)
echo ==============================================================================
echo.

cd /d "%~dp0"

echo [1/4] 의사결정 복기 및 데이터 라벨링...
echo [2/4] 어제 생성된 Shadow 모델 성과 검증...
echo [3/4] 실전 Champion 모델 승격 심사...
echo [4/4] Purged Time-Series CV 기반 신규 Challenger 학습...
echo.

"C:\Users\DAPCHC-071\AppData\Local\Python\bin\python.exe" ml/eod_worker.py

echo.
echo ==============================================================================
echo EOD Retrospective 작업이 완료되었습니다.
echo ==============================================================================
pause
