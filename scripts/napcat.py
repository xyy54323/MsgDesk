"""Manage the bundled NapCat (the local QQ bridge) from inside this project.

Usage:
    python scripts/napcat.py install     # download NapCat into vendor/napcat
    python scripts/napcat.py configure   # enable the OneBot HTTP server on :3000
    python scripts/napcat.py start [-q UIN]   # start it (QR login if no -q)
    python scripts/napcat.py stop
    python scripts/napcat.py status
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import time
import urllib.request
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
VENDOR = ROOT / "vendor" / "napcat"
CONFIG_DIR = VENDOR / "napcat" / "config"
LOG_DIR = ROOT / "data" / "logs"
RELEASE_API = "https://api.github.com/repos/NapNeko/NapCatQQ/releases/latest"
ASSET_NAME = "NapCat.Shell.Windows.Node.zip"
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


def _latest_asset() -> tuple[str, str]:
    request = urllib.request.Request(RELEASE_API, headers={"User-Agent": "msgdesk"})
    with urllib.request.urlopen(request, timeout=30) as response:
        data = json.loads(response.read().decode("utf-8"))
    for asset in data.get("assets") or []:
        if asset.get("name") == ASSET_NAME:
            return asset["browser_download_url"], data.get("tag_name", "")
    raise SystemExit(f"release asset {ASSET_NAME} not found")


def install() -> None:
    if (VENDOR / "napcat.bat").exists():
        print(f"already installed: {VENDOR}")
        return
    url, tag = _latest_asset()
    VENDOR.mkdir(parents=True, exist_ok=True)
    archive = VENDOR / ASSET_NAME
    print(f"downloading {tag}: {url}")
    urllib.request.urlretrieve(url, archive)
    with zipfile.ZipFile(archive) as zf:
        zf.extractall(VENDOR)
    archive.unlink(missing_ok=True)
    print(f"installed into {VENDOR}")


def configure() -> None:
    if not CONFIG_DIR.is_dir():
        raise SystemExit("NapCat config dir missing; run install then start once to log in")
    files = sorted(CONFIG_DIR.glob("onebot11_*.json"))
    if not files:
        raise SystemExit(
            "no onebot11_<QQ>.json yet: start NapCat and scan the QR code first"
        )
    changed = 0
    for path in files:
        data = json.loads(path.read_text(encoding="utf-8"))
        network = data.setdefault("network", {})
        servers = network.setdefault("httpServers", [])
        if any(item.get("port") == 3000 for item in servers):
            continue
        servers.append(dict(HTTP_CONFIG))
        path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"configured {path.name}")
        changed += 1
    if not changed:
        print("already configured")
    print("restart NapCat for the change to take effect: python scripts/napcat.py stop && start")


def _pids() -> list[int]:
    result = subprocess.run(
        ["powershell", "-NoProfile", "-Command",
         "Get-CimInstance Win32_Process | Where-Object { $_.CommandLine -like '*vendor\\\\napcat*' "
         "-and $_.Name -ne 'pwsh.exe' } | Select-Object -ExpandProperty ProcessId"],
        capture_output=True, text=True,
    )
    return [int(line) for line in result.stdout.split() if line.strip().isdigit()]


def start(uin: str | None) -> None:
    if not (VENDOR / "napcat.bat").exists():
        raise SystemExit("NapCat not installed; run: python scripts/napcat.py install")
    if _pids():
        print("already running")
        return
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    args = [str(VENDOR / "napcat.bat")]
    if uin:
        args += ["-q", uin]
    subprocess.Popen(
        args,
        cwd=str(VENDOR),
        stdout=open(LOG_DIR / "napcat.log", "w", encoding="utf-8"),
        stderr=open(LOG_DIR / "napcat.err.log", "w", encoding="utf-8"),
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )
    print(f"started; log: {LOG_DIR / 'napcat.log'}")
    print(f"QR code (if login needed): {VENDOR / 'napcat' / 'cache' / 'qrcode.png'}")


def stop() -> None:
    pids = _pids()
    if not pids:
        print("not running")
        return
    for pid in pids:
        subprocess.run(["taskkill", "/PID", str(pid), "/F"], capture_output=True)
    print(f"stopped {len(pids)} process(es)")


def status() -> None:
    pids = _pids()
    print("running" if pids else "stopped")
    request = urllib.request.Request(
        "http://127.0.0.1:3000/get_group_list",
        data=b"{}",
        headers={"Content-Type": "application/json", "Authorization": f"Bearer {TOKEN}"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=5) as response:
            data = json.loads(response.read().decode("utf-8"))
        groups = data.get("data") or []
        print(f"onebot ok, {len(groups)} groups")
    except Exception as exc:  # noqa: BLE001
        print(f"onebot unreachable: {exc}")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=["install", "configure", "start", "stop", "status"])
    parser.add_argument("-q", "--uin", help="QQ number for silent re-login")
    args = parser.parse_args()
    if args.action == "install":
        install()
    elif args.action == "configure":
        configure()
    elif args.action == "start":
        start(args.uin)
    elif args.action == "stop":
        stop()
    else:
        status()


if __name__ == "__main__":
    main()
