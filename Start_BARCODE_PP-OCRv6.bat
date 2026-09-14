@echo off
chcp 65001 >nul
set "PYTHONUTF8=1"
rem Thread soni va CPU tanlash endi Python skriptining o'zida avtomatik
rem hisoblanadi (P-core/E-core gibrid protsessorni Windows API orqali
rem aniqlab, sekin E-core'larni chetlab o'tadi va shu PC'ning haqiqiy
rem yadro soniga moslashadi) - shuning uchun bu yerda OCR_CPU_THREADS'ni
rem oldindan belgilash shart emas. Kerak bo'lsa qo'lda override qilish
rem uchun shu qatorni oching:
rem set OCR_CPU_THREADS=8

rem By default, result logs are saved on the SAME drive as this program.
rem To keep results on a DIFFERENT drive (e.g. program on C:, results on
rem D:), set the path below. Delete/comment this line to go back to the
rem automatic same-drive default.
if not defined BARCODE_RESULT_BASE set "BARCODE_RESULT_BASE=D:\result\barcode_result"

cd /d "%~dp0"
set "PYTHON_EXE=%~dp0..\.venv_ppocr370\Scripts\python.exe"
if not exist "%PYTHON_EXE%" set "PYTHON_EXE=%~dp0..\..\.venv_ppocr370\Scripts\python.exe"
if not exist "%PYTHON_EXE%" (
    echo [ERROR] .venv_ppocr370 Python topilmadi.
    echo Kutilgan joy: %~dp0..\.venv_ppocr370\Scripts\python.exe
    pause
    exit /b 1
)
"%PYTHON_EXE%" "%~dp0BARCODE_server_OCR_PP-OCRv6.py" --host 127.0.0.1 --port 65433
pause
