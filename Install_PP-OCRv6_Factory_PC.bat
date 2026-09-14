@echo off
setlocal EnableExtensions EnableDelayedExpansion
chcp 65001 >nul
title PP-OCRv6 OCR Server - Automated Setup

echo ============================================================
echo  PP-OCRv6 VIN + Barcode OCR Server - Automated Setup (CMD)
echo ============================================================
echo.
echo This script must be run as Administrator, on a fresh PC that
echo already has the PaddleOCR_PP-OCRv6 project files copied into
echo the OCR_System folder (see the installation manual, Step 4).
echo.

rem ---- 0. Require Administrator ----
net session >nul 2>&1
if not %ERRORLEVEL%==0 (
    echo [ERROR] This script must be run as Administrator.
    echo Right-click this file and choose "Run as administrator", then try again.
    echo.
    pause
    exit /b 1
)

rem BASE is resolved from this script's own location (parent of the
rem PaddleOCR_PP-OCRv6 folder), NOT hardcoded to D:. This lets the whole
rem OCR_System tree live on C:, D:, or any other drive without editing
rem this file.
for %%I in ("%~dp0..") do set "BASE=%%~fI"
echo Using OCR_System folder: %BASE%
echo.
set "VENV=%BASE%\.venv_ppocr370"
set "REQ=%BASE%\requirements_ppocr370.txt"
set "TMP=%TEMP%\ppocrv6_setup"
if not exist "%TMP%" mkdir "%TMP%" >nul 2>&1

where curl >nul 2>&1
if errorlevel 1 (
    echo [ERROR] curl.exe was not found on this PC. It ships with Windows 10/11
    echo by default. If it is missing, download Python and VC++ Redistributable
    echo manually using the PDF manual instead of this script.
    pause
    exit /b 1
)

rem ============================================================
rem  1. Windows power plan -^> High performance
rem ============================================================
echo [1/6] Setting Windows power plan to High performance...
powercfg /setactive scheme_min >nul 2>&1
echo   Done.
echo.

rem ============================================================
rem  2. Check that the project files were already copied
rem ============================================================
echo [2/6] Checking project files in %BASE% ...
if not exist "%REQ%" (
    echo [ERROR] Not found: %REQ%
    echo Copy the PaddleOCR_PP-OCRv6 folder, barcode_codes.json and
    echo requirements_ppocr370.txt into %BASE% BEFORE running this script.
    pause
    exit /b 1
)
if not exist "%BASE%\PaddleOCR_PP-OCRv6\VIN_server_OCR_PP-OCRv6.py" (
    echo [ERROR] Not found: %BASE%\PaddleOCR_PP-OCRv6\VIN_server_OCR_PP-OCRv6.py
    echo Copy the PaddleOCR_PP-OCRv6 folder into %BASE% BEFORE running this script.
    pause
    exit /b 1
)
if not exist "%BASE%\PaddleOCR_PP-OCRv6\barcode_codes.json" (
    if exist "%BASE%\barcode_codes.json" (
        echo   NOTE: barcode_codes.json found one folder up ^(%BASE%^) - that still
        echo   works, but copying it into %BASE%\PaddleOCR_PP-OCRv6\ instead is safer
        echo   since it travels with the folder if this PC's files are ever moved.
    ) else (
        echo [WARN] Not found: %BASE%\PaddleOCR_PP-OCRv6\barcode_codes.json
        echo The Barcode server will still run, but its known-code list will be
        echo empty, which makes EVERY detection run all rotation fallbacks
        echo ^(slower, but not incorrect^). Copy barcode_codes.json into
        echo %BASE%\PaddleOCR_PP-OCRv6\ if it is available.
    )
)
echo   OK - project files found.
echo.

rem ============================================================
rem  3. Check / install Python 3.11.8 64-bit
rem ============================================================
echo [3/6] Checking Python 3.11.8 (64-bit)...
set "PYVER="
for /f "tokens=2 delims= " %%v in ('py -3.11 --version 2^>^&1') do set "PYVER=%%v"

if "!PYVER!"=="3.11.8" (
    echo   OK - Python 3.11.8 is already installed. Skipping download.
) else (
    echo   Python 3.11.8 not found ^(found: "!PYVER!"^). Downloading installer...
    curl -L --fail -o "%TMP%\python-3.11.8-amd64.exe" "https://www.python.org/ftp/python/3.11.8/python-3.11.8-amd64.exe"
    if not exist "%TMP%\python-3.11.8-amd64.exe" (
        echo [ERROR] Could not download the Python installer. Check the internet connection.
        pause
        exit /b 1
    )
    echo   Installing Python 3.11.8 silently, for all users ^(this can take a few minutes^)...
    start /wait "" "%TMP%\python-3.11.8-amd64.exe" /quiet InstallAllUsers=1 PrependPath=1 Include_launcher=1 Include_test=0
    echo   Python installation finished.

    set "PYVER="
    for /f "tokens=2 delims= " %%v in ('py -3.11 --version 2^>^&1') do set "PYVER=%%v"
    if not "!PYVER!"=="3.11.8" (
        echo [ERROR] Python 3.11.8 still not detected after install.
        echo Open a NEW terminal window and re-run this script.
        pause
        exit /b 1
    )
)
echo.

rem ============================================================
rem  4. Install Microsoft Visual C++ Redistributable (x64)
rem ============================================================
echo [4/6] Installing Microsoft Visual C++ Redistributable ^(x64^)...
curl -L --fail -o "%TMP%\vc_redist.x64.exe" "https://aka.ms/vs/17/release/vc_redist.x64.exe"
if exist "%TMP%\vc_redist.x64.exe" (
    start /wait "" "%TMP%\vc_redist.x64.exe" /install /quiet /norestart
    echo   Done ^(if it was already installed, this just verified/repaired it^).
) else (
    echo [WARN] Could not download VC++ Redistributable ^(check internet^).
    echo Install it manually later if the servers fail to start.
)
echo.

rem ============================================================
rem  5. Create virtual environment and install packages
rem ============================================================
echo [5/6] Creating virtual environment and installing packages...
if not exist "%VENV%\Scripts\python.exe" (
    py -3.11 -m venv "%VENV%"
)
if not exist "%VENV%\Scripts\python.exe" (
    echo [ERROR] Failed to create the virtual environment at %VENV%.
    pause
    exit /b 1
)
"%VENV%\Scripts\python.exe" -m pip install --upgrade pip
"%VENV%\Scripts\python.exe" -m pip install -r "%REQ%"
if errorlevel 1 (
    echo [ERROR] Package installation failed. Check the internet connection and re-run this script.
    pause
    exit /b 1
)
echo.

rem ============================================================
rem  6. Verify installed versions
rem ============================================================
echo [6/6] Verifying installed versions...
echo.
"%VENV%\Scripts\python.exe" -c "import sys,paddle,paddleocr,paddlex,cv2,numpy; print('Python      ',sys.version.split()[0]); print('PaddlePaddle',paddle.__version__); print('PaddleOCR   ',paddleocr.__version__); print('PaddleX     ',paddlex.__version__); print('OpenCV      ',cv2.__version__); print('NumPy       ',numpy.__version__)"
echo.
echo ============================================================
echo  Setup finished.
echo.
echo  Expected versions:
echo    Python       3.11.8
echo    PaddlePaddle 3.1.1
echo    PaddleOCR    3.7.0
echo    PaddleX      3.7.2
echo    OpenCV       4.10.0
echo    NumPy        2.2.6
echo.
echo  If the versions above match, start the servers with:
echo    %BASE%\PaddleOCR_PP-OCRv6\Start_All_PP-OCRv6_Servers.bat
echo.
echo  The FIRST server start needs internet access to download the
echo  OCR model files. After that, internet is not required.
echo ============================================================
echo.
pause
