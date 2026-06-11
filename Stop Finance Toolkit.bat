@echo off
title Stop Finance Team Toolkit
echo Stopping the Finance Team Toolkit server (port 8801)...
for /f "tokens=5" %%a in ('netstat -ano ^| findstr ":8801" ^| findstr "LISTENING"') do taskkill /F /PID %%a >nul 2>&1
echo Done. The app is stopped. Double-click "Start Finance Toolkit.bat" to start it again.
pause
