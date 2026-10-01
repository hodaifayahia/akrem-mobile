@echo off
rem Double-click to build the AkremMobile installer (see build_installer.ps1 for options).
rem Extra arguments are passed through, e.g.:  build_installer.bat -InstallInnoSetup
pushd "%~dp0"
powershell.exe -NoLogo -NoProfile -ExecutionPolicy Bypass -File "%~dp0build_installer.ps1" %*
set "BUILD_EXIT=%ERRORLEVEL%"
popd
if not "%BUILD_EXIT%"=="0" echo. & echo [ERROR] The build failed - see the messages above.
pause
exit /b %BUILD_EXIT%