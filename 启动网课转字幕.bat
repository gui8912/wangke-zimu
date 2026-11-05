@echo off
setlocal
cd /d "%~dp0"

set "PY=C:\Users\guiker\.workbuddy\binaries\python\envs\default\Scripts\python.exe"

if exist "%PY%" goto :ok
echo Python not found: %PY%
pause
exit /b 1

:ok
echo.
echo   Drug-Regulatory Course Speech-to-Subtitle
echo   Starting web UI...
echo   URL: http://127.0.0.1:7870  (browser opens automatically)
echo   Close this window to stop the service.
echo.
"%PY%" server.py
pause
