@echo off
chcp 65001 >nul
rem wb-accounts 本地可视化面板：双击启动，会自动打开默认浏览器
rem 浏览器里点「关闭面板」或在此窗口按 Ctrl+C 即可停止服务
python "%~dp0..\scripts\dashboard.py" %*
echo.
echo 面板服务已停止。按任意键关闭本窗口...
pause >nul
