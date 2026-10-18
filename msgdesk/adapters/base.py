"""Adapter protocol and HTTP fetching primitives."""

from __future__ import annotations

import urllib.error
import urllib.request
from dataclasses import dataclass, field
from typing import Any, Protocol

from ..errors import AppError
from ..models import (
    AdapterCapabilities,
    ConnectionStatus,
    Cursor,
    FetchResult,
    ValidationResult,
)

USER_AGENT = "MsgDesk/0.1 (+local personal reader)"


@dataclass
class FetchResponse:
    status: int
    body: bytes
    headers: dict[str, str] = field(default_factory=dict)
    url: str = ""


class Fetcher(Protocol):
    def fetch(
        self,
        url: str,
        *,
        etag: str | None = None,
        last_modified: str | None = None,
        timeout: float = 20.0,
    ) -> FetchResponse: ...


class HttpFetcher:
    def __init__(self, timeout: float = 20.0):
        self.timeout = timeout

    def fetch(
        self,
        url: str,
        *,
        etag: str | None = None,
        last_modified: str | None = None,
        timeout: float | None = None,
        headers: dict[str, str] | None = None,
    ) -> FetchResponse:
        request_headers = {"User-Agent": USER_AGENT, "Accept": "*/*"}
        if headers:
            request_headers.update({k: v for k, v in headers.items() if v})
        if etag:
            request_headers["If-None-Match"] = etag
        if last_modified:
            request_headers["If-Modified-Since"] = last_modified
        request = urllib.request.Request(url, headers=request_headers, method="GET")
        try:
            with urllib.request.urlopen(request, timeout=timeout or self.timeout) as response:
                body = response.read(8 * 1024 * 1024)
                return FetchResponse(
                    status=getattr(response, "status", 200),
                    body=body,
                    headers={k.lower(): v for k, v in response.headers.items()},
                    url=response.geturl(),
                )
        except urllib.error.HTTPError as exc:
            if exc.code == 304:
                return FetchResponse(
                    status=304,
                    body=b"",
                    headers={k.lower(): v for k, v in exc.headers.items()},
                    url=url,
                )
            code = "RATE_LIMITED" if exc.code == 429 else "REMOTE_TEMPORARY_ERROR"
            if exc.code in (401, 403):
                code = "PERMISSION_DENIED"
            raise AppError(code, f"HTTP {exc.code} for {url}") from exc
        except TimeoutError as exc:
            raise AppError("NETWORK_TIMEOUT", f"请求超时: {url}") from exc
        except urllib.error.URLError as exc:
            raise AppError("NETWORK_UNAVAILABLE", f"网络不可用: {url}") from exc


@dataclass
class SourceContext:
    id: str
    type: str
    name: str
    config: dict[str, Any]
    backfill_since: str | None = None


@dataclass
class FetchOptions:
    max_items: int = 50
    timeout_seconds: float = 20.0


class SourceAdapter(Protocol):
    def get_capabilities(self) -> AdapterCapabilities: ...

    def validate_config(self, config: dict[str, Any]) -> ValidationResult: ...

    def get_status(self, source: SourceContext | None = None) -> ConnectionStatus: ...

    def fetch_incremental(
        self,
        source: SourceContext,
        cursor: Cursor,
        options: FetchOptions,
    ) -> FetchResult: ...

    def close(self) -> None: ...


class BaseAdapter:
    adapter_type = "base"
    adapter_version = "0.1.0"

    def __init__(self, fetcher: Fetcher | None = None, **_: Any):
        self.fetcher = fetcher or HttpFetcher()

    def get_capabilities(self) -> AdapterCapabilities:
        return AdapterCapabilities(
            adapter_type=self.adapter_type, adapter_version=self.adapter_version
        )

    def validate_config(self, config: dict[str, Any]) -> ValidationResult:
        return ValidationResult(ok=True)

    def get_status(self, source: SourceContext | None = None) -> ConnectionStatus:
        return ConnectionStatus(status="ready", adapter_version=self.adapter_version)

    def close(self) -> None:
        return None

