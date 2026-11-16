@echo off
chcp 65001 >nul
setlocal
set "SCRIPT=%~dp0..\scripts\wb_accounts.py"
echo ==========================================================
echo   WorkBuddy 多账号切换
echo ==========================================================
echo.
echo  重要：请先【完全退出 WorkBuddy】（含右下角托盘图标），
echo        否则切换会被客户端退出时的回写覆盖。
echo.
python "%SCRIPT%" list
echo.
set /p TARGET=输入要切换到的账号 id（直接回车取消）:
if "%TARGET%"=="" goto :end
echo.
python "%SCRIPT%" switch %TARGET%
echo.
echo  完成后重新打开 WorkBuddy 即为该账号。
echo  若结果不对，运行下面这条回滚：
echo    python "%SCRIPT%" rollback
echo.
:end
echo.
pause
endlocal
