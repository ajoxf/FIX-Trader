@echo off
setlocal
set "PROJECT_ROOT=%~dp0"
set "VENV_PYTHON=%PROJECT_ROOT%.venv\Scripts\python.exe"

if exist "%VENV_PYTHON%" (
    "%VENV_PYTHON%" "%PROJECT_ROOT%desktop_app.py" %*
) else (
    py -3.11 "%PROJECT_ROOT%desktop_app.py" %*
)

if errorlevel 1 pause
endlocal
