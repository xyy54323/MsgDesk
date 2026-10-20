"""Loopback-only HTTP server and static frontend."""

from __future__ import annotations

import json
import logging
import mimetypes
import traceback
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

from .. import __version__
from ..errors import AppError
from ..paths import package_root
from ..runtime import Runtime
from .routes import ApiRouter

log = logging.getLogger("msgdesk.api")
STATIC_DIR = package_root() / "api" / "static"
MAX_BODY = 2 * 1024 * 1024


class ApiHandler(BaseHTTPRequestHandler):
    runtime: Runtime
    router: ApiRouter
    server_version = f"MsgDesk/{__version__}"

    def do_GET(self) -> None:
        self._handle("GET")

    def do_POST(self) -> None:
        self._handle("POST")

    def do_PATCH(self) -> None:
        self._handle("PATCH")

    def do_DELETE(self) -> None:
        self._handle("DELETE")

    def _handle(self, method: str) -> None:
        parsed = urllib.parse.urlparse(self.path)
        path = urllib.parse.unquote(parsed.path)
        query = urllib.parse.parse_qs(parsed.query)
        try:
            if path.startswith("/api/"):
                if method == "GET" and path == "/api/qq/qr":
                    self._serve_qq_qr()
                    return
                body = self._read_body() if method in ("POST", "PATCH", "PUT") else {}
                result = self.router.route(method, path, query, body)
                if result is not None:
                    status, payload = result
                    self._json(payload, status)
                return
            self._serve_static(path)
        except AppError as exc:
            status = {
                "NOT_FOUND": 404,
                "AUTH_REQUIRED": 401,
                "AUTH_EXPIRED": 401,
                "PERMISSION_DENIED": 403,
                "VALIDATION_ERROR": 400,
                "CONFIG_INVALID": 400,
            }.get(exc.code, 502 if exc.retryable else 500)
            self._json(exc.to_dict(), status)
        except json.JSONDecodeError:
            self._json({"code": "VALIDATION_ERROR", "message": "请求体不是有效 JSON"}, 400)
        except BrokenPipeError:
            return
        except Exception as exc:
            log.error("api error %s %s: %s", method, path, traceback.format_exc())
            self._json(
                {"code": "INTERNAL_ERROR", "message": f"{type(exc).__name__}: {exc}"}, 500
            )

    # HTTP helpers -------------------------------------------------------
    def _read_body(self) -> dict[str, Any]:
        length = int(self.headers.get("Content-Length") or 0)
        if length > MAX_BODY:
            raise AppError("VALIDATION_ERROR", "请求体过大")
        if length <= 0:
            return {}
        raw = self.rfile.read(length)
        value = json.loads(raw.decode("utf-8"))
        if not isinstance(value, dict):
            raise AppError("VALIDATION_ERROR", "请求体必须是 JSON 对象")
        return value

    def _json(self, payload: Any, status: int = 200) -> None:
        data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(data)

    def _serve_static(self, path: str) -> None:
        if path in ("/", "/index.html"):
            file_path = STATIC_DIR / "index.html"
        elif path.startswith("/media/"):
            base = (self.runtime.config.data_dir / "media").resolve()
            file_path = (base / path[len("/media/"):]).resolve()
            if base not in file_path.parents:
                self._json({"code": "NOT_FOUND", "message": "Not found"}, 404)
                return
        elif path.startswith("/uploads/"):
            base = (self.runtime.config.data_dir / "uploads").resolve()
            file_path = (base / path[len("/uploads/"):]).resolve()
            if base not in file_path.parents:
                self._json({"code": "NOT_FOUND", "message": "Not found"}, 404)
                return
        elif path.startswith("/attachments/"):
            base = (self.runtime.config.data_dir / "attachments").resolve()
            file_path = (base / path[len("/attachments/"):]).resolve()
            if base not in file_path.parents:
                self._json({"code": "NOT_FOUND", "message": "Not found"}, 404)
                return
        else:
            relative = path.lstrip("/")
            if relative.startswith("static/"):
                relative = relative[len("static/"):]
            file_path = (STATIC_DIR / relative).resolve()
            if STATIC_DIR.resolve() not in file_path.parents:
                self._json({"code": "NOT_FOUND", "message": "Not found"}, 404)
                return
        if not file_path.exists() or not file_path.is_file():
            self._json({"code": "NOT_FOUND", "message": "Not found"}, 404)
            return
        data = file_path.read_bytes()
        content_type, _ = mimetypes.guess_type(str(file_path))
        self.send_response(200)
        self.send_header("Content-Type", (content_type or "text/plain") + "; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(data)

    def _serve_qq_qr(self) -> None:
        from ..services import qq_bridge

        if not qq_bridge.qr_ready():
            raise AppError("NOT_FOUND", "二维码正在生成，稍后再试")
        body = qq_bridge.QR_PATH.read_bytes()
        self.send_response(200)
        self.send_header("Content-Type", "image/png")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, fmt: str, *args: Any) -> None:
        log.info("%s - %s", self.address_string(), fmt % args)


class ApiServer(ThreadingHTTPServer):
    daemon_threads = True
    # Windows honours SO_REUSEADDR by letting two processes bind :8080, which
    # silently splits requests between two different tokens.  Fail loudly
    # instead so a second instance cannot hijack a running one.
    allow_reuse_address = False

    def __init__(self, address: tuple[str, int], runtime: Runtime):
        self.runtime = runtime
        self.router = ApiRouter(runtime, self.shutdown)
        handler = type("BoundApiHandler", (ApiHandler,), {"runtime": runtime, "router": self.router})
        super().__init__(address, handler)


def create_server(runtime: Runtime) -> ApiServer:
    host = runtime.config.system.bind_host
    port = int(runtime.config.system.port)
    return ApiServer((host, port), runtime)
