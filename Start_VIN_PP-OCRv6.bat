@echo off
chcp 65001 >nul
set "PYTHONUTF8=1"
rem Thread count and CPU selection are now computed automatically inside
rem the Python script itself (it detects a hybrid P-core/E-core CPU via
rem the Windows API, avoids the slow E-cores, and adapts to this PC's
rem real core count) - so OCR_CPU_THREADS does not need to be preset
rem here. To override manually, uncomment the line below:
rem set OCR_CPU_THREADS=8

rem By default, result logs are saved on the SAME drive as this program.
rem To keep results on a DIFFERENT drive (e.g. program on C:, results on
rem D:), set the path below. Delete/comment this line to go back to the
rem automatic same-drive default.
if not defined VIN_RESULT_BASE set "VIN_RESULT_BASE=D:\result\vin_result"

cd /d "%~dp0"
set "PYTHON_EXE=%~dp0..\.venv_ppocr370\Scripts\python.exe"
if not exist "%PYTHON_EXE%" set "PYTHON_EXE=%~dp0..\..\.venv_ppocr370\Scripts\python.exe"
if not exist "%PYTHON_EXE%" (
    echo [ERROR] .venv_ppocr370 Python not found.
    echo Expected location: %~dp0..\.venv_ppocr370\Scripts\python.exe
    pause
    exit /b 1
)
"%PYTHON_EXE%" "%~dp0VIN_server_OCR_PP-OCRv6.py" --host 127.0.0.1 --port 65432
pause
