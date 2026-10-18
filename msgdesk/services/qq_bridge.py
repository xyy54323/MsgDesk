"""Start/stop the bundled NapCat and keep its OneBot endpoint configured.

The login QR code lives in ``vendor/napcat/napcat/cache/qrcode.png`` and QQ
invalidates it after about two minutes.  NapCat rewrites the file when the
session expires, but only *after* it expires, which means a user who opens the
page late scans a dead code.  We therefore talk to NapCat's local WebUI and
rotate the code before it dies, and we never hand a stale file to the browser.
"""

from __future__ import annotations

import hashlib
import json
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request
import zipfile
from pathlib import Path
from typing import Any

from ..paths import app_root

ROOT = app_root()
VENDOR = ROOT / "vendor" / "napcat"
CONFIG_DIR = VENDOR / "napcat" / "config"
WEBUI_CONFIG = CONFIG_DIR / "webui.json"
QR_PATH = VENDOR / "napcat" / "cache" / "qrcode.png"
LOG_DIR = ROOT / "data" / "logs"
TOKEN = "msgdesk-qq-local"
RELEASE_API = "https://api.github.com/repos/NapNeko/NapCatQQ/releases/latest"
ASSET_NAME = "NapCat.Shell.Windows.Node.zip"

# A QQ login QR is valid for two minutes.  Rotate it while there is still a
# comfortable window left, so scanning never races the expiry.
QR_TTL = 120
QR_REFRESH_AHEAD = 35
QR_GRACE = 20  # tolerate clock/file-write jitter when deciding to serve the file
AUTO_REFRESH_GAP = 8

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

_credential: dict[str, Any] = {"value": "", "at": 0.0, "port": 0}
_last_auto_refresh = 0.0
_last_onebot_restart = 0.0
_pid_cache: dict[str, Any] = {"at": 0.0, "pids": []}
_port_cache: dict[str, Any] = {"at": 0.0, "port": None}


def installed() -> bool:
    return (VENDOR / "napcat.bat").exists()


def install(force: bool = False) -> str:
    """Download the bundled QQ bridge into ``vendor/napcat`` once."""
    if sys.platform != "win32":
        raise RuntimeError("安装包目前只带 Windows 版 QQ 桥，其他平台要换对应的 NapCat 构建")
    if installed() and not force:
        return str(VENDOR)
    request = urllib.request.Request(RELEASE_API, headers={"User-Agent": "msgdesk"})
    with urllib.request.urlopen(request, timeout=60) as response:
        release = json.loads(response.read().decode("utf-8"))
    url = ""
    for asset in release.get("assets") or []:
        if asset.get("name") == ASSET_NAME:
            url = str(asset.get("browser_download_url") or "")
            break
    if not url:
        raise RuntimeError(f"没有找到 {ASSET_NAME} 下载地址")
    VENDOR.mkdir(parents=True, exist_ok=True)
    archive = VENDOR / ASSET_NAME
    urllib.request.urlretrieve(url, archive)
    with zipfile.ZipFile(archive) as handle:
        handle.extractall(VENDOR)
    archive.unlink(missing_ok=True)
    return str(VENDOR)


def _read_json(path: Path) -> dict[str, Any]:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}


def _pids(refresh: bool = False) -> list[int]:
    now = time.time()
    if not refresh and now - float(_pid_cache["at"]) < 5:
        return list(_pid_cache["pids"])
    script = (
        "Get-CimInstance Win32_Process | Where-Object { "
        "$_.Name -eq 'node.exe' -and $_.CommandLine -like '*vendor\\napcat*' } "
        "| Select-Object -ExpandProperty ProcessId"
    )
    try:
        result = subprocess.run(
            ["powershell", "-NoProfile", "-Command", script],
            capture_output=True, text=True, timeout=20,
        )
        pids = [int(item) for item in result.stdout.split() if item.strip().isdigit()]
    except (OSError, subprocess.SubprocessError):
        pids = list(_pid_cache["pids"])
    _pid_cache.update(at=now, pids=pids)
    return pids


def _webui_ports() -> list[int]:
    base = int(_read_json(WEBUI_CONFIG).get("port") or 6099)
    # NapCat bumps the port when the configured one is busy.
    return [base + offset for offset in range(5)]


def webui_port() -> int | None:
    """Return the port NapCat's local WebUI actually listens on."""
    cached = _port_cache["port"]
    if cached and time.time() - float(_port_cache["at"]) < 30:
        try:
            with socket.create_connection(("127.0.0.1", int(cached)), timeout=0.15):
                return int(cached)
        except OSError:
            pass
    for port in _webui_ports():
        try:
            with socket.create_connection(("127.0.0.1", port), timeout=0.15):
                _port_cache.update(at=time.time(), port=port)
                return port
        except OSError:
            continue
    _port_cache.update(at=time.time(), port=None)
    return None


def running() -> bool:
    return webui_port() is not None or bool(_pids())


def _post(port: int, path: str, payload: dict[str, Any], credential: str = "") -> dict[str, Any]:
    headers = {"Content-Type": "application/json"}
    if credential:
        headers["Authorization"] = f"Bearer {credential}"
    request = urllib.request.Request(
        f"http://127.0.0.1:{port}/api{path}",
        data=json.dumps(payload).encode("utf-8"),
        headers=headers,
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=4) as response:
            return json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8", "replace")
        try:
            return json.loads(body)
        except json.JSONDecodeError:
            return {"code": -1, "message": body or f"HTTP {exc.code}"}


def _credential_for(port: int) -> str:
    cached = str(_credential["value"])
    if cached and _credential["port"] == port and time.time() - float(_credential["at"]) < 2700:
        return cached
    token = str(_read_json(WEBUI_CONFIG).get("token") or "")
    if not token:
        raise RuntimeError("QQ WebUI 未配置访问令牌")
    digest = hashlib.sha256((token + ".napcat").encode("utf-8")).hexdigest()
    data = _post(port, "/auth/login", {"hash": digest})
    payload = data.get("data") or {}
    value = str(payload.get("Credential") or payload.get("credential") or "")
    if data.get("code") != 0 or not value:
        raise RuntimeError(str(data.get("message") or "QQ WebUI 登录失败"))
    _credential.update(value=value, at=time.time(), port=port)
    return value


def webui_call(path: str, payload: dict[str, Any] | None = None) -> dict[str, Any]:
    """Call a NapCat WebUI endpoint, re-authenticating once on 401."""
    port = webui_port()
    if port is None:
        raise RuntimeError("QQ 服务未运行")
    message = ""
    for _ in range(2):
        data = _post(port, path, payload or {}, _credential_for(port))
        if data.get("code") == 0:
            return data.get("data") or {}
        message = str(data.get("message") or "")
        if "Unauthorized" not in message and "token" not in message.lower():
            break
        _credential["value"] = ""
    raise RuntimeError(message or "QQ 服务返回错误")


def login_info() -> dict[str, Any] | None:
    # Windows can sit on a refused localhost connect for ~2s; probe first so a
    # closed OneBot port does not stall every poll.
    try:
        with socket.create_connection(("127.0.0.1", 3000), timeout=0.3):
            pass
    except OSError:
        return None
    request = urllib.request.Request(
        "http://127.0.0.1:3000/get_login_info",
        data=b"{}",
        headers={"Content-Type": "application/json", "Authorization": f"Bearer {TOKEN}"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=3) as response:
            data = json.loads(response.read().decode("utf-8"))
    except Exception:  # noqa: BLE001 - not logged in yet
        return None
    info = data.get("data") or {}
    if not info.get("user_id"):
        return None
    return {"uin": str(info.get("user_id")), "name": str(info.get("nickname") or "")}


def _qr_stamp() -> tuple[int, int] | None:
    try:
        stat = QR_PATH.stat()
    except OSError:
        return None
    return (stat.st_mtime_ns, stat.st_size)


def qr_age() -> int:
    """Seconds since the current QR file was written, or -1 when absent."""
    try:
        return int(time.time() - QR_PATH.stat().st_mtime)
    except OSError:
        return -1


def qr_ready() -> bool:
    age = qr_age()
    return 0 <= age <= QR_TTL + QR_GRACE and running()


def login_phase() -> dict[str, Any]:
    try:
        return webui_call("/QQLogin/CheckLoginStatus")
    except (RuntimeError, urllib.error.URLError, OSError):
        return {}


def webui_login_info() -> dict[str, Any]:
    """Account info straight from the WebUI (works before OneBot is wired)."""
    try:
        data = webui_call("/QQLogin/GetQQLoginInfo")
    except (RuntimeError, urllib.error.URLError, OSError):
        return {}
    return {
        "uin": str(data.get("uin") or ""),
        "name": str(data.get("nick") or ""),
    }


def refresh_qr(wait: bool = True) -> bool:
    """Ask NapCat for a brand new QR code.

    ``wait=False`` is used by the background poll: NapCat needs a couple of
    seconds to fetch and rasterise the new code, and blocking the request
    thread would make the page feel frozen.
    """
    before = _qr_stamp()
    webui_call("/QQLogin/RefreshQRcode")
    if not wait:
        return True
    for _ in range(40):
        time.sleep(0.15)
        after = _qr_stamp()
        if after and after != before:
            return True
    return False


def _auto_refresh() -> bool:
    global _last_auto_refresh
    now = time.time()
    if now - _last_auto_refresh < AUTO_REFRESH_GAP:
        return False
    _last_auto_refresh = now
    try:
        return refresh_qr(wait=False)
    except (RuntimeError, urllib.error.URLError, OSError):
        return False


def _snapshot(
    info: dict[str, Any] | None,
    phase: dict[str, Any],
    running_state: bool,
    logged_in: bool,
) -> dict[str, Any]:
    age = qr_age()
    phase_name = str(phase.get("loginPhase") or "")
    scanned = phase_name in {"qrcode_scanned", "initializing", "logging_in"}
    left = max(0, QR_TTL - age) if age >= 0 else 0
    return {
        "installed": installed(),
        "running": running_state,
        "logged_in": logged_in,
        "uin": (info or {}).get("uin", ""),
        "name": (info or {}).get("name", ""),
        "qr_age": age,
        "qr_left": left,
        "qr_ttl": QR_TTL,
        "qr_ready": 0 <= age <= QR_TTL + QR_GRACE and running_state,
        "qr_mtime": int(QR_PATH.stat().st_mtime_ns // 1000) if age >= 0 else 0,
        "phase": phase_name,
        "scanned": bool(scanned),
        "error": str(phase.get("loginError") or ""),
    }


def start(uin: str | None = None) -> None:
    if not installed():
        raise RuntimeError("未安装 QQ 桥（vendor/napcat）")
    if running():
        return
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    args = ["cmd.exe", "/c", str(VENDOR / "napcat.bat")]
    if uin:
        args += ["-q", uin]
    subprocess.Popen(
        args,
        cwd=str(VENDOR),
        stdout=open(LOG_DIR / "napcat.log", "a", encoding="utf-8"),
        stderr=open(LOG_DIR / "napcat.err.log", "a", encoding="utf-8"),
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )
    _pid_cache.update(at=0.0, pids=[])
    _port_cache.update(at=0.0, port=None)


def stop() -> None:
    for pid in _pids(refresh=True):
        subprocess.run(["taskkill", "/PID", str(pid), "/F"], capture_output=True)
    _pid_cache.update(at=0.0, pids=[])
    _port_cache.update(at=0.0, port=None)


def configure_onebot() -> bool:
    """Give every logged-in account an HTTP endpoint (idempotent)."""
    if not CONFIG_DIR.is_dir():
        return False
    changed = False
    for path in sorted(CONFIG_DIR.glob("onebot11_*.json")):
        data = _read_json(path)
        if not data:
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
    # Never leave the previous session's PNG behind: the browser must not show
    # a code that QQ already invalidated.
    QR_PATH.unlink(missing_ok=True)
    _credential.update(value="", at=0.0)
    stop()
    time.sleep(1)
    start(None)


def state() -> dict[str, Any]:
    """Read-only probe used by the sources page; rotates an expiring QR."""
    running_state = running()
    phase: dict[str, Any] = login_phase() if running_state else {}
    # The WebUI already knows whether QQ is logged in; only ask OneBot (which
    # costs a socket probe when its port is closed) once the answer is yes.
    info = login_info() if (running_state and (phase.get("isLogin") or not phase)) else None
    logged_in = bool(info) or bool(phase.get("isLogin"))
    phase_name = str(phase.get("loginPhase") or "")
    scanned = phase_name in {"qrcode_scanned", "initializing", "logging_in"}
    if running_state and not logged_in and not scanned:
        age = qr_age()
        if age < 0 or QR_TTL - age <= QR_REFRESH_AHEAD:
            if _auto_refresh():
                phase = login_phase()
    return _snapshot(info, phase, running_state, logged_in)


def ensure_ready() -> dict[str, Any]:
    """Start NapCat if needed, wire the endpoint, and report the login state."""
    global _last_onebot_restart
    if not installed():
        return {
            "installed": False, "running": False, "logged_in": False,
            "qr_ready": False, "qr_age": -1, "qr_left": 0, "qr_mtime": 0,
            "phase": "", "scanned": False, "error": "",
        }
    if not running():
        # A leftover PNG from an earlier run is guaranteed to be dead.
        if qr_age() > QR_TTL + QR_GRACE:
            QR_PATH.unlink(missing_ok=True)
        start()
        deadline = time.time() + 25
        while time.time() < deadline and not running():
            time.sleep(0.5)
    info = login_info()
    phase = {} if info else login_phase()
    logged = bool(info) or bool(phase.get("isLogin"))
    if logged and not info:
        # QQ is logged in but the OneBot HTTP endpoint is not answering.  On a
        # fresh install the account config is created by NapCat during login,
        # so wire port 3000 and restart once with the account for quick login.
        who = webui_login_info()
        changed = configure_onebot()
        if not changed:
            time.sleep(2)
            changed = configure_onebot()
        now = time.time()
        if changed or now - _last_onebot_restart > 40:
            _last_onebot_restart = now
            stop()
            time.sleep(1)
            QR_PATH.unlink(missing_ok=True)
            start(who.get("uin") or None)
            for _ in range(15):
                time.sleep(1)
                info = login_info()
                if info:
                    break
    data = state()
    data["installed"] = True
    return data
