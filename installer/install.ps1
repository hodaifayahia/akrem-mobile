$ErrorActionPreference = "Stop"
$appVersion = "__APP_VERSION__"
$installDirectory = Join-Path $env:LOCALAPPDATA "Programs\AkremMobile"
$payloadArchive = Join-Path $PSScriptRoot "AkremMobile-Payload.zip"
$uninstaller = Join-Path $installDirectory "uninstall.ps1"
$appExecutable = Join-Path $installDirectory "AkremMobile.exe"
$uninstallKeyPath = "HKCU:\Software\Microsoft\Windows\CurrentVersion\Uninstall\AkremMobile"

# A copy hidden in the tray would keep the old files in use and answer the
# new shortcut with a stale, unresponsive window: stop it first.
$running = Get-Process -Name "AkremMobile" -ErrorAction SilentlyContinue
if ($running) {
    $running | Stop-Process -Force
    Start-Sleep -Milliseconds 800
}
if (-not (Test-Path -LiteralPath $payloadArchive)) {
    throw "Installer payload not found: $payloadArchive"
}

New-Item -ItemType Directory -Path $installDirectory -Force | Out-Null
Expand-Archive -LiteralPath $payloadArchive -DestinationPath $installDirectory -Force
if (-not (Test-Path -LiteralPath $appExecutable)) {
    throw "The installed app executable is missing: $appExecutable"
}

$startMenuDirectory = Join-Path $env:APPDATA "Microsoft\Windows\Start Menu\Programs"
$startMenuShortcut = Join-Path $startMenuDirectory "AkremMobile.lnk"
$desktopShortcut = Join-Path ([Environment]::GetFolderPath("DesktopDirectory")) "AkremMobile.lnk"
$shell = New-Object -ComObject WScript.Shell
foreach ($shortcutPath in @($startMenuShortcut, $desktopShortcut)) {
    $shortcut = $shell.CreateShortcut($shortcutPath)
    $shortcut.TargetPath = $appExecutable
    $shortcut.WorkingDirectory = $installDirectory
    $shortcut.IconLocation = "$appExecutable,0"
    $shortcut.Save()
}

New-Item -Path $uninstallKeyPath -Force | Out-Null
$uninstallCommand = '"{0}" -NoProfile -ExecutionPolicy Bypass -File "{1}"' -f (Join-Path $PSHOME "powershell.exe"), $uninstaller
New-ItemProperty -Path $uninstallKeyPath -Name "DisplayName" -Value "AkremMobile Installment Manager" -PropertyType String -Force | Out-Null
New-ItemProperty -Path $uninstallKeyPath -Name "DisplayVersion" -Value $appVersion -PropertyType String -Force | Out-Null
New-ItemProperty -Path $uninstallKeyPath -Name "Publisher" -Value "AkremMobile" -PropertyType String -Force | Out-Null
New-ItemProperty -Path $uninstallKeyPath -Name "InstallLocation" -Value $installDirectory -PropertyType String -Force | Out-Null
New-ItemProperty -Path $uninstallKeyPath -Name "DisplayIcon" -Value $appExecutable -PropertyType String -Force | Out-Null
New-ItemProperty -Path $uninstallKeyPath -Name "UninstallString" -Value $uninstallCommand -PropertyType String -Force | Out-Null
New-ItemProperty -Path $uninstallKeyPath -Name "NoModify" -Value 1 -PropertyType DWord -Force | Out-Null
New-ItemProperty -Path $uninstallKeyPath -Name "NoRepair" -Value 1 -PropertyType DWord -Force | Out-Null

Write-Host "AkremMobile $appVersion was installed for this Windows user."
