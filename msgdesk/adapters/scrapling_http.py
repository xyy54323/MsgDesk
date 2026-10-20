"""Optional HTTP fetcher backed by Scrapling's sync FetcherSession."""

from __future__ import annotations

import urllib.error
from typing import Any

from ..errors import AppError
from .base import FetchResponse

MAX_BODY_BYTES = 8 * 1024 * 1024
DEPENDENCY = "scrapling[fetchers]==0.5"


def _dependency_error() -> AppError:
    return AppError(
        "DEPENDENCY_MISSING",
        "缺少 Scrapling 0.5 抓取依赖，请安装 requirements-scrapling.txt",
        {"dependency": DEPENDENCY},
    )


def _load_fetcher_session() -> type[Any]:
    try:
        from scrapling.fetchers import FetcherSession
    except Exception as exc:
        raise _dependency_error() from exc
    return FetcherSession


def _header_items(raw_headers: Any) -> list[tuple[str, str]]:
    if not raw_headers:
        return []
    if hasattr(raw_headers, "items"):
        return [(str(key).lower(), str(value)) for key, value in raw_headers.items()]
    return [(str(key).lower(), str(value)) for key, value in raw_headers]


def _body_bytes(raw_body: Any) -> bytes:
    if raw_body is None:
        return b""
    if isinstance(raw_body, bytes):
        return raw_body[:MAX_BODY_BYTES]
    if isinstance(raw_body, bytearray):
        return bytes(raw_body[:MAX_BODY_BYTES])
    if isinstance(raw_body, memoryview):
        return raw_body.tobytes()[:MAX_BODY_BYTES]
    if isinstance(raw_body, str):
        return raw_body.encode("utf-8")[:MAX_BODY_BYTES]
    try:
        return bytes(raw_body)[:MAX_BODY_BYTES]
    except (TypeError, ValueError):
        return b""


def _exception_code(exc: Exception) -> Any:
    code = getattr(exc, "code", None)
    if code is None:
        args = getattr(exc, "args", ())
        if args and isinstance(args[0], (int, str)):
            code = args[0]
    return code


def _is_timeout_error(exc: Exception) -> bool:
    if isinstance(exc, TimeoutError):
        return True
    name = type(exc).__name__.lower()
    if "timeout" in name or "timedout" in name:
        return True
    if "timeout" in str(exc).lower() or "timed out" in str(exc).lower():
        return True
    return str(_exception_code(exc)) in {"28", "110"}


def _is_network_error(exc: Exception) -> bool:
    if isinstance(exc, (ConnectionError, urllib.error.URLError)):
        return True
    name = type(exc).__name__.lower()
    if any(
        token in name
        for token in ("connect", "connection", "network", "dns", "resolve", "unreachable")
    ):
        return True
    return str(_exception_code(exc)) in {"5", "6", "7", "35", "52", "55", "56"}


def _translate_exception(exc: Exception, url: str) -> AppError:
    if _is_timeout_error(exc):
        return AppError("NETWORK_TIMEOUT", f"请求超时: {url}")
    if _is_network_error(exc):
        return AppError("NETWORK_UNAVAILABLE", f"网络不可用: {url}")
    return AppError("REMOTE_TEMPORARY_ERROR", f"远程请求失败: {url}")


class ScraplingHttpFetcher:
    def __init__(self, timeout: float = 20.0):
        self.timeout = timeout
        self._closed = False
        self._session_manager: Any | None = None
        self._session: Any | None = None

        fetcher_session = _load_fetcher_session()
        try:
            manager = fetcher_session(timeout=timeout)
            session = manager.__enter__()
        except ImportError as exc:
            raise _dependency_error() from exc
        except Exception as exc:
            raise AppError(
                "INTERNAL_ERROR",
                "初始化 Scrapling HTTP 会话失败",
                {"error_type": type(exc).__name__},
            ) from exc
        self._session_manager = manager
        self._session = session

    def fetch(
        self,
        url: str,
        *,
        etag: str | None = None,
        last_modified: str | None = None,
        timeout: float | None = None,
        headers: dict[str, str] | None = None,
    ) -> FetchResponse:
        session = self._session
        if session is None:
            raise AppError("INTERNAL_ERROR", "Scrapling HTTP Fetcher 已关闭")

        request_headers: dict[str, str] = {}
        if etag:
            request_headers["If-None-Match"] = etag
        if last_modified:
            request_headers["If-Modified-Since"] = last_modified
        if headers:
            user_headers = {
                str(key): str(value) for key, value in headers.items() if value is not None
            }
            user_names = {key.lower() for key in user_headers}
            request_headers = {
                key: value
                for key, value in request_headers.items()
                if key.lower() not in user_names
            }
            request_headers.update(user_headers)

        try:
            response = session.get(
                url,
                headers=request_headers,
                timeout=timeout if timeout is not None else self.timeout,
            )
        except AppError:
            raise
        except Exception as exc:
            raise _translate_exception(exc, url) from exc

        try:
            status = int(getattr(response, "status", 200) or 200)
        except (TypeError, ValueError):
            status = 200
        if status == 304:
            body = b""
        else:
            body = _body_bytes(getattr(response, "body", b""))
        final_url = getattr(response, "url", None) or url
        response_headers = dict(_header_items(getattr(response, "headers", {})))

        if status in (401, 403):
            raise AppError("PERMISSION_DENIED", f"HTTP {status} for {url}")
        if status == 429:
            raise AppError("RATE_LIMITED", f"HTTP {status} for {url}")
        if status >= 400:
            raise AppError("REMOTE_TEMPORARY_ERROR", f"HTTP {status} for {url}")

        return FetchResponse(
            status=status,
            body=body,
            headers=response_headers,
            url=str(final_url),
        )

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        session = self._session
        manager = self._session_manager
        self._session = None
        self._session_manager = None

        close = getattr(session, "close", None) if session is not None else None
        if callable(close):
            try:
                close()
                return
            except Exception:
                pass

        exit_session = getattr(manager, "__exit__", None) if manager is not None else None
        if callable(exit_session):
            try:
                exit_session(None, None, None)
            except Exception:
                pass
