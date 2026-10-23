@echo off
cd /d "%~dp0"
if exist .venv\Scripts\python.exe goto run
where py >nul 2>nul
if errorlevel 1 (python -m venv .venv) else (py -3 -m venv .venv)
if errorlevel 1 exit /b 1
:run
.venv\Scripts\python.exe bootstrap.py --bind 0.0.0.0 %*
pause
