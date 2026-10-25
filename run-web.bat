@echo off
rem First run: installs packages into .venv, then starts the app at http://localhost:8000
cd /d %~dp0
if not exist .venv (
  py -3 -m venv .venv || python -m venv .venv
  .venv\Scripts\python -m pip install -r requirements.txt
)
start "" http://localhost:8000
.venv\Scripts\python -m uvicorn ops_agent.web.app:app --port 8000
