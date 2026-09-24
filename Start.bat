@echo off
setlocal
cd /d "%~dp0"
set "PYTHONUTF8=1"
if not exist .venv\Scripts\python.exe (
  py -3 -c "import sys; sys.exit(0 if sys.version_info >= (3,11) else 'Python 3.11+ required')"
  if errorlevel 1 goto fail
  py -3 -m venv .venv
  if errorlevel 1 goto fail
)
set "REQUIREMENTS_HASH="
for /f "usebackq delims=" %%H in (`.venv\Scripts\python -c "import hashlib; print(hashlib.sha256(open('requirements.txt','rb').read()).hexdigest())"`) do set "REQUIREMENTS_HASH=%%H"
set "CURRENT_HASH="
if exist .venv\.dependencies-ready set /p "CURRENT_HASH="<.venv\.dependencies-ready
if not "%REQUIREMENTS_HASH%"=="%CURRENT_HASH%" (
  .venv\Scripts\python -m pip install -r requirements.txt
  if errorlevel 1 goto fail
  > .venv\.dependencies-ready echo %REQUIREMENTS_HASH%
)
.venv\Scripts\python gui.py
if errorlevel 1 goto fail
exit /b 0
:fail
pause
exit /b 1
