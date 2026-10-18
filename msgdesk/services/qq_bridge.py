"""Start/stop the bundled NapCat and keep its OneBot endpoint configured."""

from __future__ import annotations

import json
import os
import subprocess
import time
import urllib.request
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent.parent
VENDOR = ROOT / "vendor" / "napcat"
CONFIG_DIR = VENDOR / "napcat" / "config"
QR_PATH = VENDOR / "napcat" / "cache" / "qrcode.png"
LOG_DIR = ROOT / "data" / "logs"
TOKEN = "msgdesk-qq-local"
HTTP_CONFIG = {
    "name": "msgdesk",
    "enable": True,
    "host": "127.0.0.1",
    "port": 3000,
    "enableCors": False,
    "enableWebsocket": False,
    "messagePostFormat": "array",
    "token": TOKEN,
    "debug": False,
}


def installed() -> bool:
    return (VENDOR / "napcat.bat").exists()


def _pids() -> list[int]:
    script = (
        "Get-CimInstance Win32_Process | Where-Object { "
        "$_.CommandLine -like '*vendor\\napcat*' -and $_.Name -ne 'pwsh.exe' } "
        "| Select-Object -ExpandProperty ProcessId"
    )
    result = subprocess.run(
        ["powershell", "-NoProfile", "-Command", script],
        capture_output=True, text=True, timeout=20,
    )
    return [int(item) for item in result.stdout.split() if item.strip().isdigit()]


def running() -> bool:
    return bool(_pids())


def login_info() -> dict[str, Any] | None:
    request = urllib.request.Request(
        "http://127.0.0.1:3000/get_login_info",
        data=b"{}",
        headers={"Content-Type": "application/json", "Authorization": f"Bearer {TOKEN}"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=4) as response:
            data = json.loads(response.read().decode("utf-8"))
    except Exception:  # noqa: BLE001 - not logged in yet
        return None
    info = data.get("data") or {}
    if not info.get("user_id"):
        return None
    return {"uin": str(info.get("user_id")), "name": str(info.get("nickname") or "")}


def start(uin: str | None = None) -> None:
    if not installed():
        raise RuntimeError("未安装 QQ 桥（vendor/napcat）")
    if running():
        return
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    args = [str(VENDOR / "napcat.bat")]
    if uin:
        args += ["-q", uin]
    subprocess.Popen(
        args,
        cwd=str(VENDOR),
        stdout=open(LOG_DIR / "napcat.log", "a", encoding="utf-8"),
        stderr=open(LOG_DIR / "napcat.err.log", "a", encoding="utf-8"),
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )


def stop() -> None:
    for pid in _pids():
        subprocess.run(["taskkill", "/PID", str(pid), "/F"], capture_output=True)


def configure_onebot() -> bool:
    """Give every logged-in account an HTTP endpoint (idempotent)."""
    if not CONFIG_DIR.is_dir():
        return False
    changed = False
    for path in sorted(CONFIG_DIR.glob("onebot11_*.json")):
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        network = data.setdefault("network", {})
        servers = network.setdefault("httpServers", [])
        if any(item.get("port") == 3000 for item in servers):
            continue
        servers.append(dict(HTTP_CONFIG))
        path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
        changed = True
    return changed


def relogin() -> None:
    """Restart without the saved account so a new QR code is shown."""
    stop()
    time.sleep(1)
    start(None)


def state() -> dict[str, Any]:
    """Cheap read-only probe used by the sources page."""
    info = login_info()
    return {"running": running(), "logged_in": bool(info), "uin": (info or {}).get("uin", "")}


def ensure_ready() -> dict[str, Any]:
    """Start NapCat, wire the endpoint, and report what the UI should show."""
    if not installed():
        return {"installed": False, "running": False, "logged_in": False}
    start()
    info = login_info()
    if info:
        if configure_onebot():
            stop()
            time.sleep(1)
            start(info["uin"])
            for _ in range(20):
                time.sleep(1)
                if login_info():
                    break
        info = login_info()
    return {
        "installed": True,
        "running": running(),
        "logged_in": bool(info),
        "uin": (info or {}).get("uin", ""),
        "name": (info or {}).get("name", ""),
        "qr": str(QR_PATH) if QR_PATH.exists() else "",
    }
