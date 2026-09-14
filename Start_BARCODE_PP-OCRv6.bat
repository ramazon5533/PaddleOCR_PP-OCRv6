@echo off
chcp 65001 >nul
set "PYTHONUTF8=1"
rem VIN va Barcode serverlar bitta kompyuterda birga ishlaganda CPU'ni
rem teng bo'lib olishlari uchun (ikkalasi mustaqil 90%% so'rasa, jami
rem 180%% oversubscription bo'lib, ikkalasi ham sekinlashadi). Qiymat shu
rem kompyuterning haqiqiy yadro sonidan (%%NUMBER_OF_PROCESSORS%%) hisoblanadi,
rem qattiq yozilgan son emas - boshqa kompyuterda ham to'g'ri ishlaydi.
rem VIN qattiqroq 5s talabga ega bo'lgani uchun toq son qolganda qo'shimcha
rem yadroni VIN oladi (ceil), Barcode esa floor oladi.
if not defined OCR_CPU_THREADS set /a "OCR_CPU_THREADS=NUMBER_OF_PROCESSORS/2"
if "%OCR_CPU_THREADS%"=="0" set "OCR_CPU_THREADS=1"

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
