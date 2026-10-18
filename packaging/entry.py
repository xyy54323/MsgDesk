"""PyInstaller entry point (same CLI as ``python -m msgdesk``)."""

from msgdesk.cli import main

if __name__ == "__main__":
    raise SystemExit(main())
