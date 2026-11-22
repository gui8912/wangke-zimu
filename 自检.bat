@echo off
setlocal
cd /d "%~dp0"
set "PY=C:\Users\guiker\.workbuddy\binaries\python\envs\default\Scripts\python.exe"
if not exist "%PY%" set "PY=python"
echo.
echo ============================================================
echo   Self check: python / deps / models / terms / service
echo ============================================================
"%PY%" tools\selfcheck.py %*
set "RC=%ERRORLEVEL%"
echo.
if not "%RC%"=="0" (
  echo [FAIL] Some required checks failed.
  echo Report: out\_selfcheck.json
  pause
) else (
  echo [OK] All required checks passed.
  echo Report: out\_selfcheck.json
  timeout /t 3 >nul 2>nul
)
endlocal & exit /b %RC%
