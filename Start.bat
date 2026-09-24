@echo off
cd /d "%~dp0"
if not exist .venv\Scripts\python.exe (
  py -3 -c "import sys; sys.exit(0 if sys.version_info >= (3,11) else 'Python 3.11+ required')"
  if errorlevel 1 goto fail
  py -3 -m venv .venv
  if errorlevel 1 goto fail
)
if not exist .venv\.dependencies-ready (
  .venv\Scripts\python -m pip install -r requirements.txt
  if errorlevel 1 goto fail
  type nul > .venv\.dependencies-ready
)
.venv\Scripts\python gui.py
if errorlevel 1 goto fail
exit /b 0
:fail
pause
exit /b 1
