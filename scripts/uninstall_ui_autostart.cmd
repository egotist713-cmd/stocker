@echo off
rem Disable Stocker UI autostart: remove scheduled task "Stocker UI". A running UI keeps running (stop_ui.cmd).
rem Run by double-click.
powershell.exe -NoProfile -NonInteractive -ExecutionPolicy Bypass -File "%~dp0ui_service.ps1" -Action uninstall
pause
