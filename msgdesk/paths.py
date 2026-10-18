"""Filesystem layout that works both from source and from a packaged build."""

from __future__ import annotations

import os
import sys
from pathlib import Path


def is_frozen() -> bool:
    return bool(getattr(sys, "frozen", False))


def app_root() -> Path:
    """Folder that owns config.json / data / vendor.

    When packaged, that is the folder containing the executable; from source it
    is the repository root.  Keeping user data next to the app makes the whole
    thing portable (copy the folder, keep your data).
    """
    if is_frozen():
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parent.parent


def package_root() -> Path:
    """Folder holding the importable package and its data files."""
    if is_frozen():
        bundle = Path(getattr(sys, "_MEIPASS", app_root()))
        return bundle / "msgdesk"
    return Path(__file__).resolve().parent


def configure_runtime_env() -> None:
    """Point optional components at data shipped next to the executable."""
    if not is_frozen():
        return
    browsers = app_root() / "ms-playwright"
    if browsers.is_dir():
        os.environ.setdefault("PLAYWRIGHT_BROWSERS_PATH", str(browsers))
