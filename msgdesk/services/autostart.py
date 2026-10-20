"""Register MsgDesk as a per-user Windows startup entry.

The desktop build has no installer, so the app writes its own
``HKCU\\Software\\Microsoft\\Windows\\CurrentVersion\\Run`` value.  This keeps
the toggle reversible from the settings page without requiring admin rights.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path
from typing import Any

RUN_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"
VALUE_NAME = "MsgDesk"


def launcher_command(config_path: str | Path | None = None) -> str:
    """Build the command line used for the startup entry."""
    executable = Path(sys.executable)
    pythonw = executable.with_name("pythonw.exe")
    if pythonw.exists():
        executable = pythonw
    if getattr(sys, "frozen", False):
        parts = [str(executable)]
    else:
        parts = [str(executable), "-m", "msgdesk"]
    if config_path:
        parts += ["--config", str(config_path)]
    parts += ["serve", "--desktop"]
    return subprocess.list2cmdline(parts)


def is_enabled() -> bool:
    if os.name != "nt":
        return False
    import winreg

    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY) as key:
            winreg.QueryValueEx(key, VALUE_NAME)
        return True
    except OSError:
        return False


def apply(enabled: bool, config_path: str | Path | None = None) -> bool:
    """Create or remove the startup entry; returns the effective state."""
    if os.name != "nt":
        return False
    import winreg

    try:
        with winreg.OpenKey(
            winreg.HKEY_CURRENT_USER, RUN_KEY, 0, winreg.KEY_SET_VALUE
        ) as key:
            if enabled:
                winreg.SetValueEx(
                    key,
                    VALUE_NAME,
                    0,
                    winreg.REG_SZ,
                    launcher_command(config_path),
                )
            else:
                try:
                    winreg.DeleteValue(key, VALUE_NAME)
                except FileNotFoundError:
                    pass
    except OSError:
        return is_enabled()
    return is_enabled()


def sync(config: Any) -> bool:
    """Keep the registry entry aligned with the saved setting."""
    enabled = bool(getattr(config.system, "autostart", False))
    if not enabled:
        if is_enabled():
            return apply(False)
        return False
    config_path = getattr(config, "config_path", None)
    current = ""
    if os.name == "nt":
        import winreg

        try:
            with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY) as key:
                current = str(winreg.QueryValueEx(key, VALUE_NAME)[0] or "")
        except OSError:
            current = ""
    expected = launcher_command(config_path)
    if current == expected:
        return True
    return apply(True, config_path)
