"""JSON routes for the minimum MsgDesk MVP."""

from __future__ import annotations

import base64
import json
import os
import re
import shutil
import threading
import urllib.error
import urllib.request
import uuid
from copy import copy
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Callable

from ..adapters import create_adapter, list_adapter_capabilities
from ..adapters.base import SourceContext
from ..config import (
    ModelProviderConfig,
    activate_model_provider,
    merge_config_file,
    model_provider_dict,
    redact_secrets,
)
from ..errors import AppError
from ..repository import loads
from ..runtime import Runtime
from ..services import autostart
from ..services.model import ModelInput as ModelInputType, create_model_provider
from ..timeutil import iso_utc, local_date_of, local_zone, parse_datetime, utc_now


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
    key = provider.api_key or os.environ.get(provider.api_key_env, "")
    result["api_key_set"] = bool(key)
    result["api_key_hint"] = _mask_key(key)
    return result


def _model_id(value: str) -> str:
    value = re.sub(r"[^a-zA-Z0-9_-]+", "-", value.strip().lower()).strip("-_")
    return value[:48] or "provider"


class ApiRouter:
    def __init__(self, runtime: Runtime, shutdown_callback: Callable[[], None]):
        self.runtime = runtime
        self._shutdown_callback = shutdown_callback

    # Routing ----------------------------------------------------------
    def route(
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

        if path == "/api/profile" and method == "GET":
            profile = config.profile
            return 200, {
                "profile": {
                    "identity": profile.identity,
                    "interests": profile.interests,
                    "ignore": profile.ignore,
                    "todo_rule": profile.todo_rule,
                    "notes": profile.notes,
                }
            }

        if path == "/api/profile" and method in ("POST", "PUT"):
            payload = body.get("profile") if isinstance(body.get("profile"), dict) else body
            keys = ("identity", "interests", "ignore", "todo_rule", "notes")
            cleaned = {key: str(payload.get(key) or "").strip() for key in keys}
            if not config.config_path:
                raise AppError("CONFIG_INVALID", "没有配置文件，无法保存个人画像")
            merge_config_file(config.config_path, {"profile": cleaned})
            for key, value in cleaned.items():
                setattr(config.profile, key, value)
            repo.audit("profile.update", "profile", "")
            return 200, {"profile": cleaned}

        if path == "/api/range" and method == "GET":
            start, effective_end = config.effective_range()
            return 200, {
                "range": {
                    "start": start,
                    "end": config.date_range.end,
                    "effective_end": effective_end,
                },
                "digest_time": config.scheduling.digest_time,
                "autostart": bool(config.system.autostart),
                "autostart_active": autostart.is_enabled(),
            }

        if path == "/api/range" and method in ("POST", "PUT"):
            start = str(body.get("start") or "").strip()
            end = str(body.get("end") or "").strip()
            digest_time = str(
                body.get("digest_time") or config.scheduling.digest_time
            ).strip()
            autostart_enabled = body.get("autostart")
            if not start:
                raise AppError("VALIDATION_ERROR", "起始日期必填")
            try:
                start_date = datetime.fromisoformat(start).date()
                end_date = datetime.fromisoformat(end).date() if end else None
            except ValueError:
                raise AppError("VALIDATION_ERROR", "日期必须使用 YYYY-MM-DD 格式") from None
            if end_date is not None and end_date < start_date:
                raise AppError("VALIDATION_ERROR", "结束日期不能早于起始日期")
            try:
                datetime.strptime(digest_time, "%H:%M")
            except ValueError:
                raise AppError("VALIDATION_ERROR", "日报时间必须使用 HH:MM 格式") from None
            if not config.config_path:
                raise AppError("CONFIG_INVALID", "没有配置文件，无法保存时间范围")
            updates: dict[str, Any] = {
                "range": {"start": start, "end": end},
                "scheduling": {"digest_time": digest_time},
            }
            if autostart_enabled is not None:
                updates["system"] = {"autostart": bool(autostart_enabled)}
            merge_config_file(
                config.config_path,
                updates,
            )
            config.date_range.start = start
            config.date_range.end = end
            config.scheduling.digest_time = digest_time
            autostart_active = autostart.is_enabled()
            if autostart_enabled is not None:
                config.system.autostart = bool(autostart_enabled)
                autostart_active = autostart.apply(
                    bool(autostart_enabled), config.config_path
                )
            repo.audit(
                "range.update",
                "range",
                None,
                {
                    "start": start,
                    "end": end,
                    "digest_time": digest_time,
                    "autostart": config.system.autostart,
                },
            )
            return 200, {
                "range": {
                    "start": start,
                    "end": end,
                    "effective_end": config.effective_range()[1],
                },
                "digest_time": digest_time,
                "autostart": bool(config.system.autostart),
                "autostart_active": autostart_active,
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
                "digests": repo.list_digests(5),
                "config": config.public_dict(),
                "today": utc_now().astimezone(local_zone(config.system.timezone)).date().isoformat(),
            }

        if method == "POST" and path == "/api/admin/shutdown":
            threading.Thread(target=self._shutdown_callback, daemon=True).start()
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

        if path == "/api/qq/status" and method == "GET":
            from ..services import qq_bridge

            return 200, qq_bridge.ensure_ready()

        if path == "/api/qq/state" and method == "GET":
            from ..services import qq_bridge

            return 200, qq_bridge.state()

        if path == "/api/qq/install" and method == "POST":
            from ..services import qq_bridge

            try:
                where = qq_bridge.install()
            except (RuntimeError, OSError, urllib.error.URLError) as exc:
                raise AppError("REMOTE_TEMPORARY_ERROR", f"下载 QQ 桥失败：{exc}") from exc
            return 200, {"installed": True, "path": where}

        if path == "/api/qq/relogin" and method == "POST":
            from ..services import qq_bridge

            for source in repo.list_sources():
                if source.get("adapter_type") != "qq_onebot":
                    continue
                source_config = loads(source.get("config_json"), {}) or {}
                if source_config.get("selected_conversations"):
                    source_config["selected_conversations"] = []
                    repo.update_source(source["id"], {"config": source_config})
            qq_bridge.relogin()
            return 200, {"status": "restarting"}

        if path == "/api/qq/qr/refresh" and method == "POST":
            from ..services import qq_bridge

            try:
                refreshed = qq_bridge.refresh_qr()
            except RuntimeError as exc:
                raise AppError("REMOTE_TEMPORARY_ERROR", str(exc)) from exc
            if not refreshed:
                raise AppError("REMOTE_TEMPORARY_ERROR", "二维码刷新超时，请稍后再试")
            return 200, qq_bridge.state()

        groups_match = re.fullmatch(r"/api/sources/([^/]+)/groups", path)
        if groups_match and method == "POST":
            source_id = groups_match.group(1)
            row = repo.get_source(source_id)
            if row is None:
                raise AppError("NOT_FOUND", "来源不存在")
            source_config = loads(row.get("config_json"), {}) or {}
            for key in ("base_url", "access_token"):
                if body.get(key):
                    source_config[key] = str(body[key]).strip()
            context = SourceContext(
                id=row["id"],
                type=row["type"],
                name=row["name"],
                config=source_config,
                backfill_since=row.get("backfill_since"),
            )
            adapter = create_adapter(row["adapter_type"])
            try:
                groups = adapter.list_groups(context)  # type: ignore[attr-defined]
            finally:
                adapter.close()
            return 200, {"groups": groups}

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
                    if source["adapter_type"] == "qq_onebot":
                        try:
                            groups = adapter.list_groups(context)  # type: ignore[attr-defined]
                        except AppError as exc:
                            return 200, {
                                "validation": {"ok": False, "message": exc.message},
                                "capabilities": adapter.get_capabilities().to_dict(),
                                "status": {"status": exc.code},
                            }
                        return 200, {
                            "validation": {
                                "ok": True,
                                "message": f"已连接本机 QQ 服务，共 {len(groups)} 个群",
                            },
                            "capabilities": adapter.get_capabilities().to_dict(),
                            "status": {"status": "ready"},
                        }
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
            # The frontend is loopback-only; the editor needs the stored key
            # to render it masked and to reveal on demand.
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
            # Test the unsaved form payload. A missing model is an error.
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

        # Errors
        if path == "/api/errors" and method == "GET":
            failed_jobs = repo.list_jobs(50, "failed")
            sources = [
                s for s in repo.list_sources() if s.get("last_error_code")
            ]
            return 200, {
                "sources": sources,
                "jobs": [_public_job(j) for j in failed_jobs],
            }

        raise AppError("NOT_FOUND", f"接口不存在: {method} {path}")

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
                    # source of truth for the frontend.
                    **active,
                }
            },
        )
        return True

    def _validate_source(self, body: dict[str, Any]) -> dict[str, Any]:
        allowed_types = {"web", "ai_web", "qq_onebot"}
        source_type = str(body.get("type", "")).strip()
        if source_type not in allowed_types:
            raise AppError("VALIDATION_ERROR", f"来源类型必须是 {sorted(allowed_types)}")
        if not body.get("name"):
            raise AppError("VALIDATION_ERROR", "来源名称必填")
        adapter_type = body.get("adapter_type") or source_type
        if adapter_type not in allowed_types:
            raise AppError("VALIDATION_ERROR", f"未知适配器类型: {adapter_type}")
        config = body.get("config") or {}
        if adapter_type == "qq_onebot" and isinstance(config, dict):
            # Local NapCat is the default target; users should not type this.
            config.setdefault("base_url", "http://127.0.0.1:3000")
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
    return source
