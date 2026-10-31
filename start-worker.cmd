@echo off
cd /d "%~dp0"
"%~dp0.venv\Scripts\python.exe" manage.py rqworker default --worker-class rq.SimpleWorker
