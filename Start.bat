@echo off
setlocal
cd /d "%~dp0"
set "PYTHONUTF8=1"
if not exist requirements.txt (
  echo requirements.txt was not found. Run Start.bat from the project root.
  goto fail
)
if exist .venv\Scripts\python.exe (
  .venv\Scripts\python -c "import sys; sys.exit(0 if sys.version_info >= (3,11) else 1)"
  if errorlevel 1 (
    echo The existing .venv uses an unsupported Python. Remove only .venv and run Start.bat again.
    goto fail
  )
) else (
  call :create_venv
  if errorlevel 1 (
    echo Could not create .venv. Check folder permissions and Python installation.
    goto fail
  )
)
set "REQUIREMENTS_HASH="
for /f "usebackq delims=" %%H in (`.venv\Scripts\python -c "import hashlib; print(hashlib.sha256(open('requirements.txt','rb').read()).hexdigest())"`) do set "REQUIREMENTS_HASH=%%H"
set "CURRENT_HASH="
if exist .venv\.dependencies-ready set /p "CURRENT_HASH="<.venv\.dependencies-ready
if not "%REQUIREMENTS_HASH%"=="%CURRENT_HASH%" (
  .venv\Scripts\python -m pip install --requirement requirements.txt
  if errorlevel 1 (
    echo Could not install dependencies. Check access to the package index and try again.
    goto fail
  )
  > .venv\.dependencies-ready echo %REQUIREMENTS_HASH%
)
.venv\Scripts\python -m proxy_workbench %*
if errorlevel 1 goto fail
exit /b 0
:fail
echo Proxy Workbench could not start.
if "%PROXY_WORKBENCH_PAUSE_ON_ERROR%"=="1" pause
exit /b 1

:create_venv
py -3 -c "import sys; sys.exit(0 if sys.version_info >= (3,11) else 1)" >nul 2>nul
if not errorlevel 1 (
  py -3 -m venv .venv
  exit /b
)
python -c "import sys; sys.exit(0 if sys.version_info >= (3,11) else 1)" >nul 2>nul
if not errorlevel 1 (
  python -m venv .venv
  exit /b
)
echo Python 3.11+ is required. Install Python and add it to PATH.
exit /b 1
