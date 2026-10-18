@echo off
setlocal
set "ROOT=%~dp0"
set "ROOT=%ROOT:~0,-1%"
set "PY="
if exist "%ROOT%\runtime\python\python.exe" set "PY=%ROOT%\runtime\python\python.exe"
if not defined PY if exist "%ROOT%\.venv\Scripts\python.exe" set "PY=%ROOT%\.venv\Scripts\python.exe"
if not defined PY (
  py -3 -c "import sys" >nul 2>nul
  if not errorlevel 1 set "PY=py -3"
)
if not defined PY (
  python -c "import sys" >nul 2>nul
  if not errorlevel 1 set "PY=python"
)
if not defined PY (
  echo [MsgDesk] Python 3.11+ not found.
  echo Use the packaged build ^(Start-MsgDesk.cmd^), or install Python and retry.
  exit /b 1
)
%PY% -c "import sys; sys.path.insert(0, r'%ROOT%'); from msgdesk.cli import main; raise SystemExit(main())" %*
exit /b %errorlevel%
