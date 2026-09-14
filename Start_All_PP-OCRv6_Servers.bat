@echo off
chcp 65001 >nul
set "PYTHONUTF8=1"
cd /d "%~dp0"

set "PYTHON_EXE=%~dp0..\.venv_ppocr370\Scripts\python.exe"
if not exist "%PYTHON_EXE%" set "PYTHON_EXE=%~dp0..\..\.venv_ppocr370\Scripts\python.exe"

if not exist "%PYTHON_EXE%" (
    echo [ERROR] .venv_ppocr370 Python not found.
    echo Expected location: %~dp0..\.venv_ppocr370\Scripts\python.exe
    pause
    exit /b 1
)

"%PYTHON_EXE%" "%~dp0Start_All_PP-OCRv6_Servers.py"
pause
