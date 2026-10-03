@echo off
REM Usage: run.bat ^<command^> [csv] [options]   e.g. run.bat backtest data.csv --instrument nifty
cd /d "%~dp0"
python tools\intraday_lab.py %*
