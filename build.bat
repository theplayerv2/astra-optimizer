@echo off
setlocal EnableExtensions EnableDelayedExpansion
title Astra Optimizer - Build
cd /d "%~dp0"

set "PROJECT_DIR=%~dp0"
set "VENV_DIR=%PROJECT_DIR%.venv"
set "VENV_PY=%VENV_DIR%\Scripts\python.exe"
set "SOURCE=%PROJECT_DIR%optimizer.py"
set "DIST_DIR=%PROJECT_DIR%dist"
set "WORK_DIR=%PROJECT_DIR%build"
set "PY="

echo ============================================================
echo   Astra Optimizer build  ^|  "%PROJECT_DIR%"
echo ============================================================

if not exist "%SOURCE%" (
    echo [ERROR] optimizer.py not found next to build.bat.
    goto :fail
)

rem ---------------------------------------------------------------
rem 1. Locate a real Python 3.9+ interpreter (ignore the Store stub)
rem ---------------------------------------------------------------
call :find_python
if not defined PY (
    echo [INFO] Python 3.9+ not found. Installing Python 3.12 with winget...
    where winget >nul 2>&1
    if errorlevel 1 (
        echo [ERROR] winget is unavailable. Install Python 3.12 from https://www.python.org/downloads/ and re-run.
        goto :fail
    )
    winget install --id Python.Python.3.12 --exact --source winget --scope user --silent --accept-package-agreements --accept-source-agreements
    call :find_python
)
if not defined PY (
    echo [ERROR] Python installation could not be located. Install Python 3.12 manually and re-run.
    goto :fail
)
echo [OK] Using Python: "%PY%"

rem ---------------------------------------------------------------
rem 2. Isolated virtual environment + dependencies
rem ---------------------------------------------------------------
if not exist "%VENV_PY%" (
    echo [INFO] Creating virtual environment in "%VENV_DIR%"...
    "%PY%" -m venv "%VENV_DIR%"
    if errorlevel 1 (
        echo [ERROR] Failed to create the virtual environment.
        goto :fail
    )
)

echo [INFO] Installing dependencies...
"%VENV_PY%" -m pip install --disable-pip-version-check --upgrade pip
if errorlevel 1 goto :pipfail
"%VENV_PY%" -m pip install --disable-pip-version-check --upgrade -r "%PROJECT_DIR%requirements.txt" "pyinstaller>=6.6"
if errorlevel 1 goto :pipfail

rem ---------------------------------------------------------------
rem 3. Syntax check, then compile the standalone executable
rem ---------------------------------------------------------------
"%VENV_PY%" -m py_compile "%SOURCE%"
if errorlevel 1 (
    echo [ERROR] optimizer.py failed the syntax check.
    goto :fail
)

echo [INFO] Rendering the app icon...
"%VENV_PY%" "%SOURCE%" --write-icon "%PROJECT_DIR%assets"
if errorlevel 1 (
    echo [ERROR] Could not render assets\icon.ico.
    goto :fail
)

echo [INFO] Building "Astra Optimizer.exe" with PyInstaller...
"%VENV_PY%" -m PyInstaller ^
    --noconfirm ^
    --clean ^
    --onefile ^
    --noconsole ^
    --uac-admin ^
    --name "Astra Optimizer" ^
    --icon "%PROJECT_DIR%assets\icon.ico" ^
    --add-data "%PROJECT_DIR%assets;assets" ^
    --collect-all customtkinter ^
    --hidden-import pystray._win32 ^
    --distpath "%DIST_DIR%" ^
    --workpath "%WORK_DIR%" ^
    --specpath "%WORK_DIR%" ^
    "%SOURCE%"
if errorlevel 1 (
    echo [ERROR] PyInstaller build failed. See the output above.
    goto :fail
)

if not exist "%DIST_DIR%\Astra Optimizer.exe" (
    echo [ERROR] Build finished but "%DIST_DIR%\Astra Optimizer.exe" is missing.
    goto :fail
)

echo.
echo ============================================================
echo   SUCCESS: "%DIST_DIR%\Astra Optimizer.exe"
echo ============================================================
if /i not "%~1"=="--nopause" pause
endlocal
exit /b 0

rem ---------------------------------------------------------------
:find_python
set "PY="
for /f "usebackq delims=" %%P in (`py -3 -c "import sys;print(sys.executable) if sys.version_info>=(3,9) else None" 2^>nul`) do set "PY=%%P"
if defined PY if not exist "!PY!" set "PY="
if not defined PY (
    for /f "usebackq delims=" %%P in (`python -c "import sys;print(sys.executable) if sys.version_info>=(3,9) else None" 2^>nul`) do set "PY=%%P"
    if defined PY if not exist "!PY!" set "PY="
)
if not defined PY (
    for %%V in (313 312 311 310 39) do (
        if not defined PY if exist "%LOCALAPPDATA%\Programs\Python\Python%%V\python.exe" set "PY=%LOCALAPPDATA%\Programs\Python\Python%%V\python.exe"
        if not defined PY if exist "%ProgramFiles%\Python%%V\python.exe" set "PY=%ProgramFiles%\Python%%V\python.exe"
    )
)
exit /b 0

:pipfail
echo [ERROR] Dependency installation failed. Check your internet connection and re-run.
:fail
echo.
echo Build aborted.
if /i not "%~1"=="--nopause" pause
endlocal
exit /b 1
