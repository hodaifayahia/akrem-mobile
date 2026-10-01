$ErrorActionPreference = "Stop"

$projectRoot = Split-Path -Parent $PSScriptRoot
$appDirectory = Join-Path $projectRoot "dist\AkremMobile"
$installerSource = Join-Path $projectRoot "installer"
$version = "0.1.0"
$versionMatch = [regex]::Match((Get-Content -LiteralPath (Join-Path $projectRoot "app\config.py") -Raw), 'APP_VERSION\s*=\s*["'']([^"'']+)["'']')
if ($versionMatch.Success) {
    $version = $versionMatch.Groups[1].Value
}

if (-not (Test-Path -LiteralPath (Join-Path $appDirectory "AkremMobile.exe"))) {
    throw "Windows app executable not found: $appDirectory\AkremMobile.exe"
}

$iexpress = Join-Path $env:WINDIR "System32\iexpress.exe"
if (-not (Test-Path -LiteralPath $iexpress)) {
    throw "Windows IExpress packaging tool not found: $iexpress"
}

$outputDirectory = Join-Path $projectRoot "dist\installer"
New-Item -ItemType Directory -Path $outputDirectory -Force | Out-Null
$setupName = "AkremMobile-Setup-$version.exe"
$setupOutput = Join-Path $outputDirectory $setupName
$stageRoot = Join-Path $env:TEMP ("Akremobile-Setup-" + [guid]::NewGuid().ToString("N"))
New-Item -ItemType Directory -Path $stageRoot | Out-Null

try {
    $payloadDirectory = Join-Path $stageRoot "payload"
    New-Item -ItemType Directory -Path $payloadDirectory | Out-Null
    Copy-Item -Path (Join-Path $appDirectory "*") -Destination $payloadDirectory -Recurse -Force
    Copy-Item -LiteralPath (Join-Path $installerSource "uninstall.ps1") -Destination (Join-Path $payloadDirectory "uninstall.ps1")

    $payloadArchive = Join-Path $stageRoot "AkremMobile-Payload.zip"
    Compress-Archive -Path (Join-Path $payloadDirectory "*") -DestinationPath $payloadArchive -CompressionLevel Optimal

    $installScript = (Get-Content -LiteralPath (Join-Path $installerSource "install.ps1") -Raw).Replace("__APP_VERSION__", $version)
    Set-Content -LiteralPath (Join-Path $stageRoot "install.ps1") -Value $installScript -Encoding UTF8
    Copy-Item -LiteralPath (Join-Path $installerSource "install.cmd") -Destination (Join-Path $stageRoot "install.cmd")

    if (Test-Path -LiteralPath $setupOutput) {
        Remove-Item -LiteralPath $setupOutput -Force
    }

    if (-not ("AkremMobileShortPath" -as [type])) {
        Add-Type -TypeDefinition @'
using System.Runtime.InteropServices;
using System.Text;

public static class AkremMobileShortPath {
    [DllImport("kernel32.dll", CharSet = CharSet.Auto, SetLastError = true)]
    public static extern uint GetShortPathName(string longPath, StringBuilder shortPath, uint bufferLength);
}
'@
    }
    $stageBuffer = New-Object System.Text.StringBuilder 1024
    $stageLength = [AkremMobileShortPath]::GetShortPathName($stageRoot, $stageBuffer, [uint32]$stageBuffer.Capacity)
    $outputBuffer = New-Object System.Text.StringBuilder 1024
    $outputLength = [AkremMobileShortPath]::GetShortPathName($outputDirectory, $outputBuffer, [uint32]$outputBuffer.Capacity)
    if ($stageLength -eq 0 -or $outputLength -eq 0) {
        throw "Could not resolve short Windows paths for the IExpress source and target."
    }
    $stageForSed = $stageBuffer.ToString()
    $targetForSed = Join-Path $outputBuffer.ToString() $setupName
    $sedPath = Join-Path $stageRoot "AkremMobile.sed"
    if ($stageForSed.Contains(" ") -or $targetForSed.Contains(" ")) {
        throw "IExpress requires source and target paths without spaces: $stageForSed, $targetForSed"
    }

    $sedLines = @(
        "[Version]",
        "Class=IEXPRESS",
        "SEDVersion=3",
        "[Options]",
        "PackagePurpose=InstallApp",
        "ShowInstallProgramWindow=1",
        "HideExtractAnimation=0",
        "UseLongFileName=1",
        "InsideCompressed=0",
        "CAB_FixedSize=0",
        "CAB_ResvCodeSigning=0",
        "RebootMode=N",
        "InstallPrompt=%InstallPrompt%",
        "DisplayLicense=%DisplayLicense%",
        "FinishMessage=%FinishMessage%",
        "TargetName=%TargetName%",
        "FriendlyName=%FriendlyName%",
        "AppLaunched=%AppLaunched%",
        "PostInstallCmd=%PostInstallCmd%",
        "AdminQuietInstCmd=%AdminQuietInstCmd%",
        "UserQuietInstCmd=%UserQuietInstCmd%",
        "SourceFiles=SourceFiles",
        "[Strings]",
        "InstallPrompt=",
        "DisplayLicense=",
        "FinishMessage=AkremMobile has been installed. Start it from the Start menu or desktop.",
        "TargetName=$targetForSed",
        "FriendlyName=AkremMobile Installment Manager Setup",
        "AppLaunched=install.cmd",
        "PostInstallCmd=<None>",
        "AdminQuietInstCmd=",
        "UserQuietInstCmd=",
        'FILE0="AkremMobile-Payload.zip"',
        'FILE1="install.ps1"',
        'FILE2="install.cmd"',
        "[SourceFiles]",
        "SourceFiles0=$stageForSed\",
        "[SourceFiles0]",
        "%FILE0%=",
        "%FILE1%=",
        "%FILE2%="
    )
    Set-Content -LiteralPath $sedPath -Value $sedLines -Encoding ASCII

    $sedPathForArgument = Join-Path $stageForSed "AkremMobile.sed"
    $iexpressProcess = Start-Process -FilePath $iexpress -ArgumentList @("/N", "/Q", $sedPathForArgument) -WorkingDirectory $stageForSed -PassThru -Wait -WindowStyle Hidden
    if ($iexpressProcess.ExitCode -ne 0) {
        throw "IExpress exited with code $($iexpressProcess.ExitCode)."
    }
    if (-not (Test-Path -LiteralPath $setupOutput)) {
        throw "IExpress did not create the setup executable: $setupOutput"
    }

    $setupSizeMb = [math]::Round((Get-Item -LiteralPath $setupOutput).Length / 1MB, 1)
    Write-Host "Created $setupOutput ($setupSizeMb MB)" -ForegroundColor Green
}
finally {
    if (Test-Path -LiteralPath $stageRoot) {
        Remove-Item -LiteralPath $stageRoot -Recurse -Force
    }
}
