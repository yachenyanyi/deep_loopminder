@echo off
setlocal
cd /d "%~dp0"

set "PYTHONUTF8=1"
set "PM_PYTHON=C:\miniconda\envs\python312\python.exe"

if not exist "%PM_PYTHON%" (
    echo Python not found: %PM_PYTHON%
    echo Please check the Python environment path in start_pm.bat.
    pause
    exit /b 1
)

echo Starting PM workbench...
echo The workbench opens in your default browser after startup.
echo Press Ctrl+C to stop the server.
echo.
"%PM_PYTHON%" scripts\run_pm_server.py --open-workbench %*
if errorlevel 1 (
    echo.
    echo The server stopped with an error. See the messages above.
    pause
    exit /b 1
)
endlocal
