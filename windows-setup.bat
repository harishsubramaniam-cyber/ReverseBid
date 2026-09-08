@echo off
REM ============================================================
REM  ReverseBid - one-time setup for Windows.
REM  Double-click this file once, before the first run.
REM ============================================================
cd /d "%~dp0"
echo.
echo   ReverseBid setup
echo   ----------------
echo.

where py >nul 2>nul
if %errorlevel%==0 (set PY=py -3) else (set PY=python)

%PY% --version >nul 2>nul
if errorlevel 1 (
  echo   Python was not found.
  echo.
  echo   Install it from https://www.python.org/downloads/windows/
  echo   and be sure to tick "Add python.exe to PATH" on the first screen.
  echo.
  pause
  exit /b 1
)

echo   Installing the pieces the app needs. This takes a minute...
echo.
%PY% -m pip install --upgrade pip >nul
%PY% -m pip install -r requirements.txt
if errorlevel 1 (
  echo.
  echo   Something went wrong installing. Scroll up to see the message.
  pause
  exit /b 1
)

echo.
echo   Creating the demo data...
%PY% seed.py

echo.
echo   Setup finished. Now double-click windows-start.bat to run the app.
echo.
pause
