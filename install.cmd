@echo off
setlocal
chcp 65001 >nul
echo.
echo wb-accounts installer (double-click to run)
echo.
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0install.ps1" %*
set RC=%ERRORLEVEL%
echo.
if not "%RC%"=="0" echo [FAILED] exit code %RC%
pause
exit /b %RC%
