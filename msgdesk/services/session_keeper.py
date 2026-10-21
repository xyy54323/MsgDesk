"""Keep logged-in browser profiles warm so their cookies keep renewing."""

from __future__ import annotations

import logging
import threading
from pathlib import Path
from typing import Any

from ..repository import loads
from ..timeutil import iso_utc, parse_datetime, utc_now
from .browser_login import _build_cookie_header, _cookie_expiry, _has_password, _looks_like_login_url

log = logging.getLogger("msgdesk.session")

TICK_SECONDS = 60
MIN_INTERVAL = 120
DEFAULT_INTERVAL = 1800


class SessionKeeper:
    def __init__(self, config: Any, repo: Any) -> None:
        self.config = config
        self.repo = repo

    def run_forever(self, stop_event: threading.Event) -> None:
        while not stop_event.is_set():
            try:
                self.refresh_once(only_due=True)
            except Exception:  # noqa: BLE001 - keep the loop alive
                log.exception("session keep-alive failed")
            stop_event.wait(TICK_SECONDS)

    def _is_due(self, source_config: dict[str, object]) -> bool:
        refreshed = parse_datetime(str(source_config.get("cookie_refreshed_at") or ""))
        if refreshed is None:
            return True
        expires = parse_datetime(str(source_config.get("cookie_expires") or ""))
        if expires is not None:
            lifetime = max(0.0, (expires - refreshed).total_seconds())
            wait = max(MIN_INTERVAL, lifetime / 2)
        else:
            wait = DEFAULT_INTERVAL
        return (utc_now() - refreshed).total_seconds() >= wait

    def refresh_once(self, only_due: bool = False) -> int:
        """Visit each logged-in source so the site refreshes its session."""
        from playwright.sync_api import sync_playwright

        refreshed = 0
        for source in self.repo.list_sources():
            profile = Path(self.config.data_dir) / "browser" / source["id"]
            if not profile.is_dir():
                continue
            source_config = loads(source.get("config_json"), {}) or {}
            url = str(source_config.get("url") or "").strip()
            if not url.startswith(("http://", "https://")):
                continue
            if only_due and source_config.get("cookie_invalid"):
                continue
            if only_due and not self._is_due(source_config):
                continue
            try:
                with sync_playwright() as pw:
                    context = pw.chromium.launch_persistent_context(
                        str(profile), headless=True
                    )
                    authenticated = False
                    try:
                        page = context.pages[0] if context.pages else context.new_page()
                        page.goto(url, wait_until="domcontentloaded", timeout=60000)
                        page.wait_for_timeout(1500)
                        cookies = context.cookies()
                        header = _build_cookie_header(cookies, url)
                        expiry = _cookie_expiry(cookies, url) or ""
                        authenticated = (
                            bool(header)
                            and not _looks_like_login_url(page.url)
                            and not _has_password(page)
                        )
                    finally:
                        context.close()
            except Exception:  # noqa: BLE001 - one source must not stop others
                log.exception("keep-alive failed for source %s", source["id"])
                continue
            if not authenticated:
                source_config["cookie_invalid"] = True
                source_config["cookie_checked_at"] = iso_utc()
                self.repo.update_source(source["id"], {"config": source_config})
                continue
            if header:
                source_config["cookie"] = header
                source_config["cookie_expires"] = expiry
                source_config["cookie_refreshed_at"] = iso_utc()
                source_config["cookie_invalid"] = False
                self.repo.update_source(source["id"], {"config": source_config})
                refreshed += 1
        return refreshed
