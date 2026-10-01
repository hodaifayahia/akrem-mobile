$ErrorActionPreference = "Stop"
$projectRoot = Split-Path -Parent $PSScriptRoot
Set-Location -LiteralPath $projectRoot

$pythonExe = "python"
if (Test-Path ".\.venv-win\Scripts\python.exe") {
    $pythonExe = ".\.venv-win\Scripts\python.exe"
} elseif (Test-Path ".\.venv\Scripts\python.exe") {
    $pythonExe = ".\.venv\Scripts\python.exe"
} elseif (Test-Path ".\venv\Scripts\python.exe") {
    $pythonExe = ".\venv\Scripts\python.exe"
} elseif (Test-Path "$env:LocalAppData\Programs\Python\Python312\python.exe") {
    $pythonExe = "$env:LocalAppData\Programs\Python\Python312\python.exe"
}

Write-Host "Starting AkremMobile Windows Build..." -ForegroundColor Cyan
& $pythonExe scripts\build_exe.py @args

if ($LASTEXITCODE -ne 0) {
    throw "Build script exited with code $LASTEXITCODE."
}
