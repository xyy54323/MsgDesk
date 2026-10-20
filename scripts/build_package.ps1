# Build the portable MsgDesk package (Windows first, same flow per OS later).
#
#   powershell -ExecutionPolicy Bypass -File scripts\build_package.ps1
#   ... -SkipNapCat      do not bundle the QQ bridge
#   ... -WithBrowser     also bundle Playwright + Chromium (site login)
#   ... -NoZip           keep dist\MsgDesk and skip zipping

param(
    [string]$Python = "",
    [switch]$SkipNapCat,
    [switch]$WithBrowser,
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
    if (Get-Command python -ErrorAction SilentlyContinue) { return "python" }
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
$env:MSGDESK_WITH_BROWSER = if ($WithBrowser) { "1" } else { "0" }
$buildArgs = @(
    "-m", "PyInstaller",
    "--noconfirm", "--clean",
    "--distpath", "dist",
    "--workpath", "build",
    "packaging/MsgDesk.spec"
)
& $python @buildArgs
if ($LASTEXITCODE -ne 0) { throw "PyInstaller build failed" }
Remove-Item Env:\MSGDESK_WITH_BROWSER -ErrorAction SilentlyContinue

$out = Join-Path $root "dist\MsgDesk"
Write-Host "[4/5] Copying config template"
Copy-Item (Join-Path $root "config.example.json") (Join-Path $out "config.example.json") -Force
Copy-Item (Join-Path $root "README.md") (Join-Path $out "README.md") -Force
New-Item -ItemType Directory -Path (Join-Path $out "docs") -Force | Out-Null
Copy-Item (Join-Path $root "docs\screenshot.png") (Join-Path $out "docs\screenshot.png") -Force
New-Item -ItemType Directory -Path (Join-Path $out "packaging\icon") -Force | Out-Null
Copy-Item (Join-Path $root "packaging\icon\MsgDesk-256.png") (Join-Path $out "packaging\icon\MsgDesk-256.png") -Force

if (-not $SkipNapCat) {
    if (-not (Test-Path (Join-Path $root "vendor\napcat\napcat.bat"))) {
        throw "vendor\napcat not found; run MsgDesk.exe napcat install first"
    }
    Write-Host "      Copying NapCat (~345MB, account config stripped)"
    $source = Join-Path $root "vendor\napcat"
    $target = Join-Path $out "vendor\napcat"
    # Never ship this machine's QQ session: config/cache/logs belong to the user.
    robocopy $source $target /E `
        /XD (Join-Path $source "napcat\config") (Join-Path $source "napcat\cache") (Join-Path $source "napcat\logs") `
        /XF "beacon_report.log" "stdout*.log" "stderr*.log" "*.db" "*.db-shm" "*.db-wal" `
        /NFL /NDL /NJH /NJS /NP | Out-Null
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
exit 0
