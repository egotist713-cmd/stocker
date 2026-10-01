@echo off
rem Stocker MCP HTTP for OpenClaw (WSL) and n8n (/api/v1). Settings and token: .env (STOCKER_MCP_HOST=wsl, STOCKER_MCP_TOKEN).
rem Live loop works on the production catalog (passport 35ZZZP): data/prod (incoming, DB, exports).
rem The test catalog (data/) is used only manually: CLI without STOCKER_DATA_DIR, pytest.
cd /d "%~dp0.."
if not exist logs mkdir logs
set STOCKER_DATA_DIR=data/prod
".venv\Scripts\python.exe" -u -m app.service.mcp_http >> "logs\mcp_http.log" 2>&1
