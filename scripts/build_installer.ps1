# Build the Windows installer (Setup.exe) with Inno Setup.
#
#   powershell -ExecutionPolicy Bypass -File scripts\build_installer.ps1
#   ... -Version 0.2.0

param(
    [string]$Iscc = "",
    [string]$Version = "0.1.0"
)

$ErrorActionPreference = "Stop"
$root = Split-Path -Parent $PSScriptRoot
Set-Location $root

if (-not $Iscc) {
    foreach ($candidate in @(
        "F:\tool\innosetup\app\ISCC.exe",
        "C:\Program Files (x86)\Inno Setup 6\ISCC.exe",
        "C:\Program Files\Inno Setup 6\ISCC.exe"
    )) {
        if (Test-Path $candidate) { $Iscc = $candidate; break }
    }
}
if (-not $Iscc) {
    $command = Get-Command iscc -ErrorAction SilentlyContinue
    if ($command) { $Iscc = $command.Source }
}
if (-not $Iscc) {
    throw "ISCC.exe not found. Install Inno Setup into F:\tool\innosetup first."
}

$app = Join-Path $root "dist\MsgDesk"
if (-not (Test-Path (Join-Path $app "MsgDesk.exe"))) {
    throw "dist\MsgDesk\MsgDesk.exe missing. Run scripts\build_package.ps1 first."
}

$work = Join-Path $root "build\installer"
New-Item -ItemType Directory -Path $work -Force | Out-Null
Copy-Item (Join-Path $root "packaging\ChineseSimplified.isl") $work -Force

$iss = Get-Content (Join-Path $root "packaging\MsgDesk.iss") -Raw -Encoding UTF8
$iss = $iss -replace '#define AppVersion ".*"', "#define AppVersion ""$Version"""

# Inno reads UTF-8 only when the file starts with a BOM.
$target = Join-Path $work "MsgDesk.iss"
[System.IO.File]::WriteAllText($target, $iss, (New-Object System.Text.UTF8Encoding($true)))

Write-Host "[installer] app: $app"
Write-Host "[installer] iscc: $Iscc"
& $Iscc "/DSrcDir=$app" "/DOutDir=$(Join-Path $root 'dist')" $target
if ($LASTEXITCODE -ne 0) { throw "Inno Setup failed" }

Write-Host "Done: dist\MsgDesk-Setup-$Version.exe"
