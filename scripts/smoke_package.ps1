# Start the packaged app on a spare port, hit the UI, then shut it down.
#
#   powershell -ExecutionPolicy Bypass -File scripts\smoke_package.ps1

param(
    [string]$Package = "",
    [int]$Port = 8099
)

$ErrorActionPreference = "Stop"
$root = Split-Path -Parent $PSScriptRoot
if (-not $Package) { $Package = Join-Path $root "dist\MsgDesk" }
$Package = (Resolve-Path $Package).Path
$work = Join-Path $root "dist\_smoke"

if (Test-Path $work) { Remove-Item -LiteralPath $work -Recurse -Force }
New-Item -ItemType Directory -Path $work -Force | Out-Null
# Skip the 345MB QQ bridge: the UI smoke test only needs the app itself.
robocopy $Package $work /E /XD (Join-Path $Package "vendor") /NFL /NDL /NJH /NJS /NP | Out-Null
if ($LASTEXITCODE -ge 8) { throw "Failed to copy package" }

$config = Get-Content (Join-Path $work "config.example.json") -Raw -Encoding UTF8 | ConvertFrom-Json
$config.system.port = $Port
$json = $config | ConvertTo-Json -Depth 20
[System.IO.File]::WriteAllText(
    (Join-Path $work "config.json"),
    $json,
    (New-Object System.Text.UTF8Encoding($false))
)

$exe = Join-Path $work "MsgDesk-cli.exe"
$process = Start-Process -FilePath $exe -ArgumentList "serve" -WorkingDirectory $work -PassThru -WindowStyle Hidden `
    -RedirectStandardOutput (Join-Path $work "smoke.out.log") `
    -RedirectStandardError (Join-Path $work "smoke.err.log")

try {
    $ok = $false
    for ($i = 0; $i -lt 30; $i++) {
        Start-Sleep -Milliseconds 500
        try {
            $health = Invoke-WebRequest -UseBasicParsing -Uri "http://127.0.0.1:$Port/api/health" -TimeoutSec 2
            if ($health.StatusCode -eq 200) { $ok = $true; break }
        } catch { }
    }
    if (-not $ok) {
        Write-Host "FAILED: server did not answer on port $Port"
        Get-Content (Join-Path $work "smoke.err.log") -ErrorAction SilentlyContinue
        exit 1
    }
    $html = (Invoke-WebRequest -UseBasicParsing -Uri "http://127.0.0.1:$Port/").Content
    if ($html -notmatch "MSGDESK") { Write-Host "FAILED: UI not served"; exit 1 }
    Write-Host "OK: packaged app serves the UI on http://127.0.0.1:$Port/"
} finally {
    Stop-Process -Id $process.Id -Force -ErrorAction SilentlyContinue
}
