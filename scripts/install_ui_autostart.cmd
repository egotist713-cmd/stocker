@echo off
rem Enable Stocker UI autostart: scheduled task "Stocker UI" (at logon, hidden, restart on failure).
rem Run by double-click.
powershell.exe -NoProfile -NonInteractive -ExecutionPolicy Bypass -File "%~dp0ui_service.ps1" -Action install
pause
