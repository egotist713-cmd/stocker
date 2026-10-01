@echo off
rem Stop Stocker UI (port 8780). MCP (8765) and n8n are not touched.
powershell.exe -NoProfile -NonInteractive -ExecutionPolicy Bypass -File "%~dp0ui_service.ps1" -Action stop
