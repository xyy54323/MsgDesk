$projectRoot = Split-Path -Parent $MyInvocation.MyCommand.Path
$code = "import sys; sys.path.insert(0, r'$projectRoot'); from msgdesk.cli import main; raise SystemExit(main())"
& "F:\env\python\python.exe" -c $code @args
exit $LASTEXITCODE
