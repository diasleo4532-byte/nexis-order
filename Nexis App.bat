@echo off
setlocal
cd /d "%~dp0"

set "PYW=%~dp0.venv\Scripts\pythonw.exe"
set "PY=%~dp0.venv\Scripts\python.exe"

if exist "%PYW%" (
  start "" "%PYW%" -m nexis.app
  exit /b 0
)

if exist "%PY%" (
  "%PY%" -m nexis.app
  exit /b 0
)

echo Nexis is not set up yet. Run this once from this folder:
echo   py -m venv .venv
echo   .venv\Scripts\python -m pip install -e ".[app,voice,whisper]"
pause
exit /b 1
