@echo off
chcp 65001 >nul
rem wb-accounts CLI 入口（Windows）
python "%~dp0..\scripts\wb_accounts.py" %*
