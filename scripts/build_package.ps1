# Build the portable MsgDesk package (Windows first, same flow per OS later).
#
#   powershell -ExecutionPolicy Bypass -File scripts\build_package.ps1
#   ... -WithBrowser     also bundle Playwright + Chromium (site login)
#   ... -WithNapCat      also copy vendor\napcat into the package (~345MB)
#   ... -NoZip           keep dist\MsgDesk and skip zipping

param(
    [string]$Python = "",
    [switch]$WithBrowser,
    [switch]$WithNapCat,
    [switch]$NoZip
)

$ErrorActionPreference = "Stop"
$root = Split-Path -Parent $PSScriptRoot
Set-Location $root

function Resolve-Python {
    param([string]$Explicit)
    if ($Explicit) { return $Explicit }
    foreach ($candidate in @(
        (Join-Path $root "runtime\python\python.exe"),
        (Join-Path $root ".venv\Scripts\python.exe"),
        (Join-Path $root ".venv\bin\python")
    )) {
        if (Test-Path $candidate) { return $candidate }
    }
    if (Get-Command py -ErrorAction SilentlyContinue) { return "py" }
    return "python"
}

$python = Resolve-Python $Python
Write-Host "[1/5] Python: $python"
& $python -m PyInstaller --version
if ($LASTEXITCODE -ne 0) {
    Write-Host "PyInstaller missing, installing..."
    & $python -m pip install --disable-pip-version-check pyinstaller
    if ($LASTEXITCODE -ne 0) { throw "Failed to install PyInstaller" }
}

Write-Host "[2/5] Cleaning previous build"
foreach ($path in @("build", "dist\MsgDesk")) {
    $full = Join-Path $root $path
    if (Test-Path $full) { Remove-Item -LiteralPath $full -Recurse -Force }
}

Write-Host "[3/5] Running PyInstaller"
$buildArgs = @(
    "-m", "PyInstaller",
    "--noconfirm", "--clean", "--onedir", "--console",
    "--name", "MsgDesk",
    "--collect-data", "msgdesk",
    "--paths", ".",
    "--distpath", "dist",
    "--workpath", "build",
    "--specpath", "build",
    "packaging/entry.py"
)
if (-not $WithBrowser) {
    $buildArgs += @("--exclude-module", "playwright")
}
& $python @buildArgs
if ($LASTEXITCODE -ne 0) { throw "PyInstaller build failed" }

$out = Join-Path $root "dist\MsgDesk"
Write-Host "[4/5] Copying config template and launcher"
Copy-Item (Join-Path $root "config.example.json") (Join-Path $out "config.example.json") -Force
Copy-Item (Join-Path $root "README.md") (Join-Path $out "README.md") -Force

$launcher = @"
@echo off
cd /d "%~dp0"
if not exist config.json MsgDesk.exe init
MsgDesk.exe serve --open
if errorlevel 1 pause
"@
Set-Content -Path (Join-Path $out "Start-MsgDesk.cmd") -Value $launcher -Encoding OEM

if ($WithNapCat) {
    if (-not (Test-Path (Join-Path $root "vendor\napcat\napcat.bat"))) {
        throw "vendor\napcat not found; run MsgDesk.exe napcat install first"
    }
    Write-Host "      Copying NapCat (~345MB)"
    robocopy (Join-Path $root "vendor\napcat") (Join-Path $out "vendor\napcat") /E /NFL /NDL /NJH /NJS /NP | Out-Null
    if ($LASTEXITCODE -ge 8) { throw "Failed to copy NapCat" }
}

if ($WithBrowser) {
    Write-Host "      Installing Chromium into ms-playwright"
    $browsers = Join-Path $out "ms-playwright"
    New-Item -ItemType Directory -Path $browsers -Force | Out-Null
    $env:PLAYWRIGHT_BROWSERS_PATH = $browsers
    & $python -m playwright install chromium
    if ($LASTEXITCODE -ne 0) { throw "Failed to install Chromium" }
    Remove-Item Env:\PLAYWRIGHT_BROWSERS_PATH
}

Write-Host "[5/5] Output"
if ($NoZip) {
    Write-Host "Done: $out"
    exit 0
}
$zip = Join-Path $root "dist\MsgDesk-win64.zip"
if (Test-Path $zip) { Remove-Item -LiteralPath $zip -Force }
Compress-Archive -Path (Join-Path $out "*") -DestinationPath $zip -CompressionLevel Optimal
Write-Host "Done: $zip"
