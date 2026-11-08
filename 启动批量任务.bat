@echo off
setlocal
cd /d "%~dp0"
set "PY=C:/Users/guiker/.workbuddy/binaries/python/envs/default/Scripts/python.exe"
if not exist "%PY%" set "PY=python"
echo ==========================================
echo   Batch add courses to the ASR queue
echo ==========================================
echo.
echo   batch_add "D:/Courses/*.mp4"
echo   batch_add "D:/Courses" --at 22:30
echo   batch_add --status
echo.
"%PY%" batch_add.py %*
echo.
pause
