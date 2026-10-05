<#
.SYNOPSIS
    Make this PC the AkremMobile shop server (PostgreSQL) that the other shop PCs connect to.

.DESCRIPTION
    Run once, on the main shop PC: Start menu > AkremMobile > "Make this PC the shop
    server" (installed with the app), or in PowerShell:

        powershell -ExecutionPolicy Bypass -File setup_shop_server.ps1

    It asks Windows for administrator rights by itself.

    It:
      1. installs PostgreSQL 16 with winget when it is not installed yet;
      2. creates the "akrem" login and the "akremmobile" database (UTF-8);
      3. accepts connections from the shop's local network only ("samenet"),
         with password checking (scram-sha-256);
      4. opens TCP port 5432 in the Windows firewall for private networks;
      5. prints this PC's address and the database password to type in
         AkremMobile on every PC: Settings > Share the records between several PCs.

    Running it again is safe: existing users, databases and rules are kept,
    and -ResetPassword gives the "akrem" login a new password.

    Keep the printed password somewhere safe. This PC must be switched on
    while the other PCs work.
#>
[CmdletBinding()]
param(
    [string]$AppPassword = "",
    [int]$Port = 5432,
    [switch]$ResetPassword
)

$ErrorActionPreference = "Stop"
$Database = "akremmobile"
$AppUser = "akrem"

function Write-Step([string]$text) { Write-Host "`n== $text" -ForegroundColor Cyan }

function New-Password([int]$length = 20) {
    $chars = "ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnpqrstuvwxyz23456789"
    $bytes = New-Object byte[] $length
    [System.Security.Cryptography.RandomNumberGenerator]::Create().GetBytes($bytes)
    -join ($bytes | ForEach-Object { $chars[$_ % $chars.Length] })
}

function Find-PostgresBin {
    $roots = @("$env:ProgramFiles\PostgreSQL", "${env:ProgramFiles(x86)}\PostgreSQL") | Where-Object { $_ -and (Test-Path $_) }
    foreach ($root in $roots) {
        $bin = Get-ChildItem $root -Directory | Sort-Object { [int]($_.Name -replace '\D', '0') } -Descending |
            ForEach-Object { Join-Path $_.FullName "bin" } | Where-Object { Test-Path (Join-Path $_ "psql.exe") } |
            Select-Object -First 1
        if ($bin) { return $bin }
    }
    return $null
}

function Invoke-Psql([string]$bin, [string]$sql, [string]$superPassword, [string]$db = "postgres") {
    # psql writes notices to stderr; in Windows PowerShell 5.1 that must not stop the script.
    $previous = $ErrorActionPreference
    $ErrorActionPreference = "Continue"
    $env:PGPASSWORD = $superPassword
    try {
        $output = & (Join-Path $bin "psql.exe") -h 127.0.0.1 -p $Port -U postgres -d $db -v ON_ERROR_STOP=1 -tAc $sql
        if ($LASTEXITCODE -ne 0) { throw "psql failed (exit code $LASTEXITCODE) running: $sql" }
        return ($output | Out-String).Trim()
    } finally {
        Remove-Item Env:\PGPASSWORD -ErrorAction SilentlyContinue
        $ErrorActionPreference = $previous
    }
}

# ------------------------------------------------------------------ administrator
$principal = New-Object Security.Principal.WindowsPrincipal([Security.Principal.WindowsIdentity]::GetCurrent())
if (-not $principal.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)) {
    # Ask Windows for administrator rights and run again in a window that stays open.
    $arguments = @("-NoExit", "-ExecutionPolicy", "Bypass", "-File", "`"$PSCommandPath`"", "-Port", $Port)
    if ($ResetPassword) { $arguments += "-ResetPassword" }
    if ($AppPassword) { $arguments += @("-AppPassword", "`"$AppPassword`"") }
    try {
        Start-Process powershell.exe -Verb RunAs -ArgumentList $arguments
    } catch {
        Write-Host "Administrator rights are needed: right click PowerShell > Run as administrator, then run this script." -ForegroundColor Red
    }
    exit
}

# ------------------------------------------------------------------ 1. PostgreSQL
Write-Step "1/5 PostgreSQL"
$bin = Find-PostgresBin
$superPassword = $null
if (-not $bin) {
    if (-not (Get-Command winget -ErrorAction SilentlyContinue)) {
        Write-Host "PostgreSQL is not installed and winget is missing. Install PostgreSQL 16 from https://www.postgresql.org/download/windows/ then run this script again." -ForegroundColor Red
        exit 1
    }
    $superPassword = New-Password
    Write-Host "Installing PostgreSQL 16 (this takes a few minutes)..."
    winget install --id PostgreSQL.PostgreSQL.16 -e --accept-package-agreements --accept-source-agreements `
        --override "--mode unattended --unattendedmodeui none --superpassword $superPassword --serverport $Port"
    $bin = Find-PostgresBin
    if (-not $bin) { Write-Host "PostgreSQL did not install correctly." -ForegroundColor Red; exit 1 }
    Write-Host "PostgreSQL administrator (postgres) password: $superPassword" -ForegroundColor Yellow
} else {
    Write-Host "Found PostgreSQL in $bin"
    $secure = Read-Host "Password of the PostgreSQL administrator account 'postgres'" -AsSecureString
    $superPassword = [Runtime.InteropServices.Marshal]::PtrToStringAuto([Runtime.InteropServices.Marshal]::SecureStringToBSTR($secure))
}
$dataDir = Split-Path $bin -Parent | Join-Path -ChildPath "data"
$service = Get-Service | Where-Object { $_.Name -like "postgresql*" } | Select-Object -First 1
if ($service -and $service.Status -ne "Running") { Start-Service $service.Name }

# ------------------------------------------------------------------ 2. login and database
Write-Step "2/5 Database '$Database' and login '$AppUser'"
$hasUser = Invoke-Psql $bin "SELECT 1 FROM pg_roles WHERE rolname = '$AppUser'" $superPassword
if (-not $AppPassword) { $AppPassword = New-Password }
$sqlPassword = $AppPassword.Replace("'", "''")
if ($hasUser -ne "1") {
    Invoke-Psql $bin "CREATE ROLE $AppUser LOGIN PASSWORD '$sqlPassword'" $superPassword | Out-Null
    Write-Host "Login '$AppUser' created."
} elseif ($ResetPassword) {
    Invoke-Psql $bin "ALTER ROLE $AppUser PASSWORD '$sqlPassword'" $superPassword | Out-Null
    Write-Host "Password of '$AppUser' changed."
} else {
    $AppPassword = $null
    Write-Host "Login '$AppUser' already exists (password unchanged; use -ResetPassword for a new one)."
}
$hasDb = Invoke-Psql $bin "SELECT 1 FROM pg_database WHERE datname = '$Database'" $superPassword
if ($hasDb -ne "1") {
    Invoke-Psql $bin "CREATE DATABASE $Database OWNER $AppUser ENCODING 'UTF8' TEMPLATE template0" $superPassword | Out-Null
    Write-Host "Database '$Database' created."
} else {
    Write-Host "Database '$Database' already exists (kept)."
}

# ------------------------------------------------------------------ 3. local network only
Write-Step "3/5 Accept the shop's local network"
$conf = Join-Path $dataDir "postgresql.conf"
$hba = Join-Path $dataDir "pg_hba.conf"
if (-not (Test-Path $conf) -or -not (Test-Path $hba)) {
    $dataDir = Invoke-Psql $bin "SHOW data_directory" $superPassword
    $conf = Join-Path $dataDir "postgresql.conf"
    $hba = Join-Path $dataDir "pg_hba.conf"
}
$confText = Get-Content $conf -Raw
if ($confText -notmatch "(?m)^\s*listen_addresses\s*=\s*'\*'") {
    Add-Content $conf "`n# AkremMobile: other shop PCs connect over the local network`nlisten_addresses = '*'"
    Write-Host "listen_addresses = '*'"
}
$rule = "host    $Database    $AppUser    samenet    scram-sha-256"
if (-not (Select-String -Path $hba -SimpleMatch $rule -Quiet)) {
    Add-Content $hba "`n# AkremMobile: shop PCs on the same network, with a password`n$rule"
    Write-Host "pg_hba.conf: $rule"
}

# ------------------------------------------------------------------ 4. firewall
Write-Step "4/5 Windows firewall (private networks, port $Port)"
$ruleName = "AkremMobile PostgreSQL"
if (-not (Get-NetFirewallRule -DisplayName $ruleName -ErrorAction SilentlyContinue)) {
    New-NetFirewallRule -DisplayName $ruleName -Direction Inbound -Protocol TCP -LocalPort $Port -Profile Private -Action Allow | Out-Null
    Write-Host "Firewall rule added."
}
$public = Get-NetConnectionProfile | Where-Object { $_.NetworkCategory -eq "Public" }
if ($public) {
    Write-Host "Warning: the network '$($public.Name -join ', ')' is set to Public. Set it to Private (Windows Settings > Network) so the other PCs can connect." -ForegroundColor Yellow
}
if ($service) { Restart-Service $service.Name }

# ------------------------------------------------------------------ 5. what to type on the other PCs
Write-Step "5/5 Done"
$addresses = Get-NetIPAddress -AddressFamily IPv4 | Where-Object {
    $_.IPAddress -notlike "127.*" -and $_.IPAddress -notlike "169.254.*" -and $_.PrefixOrigin -ne "WellKnown"
} | Select-Object -ExpandProperty IPAddress
Write-Host ""
Write-Host "On every PC, open AkremMobile > Settings > Share the records between several PCs" -ForegroundColor Green
Write-Host "(on a new PC: the first screen > 'Another PC of the shop? Connect to the shop server') and type:" -ForegroundColor Green
Write-Host "  Main PC address : $($addresses -join '  or  ')   (on this PC itself: 127.0.0.1)"
Write-Host "  Port            : $Port"
if ($AppPassword) {
    Write-Host "  Database password: $AppPassword" -ForegroundColor Yellow
    Write-Host "Write this password down and keep it safe." -ForegroundColor Yellow
} else {
    Write-Host "  Database password: (unchanged)"
}
Write-Host ""
Write-Host "Do it first on the PC that already has the shop's records: it offers to move them to the server."
Write-Host "Tip: give this PC a fixed address in the router so the other PCs always find it."
