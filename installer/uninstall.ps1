$ErrorActionPreference = "Stop"
$expectedDirectory = [System.IO.Path]::GetFullPath((Join-Path $env:LOCALAPPDATA "Programs\AkremMobile")).TrimEnd('\')
$currentDirectory = [System.IO.Path]::GetFullPath($PSScriptRoot).TrimEnd('\')
if (-not [string]::Equals($currentDirectory, $expectedDirectory, [System.StringComparison]::OrdinalIgnoreCase)) {
    throw "Refusing to remove an unexpected directory: $currentDirectory"
}
if (Get-Process -Name "AkremMobile" -ErrorAction SilentlyContinue) {
    throw "Close AkremMobile before uninstalling it."
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
