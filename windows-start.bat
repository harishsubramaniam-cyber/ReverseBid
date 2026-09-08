@echo off
REM ============================================================
REM  ReverseBid - start the app. Double-click this file.
REM  Leave this window open while you use the app.
REM  Close it (or press Ctrl+C) to stop.
REM ============================================================
cd /d "%~dp0"

where py >nul 2>nul
if %errorlevel%==0 (set PY=py -3) else (set PY=python)

echo.
echo   Starting ReverseBid on http://localhost:8000
echo   Leave this window open. Close it to stop the app.
echo.
start "" http://localhost:8000
%PY% -m uvicorn app.main:app --port 8000
echo.
echo   The app has stopped.
pause
