$ErrorActionPreference = "Stop"
$expectedDirectory = [System.IO.Path]::GetFullPath((Join-Path $env:LOCALAPPDATA "Programs\AkremMobile")).TrimEnd('\')
$currentDirectory = [System.IO.Path]::GetFullPath($PSScriptRoot).TrimEnd('\')
if (-not [string]::Equals($currentDirectory, $expectedDirectory, [System.StringComparison]::OrdinalIgnoreCase)) {
    throw "Refusing to remove an unexpected directory: $currentDirectory"
}
# The app may still be running hidden in the tray: stop it so its files can be removed.
$running = Get-Process -Name "AkremMobile" -ErrorAction SilentlyContinue
if ($running) {
    $running | Stop-Process -Force
    Start-Sleep -Milliseconds 800
}

$startMenuShortcut = Join-Path $env:APPDATA "Microsoft\Windows\Start Menu\Programs\AkremMobile.lnk"
$desktopShortcut = Join-Path ([Environment]::GetFolderPath("DesktopDirectory")) "AkremMobile.lnk"
$uninstallKeyPath = "HKCU:\Software\Microsoft\Windows\CurrentVersion\Uninstall\AkremMobile"
foreach ($shortcutPath in @($startMenuShortcut, $desktopShortcut)) {
    if (Test-Path -LiteralPath $shortcutPath) {
        Remove-Item -LiteralPath $shortcutPath -Force
    }
}
if (Test-Path -LiteralPath $uninstallKeyPath) {
    Remove-Item -LiteralPath $uninstallKeyPath -Recurse -Force
}
Remove-Item -LiteralPath $currentDirectory -Recurse -Force
