$projectRoot = Split-Path -Parent $MyInvocation.MyCommand.Path
$candidates = @(
    (Join-Path $projectRoot "runtime\python\python.exe"),
    (Join-Path $projectRoot ".venv\Scripts\python.exe"),
    (Join-Path $projectRoot ".venv\bin\python")
)
$python = $null
foreach ($candidate in $candidates) {
    if (Test-Path $candidate) { $python = $candidate; break }
}
if (-not $python) {
    if (Get-Command py -ErrorAction SilentlyContinue) { $python = "py" }
    elseif (Get-Command python -ErrorAction SilentlyContinue) { $python = "python" }
}
if (-not $python) {
    Write-Host "[MsgDesk] Python 3.11+ not found. Use the packaged build (Start-MsgDesk.cmd)."
    exit 1
}
$code = "import sys; sys.path.insert(0, r'$projectRoot'); from msgdesk.cli import main; raise SystemExit(main())"
& $python -c $code @args
exit $LASTEXITCODE
