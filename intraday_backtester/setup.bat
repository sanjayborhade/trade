@echo off
REM One-time setup: creates a virtual environment and installs the dependencies.
cd /d "%~dp0"
where py >nul 2>nul && (set PY=py -3) || (set PY=python)
%PY% -m venv .venv || goto :error
call .venv\Scripts\activate.bat
python -m pip install --upgrade pip
pip install -r requirements.txt || goto :error
python -m pytest -q || echo Some tests failed - please report the output.
echo.
echo Setup complete. Put your CSV files in data\raw then run:  run.bat inspect_data.py
goto :eof
:error
echo SETUP FAILED - see the messages above.
exit /b 1
