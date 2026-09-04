@echo off
chcp 65001 > nul
title [깃허브 업로드] 나무증권 통합 퀀트 자동매매 시스템

echo ======================================================================
echo       [GitHub 업로드 도우미] 나무증권 퀀트 자동매매 시스템
echo ======================================================================
echo.
echo * 보안 보호: .env (API 키 / 계좌번호)는 자동으로 업로드에서 제외됩니다.
echo.
echo 깃허브에서 새 Repository(저장소)를 생성하신 후 해당 주소를 입력해주세요.
echo 예시: https://github.com/traeumen88/namu-auto-trader.git
echo.

set /p REPO_URL="깃허브 저장소 주소(URL)를 입력하세요: "

if "%REPO_URL%"=="" (
    echo.
    echo 주소가 입력되지 않아 취소되었습니다.
    pause
    exit /b
)

echo.
echo 원격 저장소 설정 및 업로드를 시작합니다...
git branch -M main
git remote remove origin >nul 2>&1
git remote add origin %REPO_URL%
git push -u origin main

echo.
echo ======================================================================
echo 업로드가 완료되었습니다!
pause
