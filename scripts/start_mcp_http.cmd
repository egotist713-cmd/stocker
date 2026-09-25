@echo off
rem Stocker MCP HTTP for OpenClaw (WSL). Settings and token: .env (STOCKER_MCP_HOST=wsl, STOCKER_MCP_TOKEN).
cd /d "%~dp0.."
if not exist logs mkdir logs
".venv\Scripts\python.exe" -u -m app.service.mcp_http >> "logs\mcp_http.log" 2>&1
