@echo off
setlocal

set "PY=C:\Users\guiker\.workbuddy\binaries\python\envs\default\Scripts\python.exe"
set "HERE=%~dp0"

if exist "%PY%" goto :pyok
echo Python not found: %PY%
pause
exit /b 1

:pyok
if "%~1"=="" (
  echo.
  echo   Drug-Regulatory Course Transcript Tool
  echo   ----------------------------------------
  echo   Usage: drag an audio/video file onto this bat
  echo           or:  transcribe.bat "D:\course\lecture01.mp4"
  echo.
  pause
  exit /b 0
)

echo Processing: %~1
echo.
"%PY%" "%HERE%transcribe.py" -i "%~1" --clean

echo.
echo Done. Output in %HERE%out\
pause
