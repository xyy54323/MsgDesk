"""Loopback-only JSON API and static workbench."""

from __future__ import annotations

import base64
import json
import logging
import mimetypes
import os
import re
import shutil
import threading
import traceback
import urllib.error
import urllib.parse
import urllib.request
import uuid
from copy import copy
from datetime import timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

from ..adapters import create_adapter, list_adapter_capabilities
from ..adapters.base import SourceContext
from ..config import (
    ModelProviderConfig,
    activate_model_provider,
    merge_config_file,
    model_config_from_dict,
    model_provider_dict,
    NotificationChannelConfig,
    redact_secrets,
)
from ..errors import AppError
from ..repository import Repository, loads
from ..runtime import Runtime
from ..services.model import ModelInput as ModelInputType, create_model_provider
from ..services.notifications import PRIORITY_RANK, build_channel
from ..timeutil import iso_utc, local_date_of, local_zone, parse_datetime, utc_now

log = logging.getLogger("msgdesk.api")
STATIC_DIR = Path(__file__).resolve().parent / "static"
MAX_BODY = 2 * 1024 * 1024


def _mask_key(value: str) -> str:
    """Return a display-only fingerprint so the UI can prove a key is saved."""
    text = str(value or "")
    if not text:
        return ""
    if len(text) <= 8:
        return "*" * len(text)
    return f"{text[:3]}…{text[-4:]}"


def _model_public(provider: ModelProviderConfig) -> dict[str, Any]:
    result = dict(model_provider_dict(provider))
    result.pop("api_key", None)
    # Environment-backed keys are intentionally reported by the config layer;
    # this route only needs to avoid returning the secret itself.
    import os
    key = provider.api_key or os.environ.get(provider.api_key_env, "")
    result["api_key_set"] = bool(key)
    result["api_key_hint"] = _mask_key(key)
    return result


def _model_id(value: str) -> str:
    value = re.sub(r"[^a-zA-Z0-9_-]+", "-", value.strip().lower()).strip("-_")
    return value[:48] or "provider"


class ApiHandler(BaseHTTPRequestHandler):
    runtime: Runtime
    server_version = "MsgDesk/0.2"

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
                if not self._authorized(path, query):
                    self._json({"code": "AUTH_REQUIRED", "message": "需要访问令牌"}, 401)
                    return
                body = self._read_body() if method in ("POST", "PATCH", "PUT") else {}
                result = self._route_api(method, path, query, body)
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

    # Auth ---------------------------------------------------------------
    def _authorized(self, path: str, query: dict[str, list[str]]) -> bool:
        config = self.runtime.config.system
        if not config.require_auth or path == "/api/health":
            return True
        expected = config.api_token
        if not expected:
            return True
        candidates = [
            self.headers.get("X-API-Token"),
            self.headers.get("Authorization", "").removeprefix("Bearer ").strip(),
            query.get("token", [None])[0],
        ]
        cookie = self.headers.get("Cookie", "")
        match = re.search(r"msgdesk_token=([^;]+)", cookie)
        if match:
            candidates.append(urllib.parse.unquote(match.group(1)))
        return expected in [c for c in candidates if c]

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

    def log_message(self, fmt: str, *args: Any) -> None:
        log.info("%s - %s", self.address_string(), fmt % args)

    # Routing ------------------------------------------------------------
    def _route_api(
        self, method: str, path: str, query: dict[str, list[str]], body: dict[str, Any]
    ) -> tuple[int, Any] | None:
        repo = self.runtime.repo
        config = self.runtime.config

        if method == "GET" and path == "/api/health":
            disk = shutil.disk_usage(config.data_dir)
            return 200, {
                "status": "ok",
                "version": "0.2.0",
                "schema_version": self.runtime.db.schema_version(),
                "time": iso_utc(),
                "data_dir": str(config.data_dir),
                "disk_free_mb": round(disk.free / 1024 / 1024, 1),
            }

        if method == "GET" and path == "/api/status":
            sources = repo.list_sources()
            now = utc_now()
            for source in sources:
                reference = parse_datetime(
                    source.get("last_success_at") or source.get("last_error_at")
                )
                if reference:
                    source["next_run_at"] = iso_utc(
                        reference + timedelta(seconds=int(source.get("interval_seconds") or 600))
                    )
                else:
                    source["next_run_at"] = iso_utc(now)
                _redact_source(source)
            return 200, {
                "stats": repo.stats(),
                "sources": sources,
                "jobs": [_public_job(j) for j in repo.list_jobs(12)],
                "digests": repo.list_digests(5),
                "config": config.public_dict(),
                "today": utc_now().astimezone(local_zone(config.system.timezone)).date().isoformat(),
            }

        if method == "POST" and path == "/api/admin/shutdown":
            threading.Thread(target=self.server.shutdown, daemon=True).start()
            return 200, {"status": "stopping"}

        if path == "/api/adapters" and method == "GET":
            return 200, {"adapters": list_adapter_capabilities()}

        # Sources
        if path == "/api/sources" and method == "GET":
            sources = repo.list_sources()
            for source in sources:
                cursor = repo.get_cursor_row(source["id"])
                source["cursor"] = dict(cursor) if cursor else None
                source["active_job"] = repo.has_active_job("sync_source", source_id=source["id"])
                _redact_source(source)
            return 200, {"sources": sources}

        if path == "/api/sources" and method == "POST":
            payload = self._validate_source(body)
            created = repo.create_source(payload)
            repo.audit("source.create", "source", created["id"], {"name": created["name"]})
            return 201, _redact_source(created)

        match = re.fullmatch(r"/api/sources/([^/]+)", path)
        if match:
            source_id = match.group(1)
            source = repo.get_source(source_id)
            if source is None:
                raise AppError("NOT_FOUND", "来源不存在")
            _redact_source(source)
            if method == "GET":
                cursor = repo.get_cursor_row(source_id)
                source["cursor"] = dict(cursor) if cursor else None
                return 200, source
            if method == "PATCH":
                target_type = str(
                    body.get("type") or body.get("adapter_type") or source["adapter_type"]
                ).strip()
                if target_type != source["adapter_type"] and "config" not in body:
                    raise AppError(
                        "VALIDATION_ERROR", "切换来源类型时必须同时提供采集配置"
                    )
                if "config" in body:
                    raw_source = repo.get_source(source_id) or {}
                    existing_config = loads(raw_source.get("config_json"), {}) or {}
                    incoming_config = body.get("config") or {}
                    if isinstance(incoming_config, dict) and isinstance(existing_config, dict):
                        merged_config = dict(existing_config)
                        for key, value in incoming_config.items():
                            if value == "***" and key in merged_config:
                                continue
                            merged_config[key] = value
                        body["config"] = merged_config
                    self._validate_adapter_config(
                        target_type, body["config"]
                    )
                if target_type != source["adapter_type"]:
                    body["type"] = target_type
                    body["adapter_type"] = target_type
                    repo.reset_cursor(source_id)
                updated = repo.update_source(source_id, body)
                if updated is None:
                    raise AppError("NOT_FOUND", "来源不存在")
                new_category = str(updated.get("default_category") or "").strip()
                old_category = str(source.get("default_category") or "").strip()
                if new_category and new_category != old_category:
                    repo.reassign_message_category(source_id, new_category)
                    day = utc_now().astimezone(
                        local_zone(config.system.timezone)
                    ).date().isoformat()
                    if not repo.has_active_digest_job(day):
                        repo.enqueue_job(
                            "generate_digest", payload={"date": day}, priority=150
                        )
                repo.audit("source.update", "source", source_id, body)
                return 200, _redact_source(updated)
            if method == "DELETE":
                purge = str(query.get("purge", ["false"])[0]).lower() == "true"
                if purge:
                    repo.purge_source(source_id)
                else:
                    repo.soft_delete_source(source_id)
                repo.audit("source.delete", "source", source_id, {"purge": purge})
                return 200, {"deleted": source_id, "purged": purge}

        login_match = re.fullmatch(r"/api/sources/([^/]+)/login", path)
        if login_match and method == "POST":
            from ..services import browser_login

            source_id = login_match.group(1)
            row = repo.get_source(source_id)
            if row is None:
                raise AppError("NOT_FOUND", "来源不存在")
            source_config = loads(row.get("config_json"), {}) or {}
            url = str(source_config.get("url") or "").strip()
            if not url:
                raise AppError("VALIDATION_ERROR", "请先填写来源地址")
            started = browser_login.start(config, repo, source_id, url)
            return 200, {"started": started, **browser_login.status(source_id)}

        login_status_match = re.fullmatch(r"/api/sources/([^/]+)/login-status", path)
        if login_status_match and method == "GET":
            from ..services import browser_login

            return 200, browser_login.status(login_status_match.group(1))

        match = re.fullmatch(r"/api/sources/([^/]+)/(test|sync)", path)
        if match and method == "POST":
            source_id, action = match.group(1), match.group(2)
            source = repo.get_source(source_id)
            if source is None:
                raise AppError("NOT_FOUND", "来源不存在")
            adapter = create_adapter(source["adapter_type"])
            try:
                source_config = loads(source.get("config_json"), {}) or {}
                context = SourceContext(
                    id=source["id"],
                    type=source["type"],
                    name=source["name"],
                    config=source_config,
                    backfill_since=source.get("backfill_since"),
                )
                validation = adapter.validate_config(source_config)
                if action == "test":
                    if source["adapter_type"] in ("web", "ai_web") and validation.ok:
                        from ..adapters.base import HttpFetcher
                        from ..adapters.web import _parse_page

                        target = str(source_config.get("url") or "").strip()
                        cookie = str(source_config.get("cookie") or "")
                        headers = (
                            {"Cookie": cookie}
                            if cookie and cookie != "***"
                            else None
                        )
                        try:
                            response = HttpFetcher().fetch(
                                target, timeout=30.0, headers=headers
                            )
                            parser = _parse_page(response.body, response.url or target)
                        except AppError as exc:
                            return 200, {
                                "validation": {"ok": False, "message": exc.message},
                                "capabilities": adapter.get_capabilities().to_dict(),
                                "status": {"status": "error"},
                            }
                        title = " ".join(parser.title_parts).strip()
                        body_text = "\n".join(parser.paragraphs)
                        needs_login = len(body_text) < 300 and "登录" in (
                            title + body_text[:80]
                        )
                        source_config["cookie_invalid"] = needs_login
                        source_config["cookie_checked_at"] = iso_utc()
                        repo.update_source(source_id, {"config": source_config})
                        return 200, {
                            "validation": {
                                "ok": not needs_login,
                                "message": (
                                    f"可访问：{title or target}，正文 {len(body_text)} 字"
                                    if not needs_login
                                    else "页面要求登录，Cookie 可能已失效"
                                ),
                            },
                            "capabilities": adapter.get_capabilities().to_dict(),
                            "status": {
                                "status": "auth_required" if needs_login else "ready",
                                "client_version": parser.meta.get("generator"),
                            },
                        }
                    status = adapter.get_status(context)
                    return 200, {
                        "validation": validation.to_dict(),
                        "capabilities": adapter.get_capabilities().to_dict(),
                        "status": status.to_dict(),
                    }
                if not validation.ok:
                    raise AppError("CONFIG_INVALID", validation.message)
                if repo.has_active_job("sync_source", source_id=source_id):
                    return 202, {"status": "already_running"}
                job_id = repo.enqueue_job(
                    "sync_source",
                    source_id=source_id,
                    payload={"max_items": int(body.get("max_items", 50))},
                    priority=40,
                )
                return 202, {"job_id": job_id, "status": "queued"}
            finally:
                adapter.close()

        # Accounts
        if path == "/api/accounts" and method == "GET":
            return 200, {"accounts": repo.list_accounts()}
        if path == "/api/accounts" and method == "POST":
            if not body.get("platform") or not body.get("display_name"):
                raise AppError("VALIDATION_ERROR", "platform 和 display_name 必填")
            account_id = repo.create_account(body)
            return 201, {"id": account_id}

        # Jobs
        if path == "/api/jobs" and method == "GET":
            status = query.get("status", [None])[0]
            limit = int(query.get("limit", ["50"])[0])
            names = {item["id"]: item["name"] for item in repo.list_sources()}
            jobs = []
            for job in repo.list_jobs(limit, status):
                public = _public_job(job)
                public["source_name"] = names.get(job.get("source_id"))
                jobs.append(public)
            return 200, {"jobs": jobs}

        if path == "/api/traces" and method == "GET":
            limit = int(query.get("limit", ["200"])[0])
            job_id = query.get("job_id", [None])[0]
            source_id = query.get("source_id", [None])[0]
            scope = query.get("scope", [None])[0]
            return 200, {"traces": repo.list_traces(limit, job_id, source_id, scope)}
        if path == "/api/jobs/run" and method == "POST":
            job_type = body.get("type")
            if job_type not in ("sync_source", "process_message", "generate_digest",
                                "send_notification"):
                raise AppError("VALIDATION_ERROR", "未知任务类型")
            job_id = repo.enqueue_job(
                job_type,
                source_id=body.get("source_id"),
                message_id=body.get("message_id"),
                payload=body.get("payload") or {},
                priority=int(body.get("priority", 60)),
            )
            return 202, {"job_id": job_id}
        match = re.fullmatch(r"/api/jobs/([^/]+)(?:/(retry|cancel))?", path)
        if match:
            job_id, action = match.group(1), match.group(2)
            job = repo.get_job(job_id)
            if job is None:
                raise AppError("NOT_FOUND", "任务不存在")
            if method == "GET" and not action:
                return 200, _public_job(job)
            if method == "POST" and action == "retry":
                repo.retry_job(job_id)
                return 200, {"job_id": job_id, "status": "pending"}
            if method == "POST" and action == "cancel":
                repo.cancel_job(job_id)
                return 200, {"job_id": job_id, "status": "cancelled"}

        # Messages
        if path == "/api/messages" and method == "GET":
            limit = int(query.get("limit", ["50"])[0])
            offset = int(query.get("offset", ["0"])[0])
            priority_text = query.get("priority", [""])[0]
            priorities = [p.strip() for p in priority_text.split(",") if p.strip()]
            messages = repo.list_messages(
                limit=limit,
                offset=offset,
                source_id=query.get("source_id", [None])[0],
                status=query.get("status", [None])[0],
                category=query.get("category", [None])[0],
                date_text=query.get("date", [None])[0],
                query_text=query.get("q", [None])[0],
                priorities=priorities,
                unread_only=str(query.get("unread", ["0"])[0]).lower() in ("1", "true"),
                todo_only=str(query.get("todo", ["0"])[0]).lower() in ("1", "true"),
                ignored_only=str(query.get("ignored", ["0"])[0]).lower() in ("1", "true"),
                zone_name=config.system.timezone,
            )
            return 200, {"messages": messages, "limit": limit, "offset": offset}

        if path == "/api/messages/batch-read" and method == "POST":
            updated = repo.mark_all_messages_read()
            return 200, {"updated": updated}

        if path == "/api/messages/batch-ignore" and method == "POST":
            ids = [str(item) for item in (body.get("ids") or [])]
            ignored = bool(body.get("ignored", True))
            for message_id in ids:
                repo.set_message_ignored(message_id, ignored)
            day = utc_now().astimezone(
                local_zone(config.system.timezone)
            ).date().isoformat()
            if not repo.has_active_digest_job(day):
                repo.enqueue_job("generate_digest", payload={"date": day}, priority=150)
            return 200, {"updated": len(ids), "ignored": ignored}

        if path == "/api/messages/batch-process" and method == "POST":
            if body.get("ids"):
                ids = [str(i) for i in body["ids"]]
            else:
                rows = repo.list_messages(limit=int(body.get("limit", 50)),
                                          status=body.get("status", "pending"))
                ids = [row["id"] for row in rows]
            job_ids = [
                repo.enqueue_job("process_message", message_id=mid, payload={"force": True},
                                 priority=70)
                for mid in ids
            ]
            return 202, {"queued": len(job_ids), "job_ids": job_ids}

        if path == "/api/messages/batch-delete" and method == "POST":
            ids = [str(value) for value in (body.get("ids") or []) if value]
            if not ids:
                raise AppError("VALIDATION_ERROR", "ids 不能为空")
            purge = bool(body.get("purge", False))
            removed = repo.delete_messages(ids, purge=purge)
            repo.audit(
                "message.batch_delete",
                "message",
                None,
                {"count": removed, "purge": purge},
            )
            return 200, {"deleted": removed, "purged": purge}

        match = re.fullmatch(r"/api/messages/([^/]+)(?:/(process))?", path)
        if match:
            message_id, action = match.group(1), match.group(2)
            message = repo.get_message(message_id)
            if message is None:
                raise AppError("NOT_FOUND", "消息不存在")
            if method == "GET" and not action:
                return 200, message
            if method == "PATCH" and not action:
                if "is_read" in body:
                    repo.set_message_read(message_id, bool(body["is_read"]))
                if "ignored" in body:
                    repo.set_message_ignored(message_id, bool(body["ignored"]))
                    row = repo.get_message(message_id) or {}
                    if row.get("collected_at"):
                        day = local_date_of(
                            row["collected_at"], config.system.timezone
                        )
                        if not repo.has_active_digest_job(day):
                            repo.enqueue_job(
                                "generate_digest",
                                payload={"date": day},
                                priority=150,
                            )
                if body.get("hidden"):
                    repo.hide_message(message_id)
                processing_fields = {
                    key: value
                    for key, value in body.items()
                    if key
                    in {
                        "category",
                        "subcategory",
                        "tags",
                        "summary_short",
                        "summary_long",
                        "action_items",
                        "deadline_at",
                        "priority",
                        "urgency",
                        "processing_status",
                    }
                }
                if processing_fields:
                    repo.override_processing(message_id, processing_fields)
                    repo.audit(
                        "message.override", "message", message_id, processing_fields
                    )
                updated = repo.get_message(message_id)
                return 200, updated
            if method == "POST" and action == "process":
                result = self.runtime.processing_service.process(
                    message_id, force=bool(body.get("force", True))
                )
                return 200, result
            if method == "DELETE" and not action:
                purge = str(query.get("purge", ["false"])[0]).lower() == "true"
                repo.delete_message(message_id, purge=purge)
                repo.audit(
                    "message.delete", "message", message_id, {"purge": purge}
                )
                return 200, {"deleted": message_id, "purged": purge}

        if path == "/api/models/vision" and method == "GET":
            from ..services.model import probe_vision

            active = config.model
            if not active.base_url or not active.model:
                return 200, {"supported": False, "reason": "未配置模型"}
            return 200, {
                "supported": probe_vision(active),
                "model": active.model,
            }

        if path == "/api/uploads" and method == "POST":
            filename = str(body.get("filename") or "").strip()
            payload_b64 = str(body.get("data_base64") or "").strip()
            if not filename or not payload_b64:
                raise AppError("VALIDATION_ERROR", "filename 和 data_base64 必填")
            try:
                raw = base64.b64decode(payload_b64, validate=True)
            except Exception:
                raise AppError("VALIDATION_ERROR", "文件内容不是有效的 base64") from None
            if len(raw) > 10 * 1024 * 1024:
                raise AppError("VALIDATION_ERROR", "单个文件不能超过 10MB")
            safe = re.sub(r"[^A-Za-z0-9._-]+", "_", Path(filename).name) or "file"
            target_dir = config.data_dir / "uploads"
            target_dir.mkdir(parents=True, exist_ok=True)
            target = target_dir / f"{uuid.uuid4().hex[:8]}-{safe}"
            target.write_bytes(raw)
            return 201, {
                "name": filename,
                "path": f"/uploads/{target.name}",
                "size": len(raw),
            }

        # Models: only explicitly configured remote providers are listed.
        if path == "/api/models" and method == "GET":
            providers = [_model_public(item) for item in config.model.providers]
            return 200, {
                "providers": providers,
                "default_provider_id": config.model.default_provider_id,
                "active": _model_public(config.model) if config.model.provider else None,
                "provider": self.runtime.model.name,
                "model": self.runtime.model.model_name,
                "config": config.public_dict()["model"],
                "adapters": list_adapter_capabilities(),
            }

        key_match = re.fullmatch(r"/api/models/([^/]+)/key", path)
        if key_match and method == "GET":
            # The workbench is loopback-only and token protected; the editor
            # needs the stored key to render it masked and to reveal on demand.
            provider_id = key_match.group(1)
            provider = next(
                (item for item in config.model.providers if item.id == provider_id),
                None,
            )
            if provider is None:
                raise AppError("NOT_FOUND", "模型服务商不存在")
            stored = provider.api_key or os.environ.get(provider.api_key_env, "")
            if not stored:
                raise AppError("NOT_FOUND", "该供应商尚未保存 API Key")
            repo.audit("model.key.read", "model", provider_id, {})
            return 200, {"api_key": stored}

        model_match = re.fullmatch(r"/api/models/([^/]+)", path)
        if model_match and method in ("PATCH", "DELETE"):
            provider_id = model_match.group(1)
            existing = next(
                (item for item in config.model.providers if item.id == provider_id), None
            )
            if existing is None:
                raise AppError("NOT_FOUND", "模型服务商不存在")
            if method == "DELETE":
                config.model.providers = [
                    item for item in config.model.providers if item.id != provider_id
                ]
                if config.model.default_provider_id == provider_id:
                    config.model.default_provider_id = ""
                selected = next(
                    (item for item in config.model.providers
                     if item.id == config.model.default_provider_id),
                    None,
                )
                activate_model_provider(config.model, selected)
                self._persist_model_settings()
                self.runtime.apply_model_config()
                repo.audit("model.delete", "model", provider_id, {})
                return 200, {"deleted": provider_id}
            provider = self._validate_model_provider(body, existing=existing)
            provider.id = provider_id
            if body.get("clear_api_key"):
                provider.api_key = ""
            index = config.model.providers.index(existing)
            config.model.providers[index] = provider
            if config.model.default_provider_id == provider_id:
                activate_model_provider(config.model, provider)
            self._persist_model_settings()
            self.runtime.apply_model_config()
            repo.audit("model.update", "model", provider_id, {"base_url": provider.base_url, "model": provider.model})
            return 200, _model_public(provider)

        default_match = re.fullmatch(r"/api/models/([^/]+)/default", path)
        if default_match and method == "POST":
            provider_id = default_match.group(1)
            provider = next(
                (item for item in config.model.providers if item.id == provider_id), None
            )
            if provider is None:
                raise AppError("NOT_FOUND", "模型服务商不存在")
            config.model.default_provider_id = provider_id
            activate_model_provider(config.model, provider)
            self._persist_model_settings()
            self.runtime.apply_model_config()
            repo.audit("model.default", "model", provider_id, {})
            return 200, {"default_provider_id": provider_id, "active": _model_public(provider)}

        if path == "/api/models" and method == "POST":
            provider = self._validate_model_provider(body)
            requested_id = str(body.get("id", "") or "").strip()
            provider.id = requested_id or _model_id(provider.display_name)
            if any(item.id == provider.id for item in config.model.providers):
                raise AppError("VALIDATION_ERROR", "模型服务商 ID 已存在")
            config.model.providers.append(provider)
            if not config.model.default_provider_id or body.get("set_default"):
                config.model.default_provider_id = provider.id
                activate_model_provider(config.model, provider)
            self._persist_model_settings()
            self.runtime.apply_model_config()
            repo.audit("model.create", "model", provider.id, {"base_url": provider.base_url, "model": provider.model})
            return 200, {
                **_model_public(provider),
                "status": "saved",
                "persisted": config.config_path is not None,
                "provider": self.runtime.model.name,
                "model": self.runtime.model.model_name,
                "config": config.public_dict()["model"],
            }

        if path == "/api/models/fetch" and method == "POST":
            # Fetch model IDs from the currently edited provider without saving it.
            fetch_id = str(body.get("id", "") or "").strip()
            existing = None
            if fetch_id:
                existing = next(
                    (item for item in config.model.providers if item.id == fetch_id), None
                )
                if existing is None:
                    raise AppError("NOT_FOUND", "模型服务商不存在")
            provider = self._validate_model_provider(
                body, existing=existing, require_model=False
            )
            api_key = provider.api_key or os.environ.get(provider.api_key_env, "")
            if not api_key:
                raise AppError("AUTH_REQUIRED", "请先填写 API Key")
            request = urllib.request.Request(
                provider.base_url.rstrip("/") + "/models",
                headers={
                    "Accept": "application/json",
                    "Authorization": f"Bearer {api_key}",
                },
                method="GET",
            )
            try:
                with urllib.request.urlopen(request, timeout=provider.timeout_seconds) as response:
                    remote = json.loads(response.read().decode("utf-8"))
            except urllib.error.HTTPError as exc:
                code = "AUTH_EXPIRED" if exc.code in (401, 403) else (
                    "RATE_LIMITED" if exc.code == 429 else "REMOTE_TEMPORARY_ERROR"
                )
                raise AppError(code, f"模型列表请求失败: HTTP {exc.code}") from exc
            except urllib.error.URLError as exc:
                raise AppError("NETWORK_UNAVAILABLE", "模型列表接口不可达") from exc
            except TimeoutError as exc:
                raise AppError("NETWORK_TIMEOUT", "获取模型列表超时") from exc
            except json.JSONDecodeError as exc:
                raise AppError("SCHEMA_INVALID", "模型列表接口返回不是 JSON") from exc
            rows = remote.get("data") if isinstance(remote, dict) else None
            if not isinstance(rows, list):
                raise AppError("SCHEMA_INVALID", "模型列表接口未返回 data 数组")
            models: list[str] = []
            for row in rows:
                if isinstance(row, dict):
                    value = row.get("id") or row.get("name")
                else:
                    value = row
                value = str(value or "").strip()
                if value and value not in models:
                    models.append(value)
            return 200, {"models": models}

        if path == "/api/models/test" and method == "POST":
            # Test the unsaved form payload. A missing model is an error; the
            # old rules fallback must never report a successful connection.
            test_id = str(body.get("id", "") or "").strip()
            existing = None
            if test_id:
                existing = next(
                    (item for item in config.model.providers if item.id == test_id), None
                )
                if existing is None:
                    raise AppError("NOT_FOUND", "模型服务商不存在")
            provider = self._validate_model_provider(body, existing=existing)
            test_config = copy(config.model)
            for key in (
                "id", "display_name", "note", "official_url", "provider", "protocol",
                "base_url", "model", "model_list", "api_key_env", "api_key",
                "timeout_seconds", "max_input_chars", "daily_call_limit",
            ):
                setattr(test_config, key, getattr(provider, key))
            tester = create_model_provider(test_config, config.system.timezone)
            model_input = ModelInputType(
                text=str(body.get("text") or "连接测试"),
                source_name="连接测试",
                source_type="test",
                conversation_name=None,
                published_at=iso_utc(),
                source_url=None,
            )
            result = tester.analyze(model_input, {})
            return 200, {
                "provider": tester.name,
                "model": tester.model_name,
                "result": {k: v for k, v in result.items() if not k.startswith("_")},
            }

        # Notifications
        if path == "/api/notification-channels" and method == "GET":
            channels = []
            latest = repo.latest_notification_by_channel()
            for channel in config.notifications.channels:
                record = latest.get(channel.id) or {}
                channels.append(_public_channel(channel, record))
            return 200, {
                "channels": channels,
                "min_priority": config.notifications.min_priority,
                "hourly_limit": config.notifications.hourly_limit,
                "quiet_hours": {
                    "start": config.scheduling.quiet_hours.start,
                    "end": config.scheduling.quiet_hours.end,
                    "allow_urgent": config.scheduling.quiet_hours.allow_urgent,
                },
            }
        if path == "/api/notification-channels" and method == "POST":
            self._upsert_notification_channel(body)
            channels = []
            latest = repo.latest_notification_by_channel()
            for channel in config.notifications.channels:
                channels.append(_public_channel(channel, latest.get(channel.id) or {}))
            return 200, {"channels": channels}
        match = re.fullmatch(r"/api/notification-channels/([^/]+)", path)
        if match and method in ("PATCH", "DELETE"):
            channel_id = match.group(1)
            channel = next(
                (c for c in config.notifications.channels if c.id == channel_id), None
            )
            if channel is None:
                raise AppError("NOT_FOUND", "通知渠道不存在")
            if method == "DELETE":
                config.notifications.channels = [
                    c for c in config.notifications.channels if c.id != channel_id
                ]
            else:
                body["id"] = channel_id
                self._upsert_notification_channel(body)
            self._persist_notification_settings()
            self.runtime.apply_notification_config()
            repo.audit(
                "notification.channel_update",
                "notification_channel",
                channel_id,
                {"action": method.lower()},
            )
            return 200, {"status": "saved", "channel": channel_id}
        if path == "/api/notifications/settings" and method == "POST":
            self._save_notification_settings(body)
            return 200, {
                "status": "saved",
                "min_priority": config.notifications.min_priority,
                "hourly_limit": config.notifications.hourly_limit,
                "quiet_hours": {
                    "start": config.scheduling.quiet_hours.start,
                    "end": config.scheduling.quiet_hours.end,
                    "allow_urgent": config.scheduling.quiet_hours.allow_urgent,
                },
            }
        match = re.fullmatch(r"/api/notification-channels/([^/]+)/test", path)
        if match and method == "POST":
            return 200, self.runtime.notification_service.test_channel(match.group(1))
        if path == "/api/notifications" and method == "GET":
            return 200, {"notifications": repo.list_notifications(
                int(query.get("limit", ["50"])[0])
            )}

        # Digests
        if path == "/api/digests" and method == "GET":
            return 200, {"digests": repo.list_digests(60)}
        match = re.fullmatch(r"/api/digests/([0-9]{4}-[0-9]{2}-[0-9]{2})(?:/(regenerate))?", path)
        if match:
            date_text, action = match.group(1), match.group(2)
            if action == "regenerate" and method == "POST":
                summary = self.runtime.digest_service.generate(date_text)
                repo.audit("digest.regenerate", "digest", date_text)
                return 200, summary
            record = repo.get_digest(date_text)
            content = None
            if record:
                path_obj = Path(record["file_path"])
                if path_obj.exists():
                    content = path_obj.read_text(encoding="utf-8")
            if record is None and content is None:
                raise AppError("NOT_FOUND", "日报不存在")
            return 200, {"digest": record, "content": content}

        # Rules
        if path == "/api/rules" and method == "GET":
            rules = repo.list_rules()
            for rule in rules:
                rule["condition"] = loads(rule.pop("condition_json"), {})
                rule["action"] = loads(rule.pop("action_json"), {})
            return 200, {"rules": rules}
        if path == "/api/rules" and method == "POST":
            if not body.get("name"):
                raise AppError("VALIDATION_ERROR", "规则名称必填")
            rule_id = repo.create_rule(body)
            repo.audit("rule.create", "rule", rule_id, {"name": body.get("name")})
            return 201, {"id": rule_id}
        match = re.fullmatch(r"/api/rules/([^/]+)", path)
        if match:
            rule_id = match.group(1)
            if method == "PATCH":
                repo.update_rule(rule_id, body)
                return 200, {"id": rule_id}
            if method == "DELETE":
                repo.delete_rule(rule_id)
                return 200, {"deleted": rule_id}

        # Errors
        if path == "/api/errors" and method == "GET":
            failed_jobs = repo.list_jobs(50, "failed")
            failed_notifications = [
                n for n in repo.list_notifications(100) if n.get("status") == "failed"
            ]
            sources = [
                s for s in repo.list_sources() if s.get("last_error_code")
            ]
            return 200, {
                "sources": sources,
                "jobs": [_public_job(j) for j in failed_jobs],
                "notifications": failed_notifications,
            }

        raise AppError("NOT_FOUND", f"接口不存在: {method} {path}")

    # Settings -----------------------------------------------------------
    def _upsert_notification_channel(self, body: dict[str, Any]) -> None:
        config = self.runtime.config
        channel_id = str(body.get("id") or "").strip()
        if not channel_id:
            raise AppError("VALIDATION_ERROR", "渠道 id 必填")
        existing = next(
            (c for c in config.notifications.channels if c.id == channel_id), None
        )
        channel_type = str(body.get("type") or (existing.type if existing else "")).strip()
        allowed = {"mock", "file", "webhook", "pushplus", "serverchan", "wecom_bot", "bark"}
        if channel_type not in allowed:
            raise AppError("VALIDATION_ERROR", f"未知通知渠道类型: {channel_type}")
        incoming = body.get("config") or {}
        if not isinstance(incoming, dict):
            raise AppError("VALIDATION_ERROR", "渠道 config 必须是对象")
        merged: dict[str, Any] = dict(existing.config) if existing else {}
        for key, value in incoming.items():
            if value == "***" and key in merged:
                continue
            merged[key] = value
        channel = NotificationChannelConfig(
            id=channel_id,
            type=channel_type,
            enabled=bool(body.get("enabled", existing.enabled if existing else True)),
            config=merged,
        )
        try:
            build_channel(channel, config.data_dir)
        except AppError:
            raise
        if existing is None:
            config.notifications.channels.append(channel)
        else:
            config.notifications.channels = [
                channel if c.id == channel_id else c
                for c in config.notifications.channels
            ]
        self._persist_notification_settings()
        self.runtime.apply_notification_config()
        self.runtime.repo.audit(
            "notification.channel_save",
            "notification_channel",
            channel_id,
            {"type": channel_type, "enabled": channel.enabled},
        )

    def _save_notification_settings(self, body: dict[str, Any]) -> None:
        config = self.runtime.config
        min_priority = str(
            body.get("min_priority", config.notifications.min_priority)
        )
        if min_priority not in PRIORITY_RANK:
            raise AppError("VALIDATION_ERROR", "min_priority 取值无效")
        try:
            hourly_limit = int(
                body.get("hourly_limit", config.notifications.hourly_limit)
            )
        except (TypeError, ValueError):
            raise AppError("VALIDATION_ERROR", "每小时上限必须是整数") from None
        if not 1 <= hourly_limit <= 1000:
            raise AppError("VALIDATION_ERROR", "每小时上限必须在 1 到 1000 之间")
        quiet = body.get("quiet_hours") or {}
        start = str(quiet.get("start", config.scheduling.quiet_hours.start))
        end = str(quiet.get("end", config.scheduling.quiet_hours.end))
        for label, value in (("开始", start), ("结束", end)):
            try:
                parts = value.split(":")
                if len(parts) != 2:
                    raise ValueError
                hour, minute = int(parts[0]), int(parts[1])
                if not (0 <= hour <= 23 and 0 <= minute <= 59):
                    raise ValueError
            except (ValueError, IndexError):
                raise AppError(
                    "VALIDATION_ERROR", f"静默时间{label}必须使用 HH:MM 格式"
                ) from None
        config.notifications.min_priority = min_priority
        config.notifications.hourly_limit = hourly_limit
        config.scheduling.quiet_hours.start = start
        config.scheduling.quiet_hours.end = end
        config.scheduling.quiet_hours.allow_urgent = bool(
            quiet.get(
                "allow_urgent", config.scheduling.quiet_hours.allow_urgent
            )
        )
        self._persist_notification_settings()
        self.runtime.apply_notification_config()
        self.runtime.repo.audit(
            "notification.settings",
            "notification_settings",
            None,
            {"min_priority": min_priority, "hourly_limit": hourly_limit},
        )

    def _persist_notification_settings(self) -> None:
        config = self.runtime.config
        if config.config_path is None:
            return
        merge_config_file(
            config.config_path,
            {
                "notifications": {
                    "min_priority": config.notifications.min_priority,
                    "hourly_limit": config.notifications.hourly_limit,
                    "channels": [
                        {
                            "id": channel.id,
                            "type": channel.type,
                            "enabled": channel.enabled,
                            "config": channel.config,
                        }
                        for channel in config.notifications.channels
                    ],
                },
                "scheduling": {
                    "quiet_hours": {
                        "start": config.scheduling.quiet_hours.start,
                        "end": config.scheduling.quiet_hours.end,
                        "allow_urgent": config.scheduling.quiet_hours.allow_urgent,
                    }
                },
            },
        )

    # Validation ---------------------------------------------------------
    def _validate_model_provider(
        self,
        body: dict[str, Any],
        *,
        existing: ModelProviderConfig | None = None,
        require_model: bool = True,
    ) -> ModelProviderConfig:
        """Validate one real OpenAI-compatible provider configuration."""
        base = model_provider_dict(existing) if existing is not None else {}
        incoming = dict(body)
        if "display_name" not in incoming and "name" in incoming:
            incoming["display_name"] = incoming["name"]
        merged = {**base, **incoming}
        display_name = str(merged.get("display_name", "") or "").strip()
        provider_name = str(merged.get("provider", "openai_compatible") or "").strip()
        if provider_name not in ("openai_compatible", "openai"):
            raise AppError("VALIDATION_ERROR", "仅支持 OpenAI 兼容接口")
        protocol = str(merged.get("protocol", "chat_completions") or "").strip()
        if protocol != "chat_completions":
            raise AppError("VALIDATION_ERROR", "当前仅支持 Chat Completions 上游格式")
        base_url = str(merged.get("base_url", "") or "").strip()
        if not base_url.startswith(("http://", "https://")):
            raise AppError("VALIDATION_ERROR", "API 请求地址必须以 http:// 或 https:// 开头")
        model_name = str(merged.get("model", "") or "").strip()
        model_list_value = merged.get("model_list", "")
        if isinstance(model_list_value, list):
            model_list_value = "\n".join(
                str(item).strip() for item in model_list_value if str(item).strip()
            )
        if not isinstance(model_list_value, str):
            raise AppError("VALIDATION_ERROR", "模型列表必须是文本或数组")
        model_list_value = "\n".join(
            line.strip() for line in model_list_value.splitlines() if line.strip()
        )
        if not model_name and model_list_value:
            model_name = model_list_value.splitlines()[0]
        if not model_name:
            # Listing a provider's models happens before any model is chosen,
            # so that path validates the endpoint and key only.
            if require_model:
                raise AppError("VALIDATION_ERROR", "模型名称不能为空")
            model_name = ""
        if not display_name:
            # Keep the API usable for clients that only provide endpoint and
            # model; this is a label derived from the user's own config, not a
            # pre-seeded/fake provider.
            display_name = model_name
        official_url = str(merged.get("official_url", "") or "").strip()
        if official_url and not official_url.startswith(("http://", "https://")):
            raise AppError("VALIDATION_ERROR", "官网链接必须以 http:// 或 https:// 开头")
        api_key_env = str(
            merged.get("api_key_env", "MSGDESK_MODEL_API_KEY") or ""
        ).strip()
        if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", api_key_env):
            raise AppError("VALIDATION_ERROR", "密钥环境变量名无效")
        raw_key = merged.get("api_key")
        if raw_key == "***" and existing is not None:
            api_key = existing.api_key
        else:
            api_key = str(raw_key or "").strip()
        try:
            timeout_seconds = int(merged.get("timeout_seconds", 60))
            max_input_chars = int(merged.get("max_input_chars", 6000))
            daily_call_limit = int(merged.get("daily_call_limit", 500))
        except (TypeError, ValueError):
            raise AppError("VALIDATION_ERROR", "连接参数必须是整数") from None
        if not 1 <= timeout_seconds <= 600:
            raise AppError("VALIDATION_ERROR", "超时时间必须在 1 到 600 秒之间")
        if not 500 <= max_input_chars <= 100000:
            raise AppError("VALIDATION_ERROR", "最大输入长度必须在 500 到 100000 之间")
        if not 0 <= daily_call_limit <= 100000:
            raise AppError("VALIDATION_ERROR", "每日调用上限必须是非负整数")
        return ModelProviderConfig(
            id=str(merged.get("id", "") or "").strip(),
            display_name=display_name,
            note=str(merged.get("note", "") or "").strip(),
            official_url=official_url,
            provider=provider_name,
            protocol=protocol,
            base_url=base_url,
            model=model_name,
            model_list=model_list_value,
            api_key_env=api_key_env,
            api_key=api_key,
            timeout_seconds=timeout_seconds,
            max_input_chars=max_input_chars,
            daily_call_limit=daily_call_limit,
        )

    def _persist_model_settings(self) -> bool:
        config = self.runtime.config
        if config.config_path is None:
            return False
        active = model_provider_dict(config.model)
        active.pop("id", None)
        merge_config_file(
            config.config_path,
            {
                "model": {
                    "providers": [
                        model_provider_dict(item) for item in config.model.providers
                    ],
                    "default_provider_id": config.model.default_provider_id,
                    # Keep the old top-level fields for older callers and
                    # config readers. The provider collection remains the
                    # source of truth for the workbench.
                    **active,
                }
            },
        )
        return True

    def _validate_source(self, body: dict[str, Any]) -> dict[str, Any]:
        allowed_types = {
            "qq", "qq_onebot", "wechat", "wecom", "rss", "web", "frontier",
            "mock_chat", "chat_import",
        }
        source_type = str(body.get("type", "")).strip()
        if source_type not in allowed_types:
            raise AppError("VALIDATION_ERROR", f"来源类型必须是 {sorted(allowed_types)}")
        if not body.get("name"):
            raise AppError("VALIDATION_ERROR", "来源名称必填")
        adapter_type = body.get("adapter_type") or (
            "rss" if source_type == "frontier" else source_type
        )
        config = body.get("config") or {}
        self._validate_adapter_config(adapter_type, config)
        return {
            "type": source_type,
            "name": str(body["name"]).strip(),
            "adapter_type": adapter_type,
            "account_id": body.get("account_id"),
            "enabled": bool(body.get("enabled", True)),
            "config": config,
            "default_category": body.get("default_category"),
            "default_priority": body.get("default_priority", "normal"),
            "interval_seconds": int(body.get("interval_seconds", 600)),
            "backfill_since": body.get("backfill_since"),
        }

    @staticmethod
    def _validate_adapter_config(adapter_type: str, config: dict[str, Any]) -> None:
        adapter = create_adapter(adapter_type)
        try:
            validation = adapter.validate_config(config)
        finally:
            adapter.close()
        if not validation.ok:
            raise AppError("CONFIG_INVALID", validation.message, validation.details)


def _public_job(job: dict[str, Any]) -> dict[str, Any]:
    result = dict(job)
    result["payload"] = loads(result.pop("payload_json", "{}"), {})
    return result


def _public_channel(
    channel: Any, record: dict[str, Any] | None = None
) -> dict[str, Any]:
    record = record or {}
    return {
        "id": channel.id,
        "type": channel.type,
        "enabled": channel.enabled,
        "config": redact_secrets(channel.config),
        "last_status": record.get("status"),
        "last_error": record.get("error_code"),
        "last_sent_at": record.get("sent_at"),
        "last_attempt_at": record.get("updated_at"),
    }


def _redact_source(source: dict[str, Any]) -> dict[str, Any]:
    config = loads(source.get("config_json"), {}) or {}
    if not isinstance(config, dict):
        config = {}
    safe = redact_secrets(config, hide_url=False)
    # The editor needs the stored cookie back so it can show and edit it;
    # logs and audit records still go through redact_secrets separately.
    if config.get("cookie"):
        safe["cookie"] = config["cookie"]
    source["config"] = safe
    source["config_json"] = json.dumps(safe, ensure_ascii=False)
    if source.get("adapter_type") == "chat_import" and config.get("directory"):
        source["import_directory"] = config["directory"]
    return source


class ApiServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True

    def __init__(self, address: tuple[str, int], runtime: Runtime):
        self.runtime = runtime
        handler = type("BoundApiHandler", (ApiHandler,), {"runtime": runtime})
        super().__init__(address, handler)


def create_server(runtime: Runtime) -> ApiServer:
    host = runtime.config.system.bind_host
    port = int(runtime.config.system.port)
    return ApiServer((host, port), runtime)

