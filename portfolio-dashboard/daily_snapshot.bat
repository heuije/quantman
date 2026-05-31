@echo off
chcp 65001 >nul
cd /d "%~dp0"
set PYTHONIOENCODING=utf-8
"C:\Users\heuij\AppData\Local\Python\pythoncore-3.14-64\python.exe" snapshot.py >> snapshot_log.txt 2>&1
