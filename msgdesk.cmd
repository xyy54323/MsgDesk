@echo off
"F:\env\python\python.exe" -c "import os, sys; sys.path.insert(0, os.path.dirname(r'%~f0')); from msgdesk.cli import main; raise SystemExit(main())" %*
