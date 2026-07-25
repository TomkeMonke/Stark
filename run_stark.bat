@echo off
REM Launch Stark using its virtual environment.
cd /d "%~dp0"
".venv\Scripts\python.exe" stark.py %*
