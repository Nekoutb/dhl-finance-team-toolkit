@echo off
title Finance Team Toolkit
cd /d "%~dp0"
echo ============================================================
echo   Finance Team Toolkit
echo   Restarting local server...
echo   Browser: http://127.0.0.1:8801
echo.
echo   Keep this window open while you use the app.
echo   Close this window (or press Ctrl+C) to stop the server.
echo ============================================================
echo.
rem Stop any instance already using the port, then start fresh.
for /f "tokens=5" %%a in ('netstat -ano ^| findstr ":8801" ^| findstr "LISTENING"') do taskkill /F /PID %%a >nul 2>&1
start "" "http://127.0.0.1:8801"
python -m uvicorn app.main:app --host 127.0.0.1 --port 8801
echo.
echo Server stopped. Press any key to close.
pause >nul
