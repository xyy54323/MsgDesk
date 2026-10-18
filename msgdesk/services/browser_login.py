"""Let the user log in inside a real browser and capture the cookies."""

from __future__ import annotations

import threading
import time
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from ..repository import loads
from ..timeutil import iso_utc
from datetime import datetime, timezone

_STATE: dict[str, dict[str, Any]] = {}
_LOCK = threading.Lock()
MAX_WAIT_SECONDS = 900


def status(source_id: str) -> dict[str, Any]:
    with _LOCK:
        return dict(_STATE.get(source_id) or {"running": False, "error": None, "saved": 0})


def start(config: Any, repo: Any, source_id: str, url: str) -> bool:
    with _LOCK:
        if (_STATE.get(source_id) or {}).get("running"):
            return False
        _STATE[source_id] = {"running": True, "error": None, "saved": 0}
    threading.Thread(
        target=_run, args=(config, repo, source_id, url), daemon=True
    ).start()
    return True


def _build_cookie_header(cookies: list[dict[str, Any]], url: str) -> str:
    host = (urlsplit(url).hostname or "").lower()
    pairs: list[str] = []
    for cookie in cookies:
        domain = str(cookie.get("domain") or "").lstrip(".").lower()
        if not domain:
            continue
        if host == domain or host.endswith("." + domain):
            pairs.append(f"{cookie.get('name')}={cookie.get('value')}")
    return "; ".join(pairs)


def _cookie_expiry(cookies: list[dict[str, Any]], url: str) -> str | None:
    """Earliest positive expiry among this site's cookies, if any."""
    host = (urlsplit(url).hostname or "").lower()
    stamps: list[float] = []
    jwt_stamps: list[float] = []
    for cookie in cookies:
        domain = str(cookie.get("domain") or "").lstrip(".").lower()
        if not domain:
            continue
        if host != domain and not host.endswith("." + domain):
            continue
        expires = float(cookie.get("expires") or -1)
        if expires > 0:
            stamps.append(expires)
        jwt_exp = _jwt_expiry(str(cookie.get("value") or ""))
        if jwt_exp:
            jwt_stamps.append(jwt_exp)
    candidates = stamps or jwt_stamps
    if not candidates:
        return None
    return iso_utc(datetime.fromtimestamp(min(candidates), tz=timezone.utc))


def _jwt_expiry(value: str) -> float | None:
    """Read the exp claim of a JWT-looking cookie value."""
    parts = value.split(".")
    if len(parts) != 3:
        return None
    import base64
    import json

    payload = parts[1] + "=" * (-len(parts[1]) % 4)
    try:
        data = json.loads(base64.urlsafe_b64decode(payload).decode("utf-8"))
    except Exception:  # noqa: BLE001 - not a JWT
        return None
    exp = data.get("exp") if isinstance(data, dict) else None
    return float(exp) if isinstance(exp, (int, float)) else None


def _has_password(page: Any) -> bool:
    """True while any frame still shows a password box (i.e. not logged in)."""
    try:
        frames = list(page.frames)
    except Exception:  # noqa: BLE001
        return True
    for frame in frames:
        try:
            if frame.query_selector("input[type=password]"):
                return True
        except Exception:  # noqa: BLE001 - detached frame
            continue
    return False


def _looks_like_login_url(url: str) -> bool:
    lowered = url.lower()
    return any(
        marker in lowered
        for marker in ("login", "signin", "sign-in", "sso", "/auth", "passport")
    )


def _run(config: Any, repo: Any, source_id: str, url: str) -> None:
    try:
        from playwright.sync_api import sync_playwright

        with sync_playwright() as pw:
            profile = Path(config.data_dir) / "browser" / source_id
            profile.mkdir(parents=True, exist_ok=True)
            context = pw.chromium.launch_persistent_context(str(profile), headless=False)
            page = context.pages[0] if context.pages else context.new_page()
            page.goto(url, wait_until="domcontentloaded", timeout=60000)
            target = url.rstrip("/").lower()
            login_url = page.url.rstrip("/").lower()
            baseline = {cookie["name"] for cookie in context.cookies()}
            saw_login = _has_password(page) or _looks_like_login_url(login_url)
            deadline = time.time() + MAX_WAIT_SECONDS
            cookies: list[dict[str, Any]] = []
            stable = 0
            while time.time() < deadline:
                try:
                    if page.is_closed():
                        break
                    page.wait_for_timeout(1000)
                except Exception:  # noqa: BLE001 - window closed underneath us
                    break
                try:
                    cookies = context.cookies()
                except Exception:  # noqa: BLE001 - context already gone
                    break
                password = _has_password(page)
                current = page.url.rstrip("/").lower()
                if password or _looks_like_login_url(current):
                    saw_login = True
                gained = bool({c["name"] for c in cookies} - baseline)
                on_target = current.startswith(target)
                generic = (
                    saw_login
                    and not _looks_like_login_url(current)
                    and (current != login_url or gained)
                    and gained
                )
                need = 2 if (generic and saw_login) else 3
                if not password and (on_target or generic):
                    stable += 1
                    if stable >= need:
                        break
                else:
                    stable = 0
            try:
                cookies = context.cookies() or cookies
            except Exception:  # noqa: BLE001 - window already closed
                pass
            finally:
                try:
                    context.close()
                except Exception:  # noqa: BLE001
                    pass
        header = _build_cookie_header(cookies, url)
        if not header:
            raise RuntimeError(
                "没有取到该站点的 Cookie。请确认登录成功，并在页面上停留几秒再关闭窗口。"
            )
        row = repo.get_source(source_id) or {}
        source_config = loads(row.get("config_json"), {}) or {}
        source_config["cookie"] = header
        source_config["cookie_expires"] = _cookie_expiry(cookies, url) or ""
        source_config["cookie_refreshed_at"] = iso_utc()
        source_config["cookie_invalid"] = False
        source_config["cookie_checked_at"] = iso_utc()
        repo.update_source(source_id, {"config": source_config})
        with _LOCK:
            _STATE[source_id] = {"running": False, "error": None, "saved": len(cookies)}
    except Exception as exc:  # noqa: BLE001 - report to the UI
        with _LOCK:
            _STATE[source_id] = {"running": False, "error": str(exc), "saved": 0}
