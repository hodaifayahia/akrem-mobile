@echo off
setlocal enabledelayedexpansion

title AkremMobile - Build Executable

echo ============================================================
echo   AkremMobile Executable Builder
echo ============================================================
echo.

:: Use pushd instead of cd /d so Windows automatically maps UNC paths (\\wsl.localhost\...) to a temporary drive letter
pushd "%~dp0"

:: 1. Locate Python interpreter (prefer project virtualenv)
set "PYTHON_EXE="

if exist ".venv-win\Scripts\python.exe" (
    set "PYTHON_EXE=.venv-win\Scripts\python.exe"
    echo [*] Using virtual environment: .venv-win
) else if exist ".venv\Scripts\python.exe" (
    set "PYTHON_EXE=.venv\Scripts\python.exe"
    echo [*] Using virtual environment: .venv
) else if exist "venv\Scripts\python.exe" (
    set "PYTHON_EXE=venv\Scripts\python.exe"
    echo [*] Using virtual environment: venv
) else if exist "%LocalAppData%\Programs\Python\Python312\python.exe" (
    set "PYTHON_EXE=%LocalAppData%\Programs\Python\Python312\python.exe"
    echo [*] Using Python 3.12: %LocalAppData%\Programs\Python\Python312\python.exe
) else (
    where py >nul 2>nul
    if !ERRORLEVEL! equ 0 (
        set "PYTHON_EXE=py -3.12"
        echo [*] Using Python launcher: py -3.12
    ) else (
        where python >nul 2>nul
        if !ERRORLEVEL! equ 0 (
            set "PYTHON_EXE=python"
            echo [*] Using system Python
        )
    )
)

if "%PYTHON_EXE%"=="" (
    echo [ERROR] Python was not found!
    echo Please install Python 3.12 or activate your virtual environment.
    pause
    popd
    exit /b 1
)

:: 2. Verify dependencies are installed
%PYTHON_EXE% -c "import PyInstaller, PySide6" >nul 2>nul
if %ERRORLEVEL% neq 0 (
    echo [*] Required dependencies not detected. Installing from requirements.txt...
    %PYTHON_EXE% -m pip install -r requirements.txt
    if %ERRORLEVEL% neq 0 (
        echo [ERROR] Failed to install dependencies.
        pause
        popd
        exit /b 1
    )
)

:: 3. Run build script
echo [*] Launching build script...
echo.
%PYTHON_EXE% scripts\build_exe.py %*

if %ERRORLEVEL% neq 0 (
    echo.
    echo ============================================================
    echo [ERROR] Build failed! Check the error log above.
    echo ============================================================
    pause
    popd
    exit /b %ERRORLEVEL%
)

echo.
echo ============================================================
echo [SUCCESS] Executable built successfully!
echo Files are located in the "dist" folder.
echo ============================================================
echo.
popd
pause
