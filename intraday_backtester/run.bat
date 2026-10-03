@echo off
REM Runs a script inside the virtual environment, e.g.  run.bat backtest.py --strategy orb
cd /d "%~dp0"
if not exist .venv\Scripts\python.exe (echo Run setup.bat first & exit /b 1)
.venv\Scripts\python.exe %*
