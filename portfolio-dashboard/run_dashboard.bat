@echo off
chcp 65001 >nul
cd /d "%~dp0"
echo ====================================================
echo   포트폴리오 대시보드를 시작합니다...
echo   브라우저가 자동으로 열립니다. 종료하려면 이 창에서 Ctrl+C
echo ====================================================
set PYTHONIOENCODING=utf-8
python -m streamlit run app.py
pause
