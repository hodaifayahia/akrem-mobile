<#
.SYNOPSIS
    Build the AkremMobile Windows installer from a clean checkout, in one go.

.DESCRIPTION
    1. Finds Python 3.12 and creates/updates the build virtual environment (.venv-build).
    2. Installs requirements.txt (PySide6, PyInstaller, ...).
    3. Runs the test suite (skip with -SkipTests).
    4. Builds the one-folder app with PyInstaller (AkremMobile.spec) and a portable ZIP.
    5. Runs the packaged app's self-test: it opens the real window and clicks every
       sidebar button with the mouse, so an app that installs but cannot be clicked
       is caught here (skip with -SkipSelfTest).
    6. Compiles the Inno Setup installer (installs Inno Setup with winget when
       -InstallInnoSetup is given); without Inno Setup it falls back to the
       built-in Windows IExpress packager.
    7. Prints the installer path and its SHA-256 checksum.

.EXAMPLE
    .\build_installer.ps1
.EXAMPLE
    .\build_installer.ps1 -InstallInnoSetup
.EXAMPLE
    .\build_installer.ps1 -SkipTests -Python "C:\Python312\python.exe"
#>
[CmdletBinding()]
param(
    [switch]$SkipTests,
    [switch]$SkipSelfTest,
    [switch]$InstallInnoSetup,
    [string]$Python = ""
)

$ErrorActionPreference = "Stop"
$ProjectRoot = $PSScriptRoot
Set-Location -LiteralPath $ProjectRoot
$VenvDir = Join-Path $ProjectRoot ".venv-build"
$VenvPython = Join-Path $VenvDir "Scripts\python.exe"
$DistDir = Join-Path $ProjectRoot "dist"
$AppExe = Join-Path $DistDir "AkremMobile\AkremMobile.exe"
$IssFile = Join-Path $ProjectRoot "installer\AkremMobile.iss"

function Write-Step([string]$Text) {
    Write-Host ""
    Write-Host ("=" * 64) -ForegroundColor DarkCyan
    Write-Host "  $Text" -ForegroundColor Cyan
    Write-Host ("=" * 64) -ForegroundColor DarkCyan
}

function Invoke-Checked([string]$FilePath, [string[]]$Arguments, [string]$What) {
    & $FilePath @Arguments
    if ($LASTEXITCODE -ne 0) {
        throw "$What failed (exit code $LASTEXITCODE)."
    }
}

function Get-AppVersion {
    $config = Get-Content -LiteralPath (Join-Path $ProjectRoot "app\config.py") -Raw
    $match = [regex]::Match($config, 'APP_VERSION\s*=\s*["'']([^"'']+)["'']')
    if ($match.Success) { return $match.Groups[1].Value }
    return "0.1.0"
}

function Find-Python312 {
    $candidates = @()
    if ($Python) { $candidates += $Python }
    $launcher = Get-Command py -ErrorAction SilentlyContinue
    if ($launcher) {
        $fromLauncher = & py -3.12 -c "import sys; print(sys.executable)" 2>$null
        if ($LASTEXITCODE -eq 0 -and $fromLauncher) { $candidates += $fromLauncher.Trim() }
    }
    $candidates += (Join-Path $env:LOCALAPPDATA "Programs\Python\Python312\python.exe")
    $candidates += "C:\Program Files\Python312\python.exe"
    $onPath = Get-Command python -ErrorAction SilentlyContinue
    if ($onPath) { $candidates += $onPath.Source }
    foreach ($candidate in $candidates) {
        if (-not $candidate -or -not (Test-Path -LiteralPath $candidate)) { continue }
        $version = & $candidate -c "import sys; print('%d.%d' % sys.version_info[:2])" 2>$null
        if ($LASTEXITCODE -eq 0 -and $version.Trim() -eq "3.12") { return $candidate }
    }
    throw "Python 3.12 was not found. Install it from https://www.python.org/downloads/ (tick 'Add python.exe to PATH') or pass -Python <path to python.exe>."
}

function Find-InnoSetup {
    $onPath = Get-Command ISCC.exe -ErrorAction SilentlyContinue
    if ($onPath) { return $onPath.Source }
    $roots = @(${env:ProgramFiles(x86)}, $env:ProgramFiles, (Join-Path $env:LOCALAPPDATA "Programs"))
    foreach ($root in $roots) {
        if (-not $root) { continue }
        foreach ($folder in @("Inno Setup 6", "Inno Setup 5")) {
            $candidate = Join-Path $root "$folder\ISCC.exe"
            if (Test-Path -LiteralPath $candidate) { return $candidate }
        }
    }
    return $null
}

$Version = Get-AppVersion
Write-Step "AkremMobile $Version - full installer build"

# 1. Python + build environment --------------------------------------------
Write-Step "1/6 Python 3.12 and the build environment"
$BasePython = Find-Python312
Write-Host "Python: $BasePython"
if (-not (Test-Path -LiteralPath $VenvPython)) {
    Invoke-Checked $BasePython @("-m", "venv", $VenvDir) "Creating the virtual environment"
}
Invoke-Checked $VenvPython @("-m", "pip", "install", "--upgrade", "pip") "Upgrading pip"
Invoke-Checked $VenvPython @("-m", "pip", "install", "-r", (Join-Path $ProjectRoot "requirements.txt")) "Installing requirements"

# 2. Tests -------------------------------------------------------------------
if ($SkipTests) {
    Write-Step "2/6 Tests skipped (-SkipTests)"
} else {
    Write-Step "2/6 Running the test suite"
    $env:QT_QPA_PLATFORM = "offscreen"
    try {
        Invoke-Checked $VenvPython @("-m", "pytest", "-q", "-p", "no:cacheprovider") "The test suite"
    } finally {
        Remove-Item Env:QT_QPA_PLATFORM -ErrorAction SilentlyContinue
    }
}

# 3. Executable ----------------------------------------------------------------
Write-Step "3/6 Building the application with PyInstaller"
Get-Process -Name "AkremMobile" -ErrorAction SilentlyContinue | Stop-Process -Force
Invoke-Checked $VenvPython @((Join-Path $ProjectRoot "scripts\build_exe.py"), "--skip-installer") "PyInstaller build"
if (-not (Test-Path -LiteralPath $AppExe)) {
    throw "The build finished but $AppExe is missing."
}

# 4. Self-test of the packaged app -----------------------------------------------
if ($SkipSelfTest) {
    Write-Step "4/6 Self-test skipped (-SkipSelfTest)"
} else {
    Write-Step "4/6 Self-test: open the packaged app and click every sidebar button"
    $report = Join-Path $DistDir "selftest-report.json"
    Remove-Item -LiteralPath $report -ErrorAction SilentlyContinue
    $process = Start-Process -FilePath $AppExe -ArgumentList @("--self-test", "`"$report`"") -Wait -PassThru
    if (Test-Path -LiteralPath $report) {
        Get-Content -LiteralPath $report | Write-Host
    }
    if ($process.ExitCode -ne 0) {
        throw "The packaged app failed its self-test (exit code $($process.ExitCode)). See $report and %APPDATA%\AkremMobile\logs\akremmobile.log."
    }
    Write-Host "Self-test passed." -ForegroundColor Green
}

# 5. Installer -------------------------------------------------------------------
Write-Step "5/6 Building the installer"
$Iscc = Find-InnoSetup
if (-not $Iscc -and $InstallInnoSetup) {
    $winget = Get-Command winget -ErrorAction SilentlyContinue
    if (-not $winget) { throw "winget is not available; install Inno Setup 6 from https://jrsoftware.org/isdl.php" }
    Invoke-Checked "winget" @("install", "--id", "JRSoftware.InnoSetup", "-e", "--silent",
        "--accept-package-agreements", "--accept-source-agreements") "Installing Inno Setup"
    $Iscc = Find-InnoSetup
}
$InstallerDir = Join-Path $DistDir "installer"
New-Item -ItemType Directory -Path $InstallerDir -Force | Out-Null
if ($Iscc) {
    Write-Host "Inno Setup: $Iscc"
    Invoke-Checked $Iscc @("/Q", "/DAppVersion=$Version", $IssFile) "Inno Setup compilation"
    $Installer = Join-Path $InstallerDir "AkremMobile-Setup-$Version.exe"
} else {
    Write-Host "Inno Setup 6 was not found (re-run with -InstallInnoSetup to install it)." -ForegroundColor Yellow
    Write-Host "Falling back to the Windows IExpress packager." -ForegroundColor Yellow
    & (Join-Path $ProjectRoot "scripts\package_windows.ps1")
    $Installer = Join-Path $InstallerDir "AkremMobile-Setup-$Version.exe"
}
if (-not (Test-Path -LiteralPath $Installer)) {
    throw "The installer was not produced: $Installer"
}

# 6. Summary ---------------------------------------------------------------------
Write-Step "6/6 Done"
$hash = (Get-FileHash -LiteralPath $Installer -Algorithm SHA256).Hash
$sizeMb = [math]::Round((Get-Item -LiteralPath $Installer).Length / 1MB, 1)
Write-Host "Installer : $Installer ($sizeMb MB)" -ForegroundColor Green
Write-Host "SHA-256   : $hash"
Write-Host "App folder: $(Split-Path -Parent $AppExe)"
Write-Host ""
Write-Host "Updating a shop PC: run the new installer. It closes any running copy"
Write-Host "(including one hidden in the tray) and keeps the data in %APPDATA%\AkremMobile."
