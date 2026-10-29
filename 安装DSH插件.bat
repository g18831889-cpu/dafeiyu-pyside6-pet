@echo off
chcp 65001 >nul
rem 用 -File 调用同名 .ps1；中文文件名在 UTF-8(BOM) 的 bat 下可被正确传递
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0安装DSH插件.ps1"
pause
