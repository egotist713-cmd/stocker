@echo off
rem Stocker web UI (production catalog data/prod): http://127.0.0.1:8780/
rem Listens on 127.0.0.1 only. Stop: Ctrl+C here, or scripts\stop_ui.cmd.
rem Without a terminal window: scripts\start_ui_hidden.vbs (log: data\prod\logs\ui.log).
cd /d "%~dp0.."
set STOCKER_DATA_DIR=data/prod
".venv\Scripts\python.exe" -m app.ui.server
