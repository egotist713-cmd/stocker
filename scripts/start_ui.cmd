@echo off
rem Stocker web UI (production catalog data/prod): http://127.0.0.1:8780/
rem Слушает только 127.0.0.1. Остановить — Ctrl+C.
cd /d "%~dp0.."
set STOCKER_DATA_DIR=data/prod
".venv\Scripts\python.exe" -m app.ui.server
