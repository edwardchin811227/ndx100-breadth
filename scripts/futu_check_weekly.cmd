@echo off
rem Windows Task Scheduler entry point for the weekly Futu cross-check.
set PYTHONUTF8=1
cd /d "%~dp0.."
if not exist reports mkdir reports
python scripts\futu_check.py >> reports\futu_task.log 2>&1
