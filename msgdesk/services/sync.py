"""Incremental source synchronization with platform and content dedupe."""

from __future__ import annotations

import json
import re
import urllib.error
import urllib.request
import uuid
from datetime import datetime, time, timedelta
from pathlib import Path
from typing import Any, Callable

from ..adapters import create_adapter
from ..adapters.base import FetchOptions, SourceContext
from ..config import AppConfig
from ..errors import AppError
from ..models import Cursor
from ..repository import Repository, dumps, loads, new_id
from ..timeutil import iso_utc, local_date_of, local_zone, utc_now
from .normalize import clean_text, content_hash, infer_content_type

PLATFORM_DEDUPE_WINDOW = timedelta(days=30)
CONTENT_DEDUPE_WINDOW = timedelta(days=7)


class SyncService:
    def __init__(self, config: AppConfig, repo: Repository,
                 adapter_factory: Callable[[str], Any] = create_adapter):
        self.config = config
        self.repo = repo
        self.adapter_factory = adapter_factory

    def sync_source(self, source_id: str, *, max_items: int = 50) -> dict[str, Any]:
        source_row = self.repo.get_source(source_id)
        if source_row is None:
            raise AppError("NOT_FOUND", f"来源不存在: {source_id}")
        config = loads(source_row.get("config_json"), {}) or {}
        source = SourceContext(
            id=source_row["id"],
            type=source_row["type"],
            name=source_row["name"],
            config=config,
            backfill_since=self.config.date_range.start.strip() or source_row.get("backfill_since"),
        )
        trace_id = str(uuid.uuid4())
        history_range = self._history_range()
        range_start, range_end = self.config.effective_range()
        if source_row["adapter_type"] == "ai_web":
            from .site_crawler import StaticListCollector, StaticListUnavailable
            try:
                result = StaticListCollector(self.config).collect(source)
            except StaticListUnavailable:
                from .agent import AgentCollector

                try:
                    def save_cookies(header: str, expiry: str) -> None:
                        row = self.repo.get_source(source_id) or {}
                        cfg = loads(row.get("config_json"), {}) or {}
                        cfg["cookie"] = header
                        cfg["cookie_expires"] = expiry
                        cfg["cookie_refreshed_at"] = iso_utc()
                        cfg["cookie_invalid"] = False
                        self.repo.update_source(source_id, {"config": cfg})

                    collector = AgentCollector(self.config, on_cookies=save_cookies)
                    try:
                        result = collector.collect(source)
                    finally:
                        collector.close()
                except AppError as exc:
                    self.repo.mark_source_error(source_id, exc.code)
                    raise
                except Exception as exc:  # noqa: BLE001 - surface a stable code
                    self.repo.mark_source_error(source_id, "REMOTE_TEMPORARY_ERROR")
                    raise AppError(
                        "REMOTE_TEMPORARY_ERROR", f"智能体采集异常: {type(exc).__name__}"
                    ) from exc
            except AppError as exc:
                self.repo.mark_source_error(source_id, exc.code)
                raise
            except Exception as exc:  # noqa: BLE001 - surface a stable code
                self.repo.mark_source_error(source_id, "REMOTE_TEMPORARY_ERROR")
                raise AppError(
                    "REMOTE_TEMPORARY_ERROR", f"智能体采集异常: {type(exc).__name__}"
                ) from exc
        else:
            adapter = self.adapter_factory(source_row["adapter_type"])
            validation = adapter.validate_config(config)
            if not validation.ok:
                self.repo.mark_source_error(source_id, "CONFIG_INVALID")
                raise AppError("CONFIG_INVALID", validation.message)
            cursor_row = self.repo.get_cursor_row(source_id)
            cursor = Cursor.from_row(cursor_row)
            try:
                if history_range is not None and hasattr(adapter, "fetch_range"):
                    result = adapter.fetch_range(source, history_range[0], history_range[1])
                else:
                    result = adapter.fetch_incremental(
                        source,
                        cursor,
                        FetchOptions(max_items=max_items,
                                     timeout_seconds=float(config.get("timeout_seconds", 20))),
                    )
            except AppError as exc:
                self.repo.mark_source_error(source_id, exc.code)
                raise
            except Exception as exc:
                self.repo.mark_source_error(source_id, "REMOTE_TEMPORARY_ERROR")
                raise AppError(
                    "REMOTE_TEMPORARY_ERROR", f"采集异常: {type(exc).__name__}"
                ) from exc
            finally:
                adapter.close()

        stats = {"fetched": len(result.messages), "inserted": 0, "duplicated": 0,
                 "merged": 0, "failed": 0, "filtered": 0}
        raw_dir = self._raw_dir()
        now = iso_utc()
        qq_slices: set[tuple[str, str]] = set()
        warnings: list[str] = []
        is_qq = source_row["adapter_type"] == "qq_onebot"
        with self.repo.db.connection() as conn:
            for raw in result.messages:
                try:
                    text = clean_text(raw.content_text)
                    if not text:
                        stats["failed"] += 1
                        continue
                    published_day = local_date_of(
                        raw.published_at, self.config.system.timezone
                    )
                    if range_start and (
                        published_day < range_start or published_day > range_end
                    ):
                        stats["filtered"] += 1
                        continue
                    conversation_id = self.repo.upsert_conversation(
                        conn, source_id, raw.conversation_id,
                        raw.conversation_name or source.name,
                        raw.conversation_kind or "web",
                    )
                    if raw.platform_message_id:
                        existing = self.repo.find_message_by_platform(
                            conn, source_id, conversation_id, raw.platform_message_id
                        )
                        if existing is not None:
                            stats["duplicated"] += 1
                            continue
                    digest = content_hash(text)
                    same_source = self.repo.find_recent_by_hash(
                        conn, digest,
                        iso_utc(utc_now() - CONTENT_DEDUPE_WINDOW),
                        cross_source=False,
                        source_id=source_id,
                    )
                    if same_source is not None:
                        self.repo.add_message_source(
                            conn, same_source["id"], source_id,
                            raw.platform_message_id, raw.source_url,
                        )
                        stats["duplicated"] += 1
                        continue
                    if (
                        raw.source_url
                        and len(text) >= 40
                        and source_row["type"] in ("web", "ai_web")
                    ):
                        cross = self.repo.find_recent_by_hash(
                            conn, digest,
                            iso_utc(utc_now() - CONTENT_DEDUPE_WINDOW),
                            cross_source=True,
                            source_id=source_id,
                        )
                        if cross is not None and cross["source_id"] != source_id:
                            self.repo.add_message_source(
                                conn, cross["id"], source_id,
                                raw.platform_message_id, raw.source_url,
                            )
                            stats["merged"] += 1
                            continue
                    attachments = self._download_attachments(
                        raw, source_id, warnings
                    )
                    if attachments:
                        text += "\n\n## 附件\n\n" + "\n".join(
                            f"![图片]({url})" if kind == "image"
                            else f"- [{name}]({url})"
                            for name, url, kind in attachments
                        )
                        digest = content_hash(text)
                    raw_ref = self._write_raw(raw_dir, source_id, raw.raw_payload or {
                        "content_text": text,
                        "source_url": raw.source_url,
                    }, now)
                    message_id = self.repo.insert_message(
                        conn,
                        {
                            "id": new_id(),
                            "source_id": source_id,
                            "account_id": source_row.get("account_id"),
                            "conversation_id": conversation_id,
                            "sender_id": raw.sender_id,
                            "sender_name": raw.sender_name,
                            "platform_message_id": raw.platform_message_id,
                            "published_at": raw.published_at,
                            "collected_at": now,
                            "content_type": infer_content_type(raw.__dict__),
                            "content_text": text,
                            "content_completeness": raw.content_completeness,
                            "source_url": raw.source_url,
                            "raw_payload_ref": str(raw_ref),
                            "content_hash": digest,
                        },
                    )
                    if is_qq:
                        # Group chats are summarised per conversation, once
                        # per sync cycle, instead of one call per message.
                        qq_slices.add((
                            conversation_id,
                            local_date_of(raw.published_at, self.config.system.timezone),
                        ))
                    else:
                        self.repo.enqueue_job(
                            "process_message",
                            message_id=message_id,
                            source_id=source_id,
                            payload={},
                            priority=80,
                            trace_id=trace_id,
                            conn=conn,
                        )
                    stats["inserted"] += 1
                except Exception:
                    stats["failed"] += 1
            for conversation_id, date_text in sorted(qq_slices):
                if self.repo.open_job_exists(
                    "summarize_conversation",
                    source_id,
                    conversation_id=conversation_id,
                    date=date_text,
                ):
                    continue
                self.repo.enqueue_job(
                    "summarize_conversation",
                    source_id=source_id,
                    payload={"conversation_id": conversation_id, "date": date_text},
                    priority=90,
                    trace_id=trace_id,
                    conn=conn,
                )
            if result.next_cursor is not None:
                next_cursor = result.next_cursor
                self.repo.save_cursor(
                    conn,
                    source_id,
                    cursor_type=next_cursor.cursor_type,
                    cursor_value=next_cursor.cursor_value,
                    last_message_time=next_cursor.last_message_time,
                    last_platform_message_id=next_cursor.last_platform_message_id,
                    etag=next_cursor.etag,
                    last_modified=next_cursor.last_modified,
                )
        result.warnings.extend(warnings)
        if is_qq:
            self.backfill_qq_attachments(source_id)
        self.repo.mark_source_success(source_id)
        return {
            "trace_id": trace_id,
            "source_id": source_id,
            "source_name": source_row["name"],
            **stats,
            "warnings": result.warnings,
            "partial": result.partial,
        }

    def backfill_qq_attachments(self, source_id: str) -> int:
        """Add attachment links to old QQ messages that predate this feature."""
        from ..adapters.qq_onebot import QQOneBotAdapter

        source_row = self.repo.get_source(source_id)
        if source_row is None:
            return 0
        source = SourceContext(
            id=source_row["id"],
            type=source_row["type"],
            name=source_row["name"],
            config=loads(source_row.get("config_json"), {}) or {},
        )
        adapter = QQOneBotAdapter()
        changed = 0
        slices: set[tuple[str, str]] = set()
        try:
            for message in self.repo.list_messages(limit=1000, source_id=source_id):
                content = str(message.get("content_text") or "")
                if "## 附件" in content or "/attachments/" in content:
                    continue
                raw_ref = str(message.get("raw_payload_ref") or "").strip()
                if not raw_ref:
                    continue
                try:
                    record = json.loads(Path(raw_ref).read_text(encoding="utf-8"))
                except (OSError, json.JSONDecodeError):
                    continue
                payload = record.get("payload")
                if not isinstance(payload, dict):
                    continue
                converted = adapter._convert(
                    payload,
                    str(message.get("conversation_id") or ""),
                    source,
                )
                if converted is None or not converted.attachments:
                    continue
                saved = self._download_attachments(converted, source_id, [])
                if not saved:
                    continue
                updated = converted.content_text + "\n\n## 附件\n\n" + "\n".join(
                    f"![图片]({url})" if kind == "image"
                    else f"- [{name}]({url})"
                    for name, url, kind in saved
                )
                self.repo.update_message_fields(
                    message["id"],
                    {
                        "content_text": updated,
                        "content_hash": content_hash(updated),
                    },
                )
                slices.add((
                    str(message.get("conversation_id") or ""),
                    local_date_of(converted.published_at, self.config.system.timezone),
                ))
                changed += 1
        finally:
            adapter.close()
        for conversation_id, date_text in sorted(slices):
            if not conversation_id:
                continue
            if self.repo.open_job_exists(
                "summarize_conversation",
                source_id,
                conversation_id=conversation_id,
                date=date_text,
            ):
                continue
            self.repo.enqueue_job(
                "summarize_conversation",
                source_id=source_id,
                payload={"conversation_id": conversation_id, "date": date_text},
                priority=90,
            )
        return changed

    def _history_range(self) -> tuple[datetime, datetime] | None:
        """Return the global collection window as timezone-aware datetimes."""
        start_text, end_text = self.config.effective_range()
        if not start_text:
            return None
        zone = local_zone(self.config.system.timezone)
        try:
            start = datetime.combine(datetime.fromisoformat(start_text).date(), time.min, zone)
            end = datetime.combine(datetime.fromisoformat(end_text).date(), time.max, zone)
        except ValueError:
            return None
        if end < start:
            start, end = end, start
        return start, end

    def _raw_dir(self) -> Path:
        now = utc_now().astimezone()
        path = self.config.data_dir / "raw" / now.strftime("%Y") / now.strftime("%m") / now.strftime("%d")
        path.mkdir(parents=True, exist_ok=True)
        return path

    def _download_attachments(
        self,
        raw: Any,
        source_id: str,
        warnings: list[str],
    ) -> list[tuple[str, str, str]]:
        """Download every attachment exposed by a source, regardless of type."""
        saved: list[tuple[str, str, str]] = []
        date_text = local_date_of(raw.published_at, self.config.system.timezone)
        root = self.config.data_dir / "attachments" / date_text
        for entry in getattr(raw, "attachments", []) or []:
            if not isinstance(entry, dict):
                continue
            url = str(entry.get("url") or "").strip()
            name = str(entry.get("name") or "").strip()
            kind = str(entry.get("type") or "").strip()
            if not url.startswith(("http://", "https://")):
                continue
            if not name:
                name = Path(url.split("?", 1)[0]).name or "attachment"
            request = urllib.request.Request(
                url, headers={"User-Agent": "MsgDesk/0.1"}
            )
            try:
                with urllib.request.urlopen(request, timeout=30) as response:
                    body = response.read(50 * 1024 * 1024 + 1)
            except (OSError, urllib.error.URLError, urllib.error.HTTPError) as exc:
                warnings.append(f"附件下载失败 {name}: {type(exc).__name__}")
                continue
            if len(body) > 50 * 1024 * 1024:
                warnings.append(f"附件过大，已跳过 {name}")
                continue
            safe_name = re.sub(r"[^\w.\-]+", "_", Path(name).name) or "attachment.bin"
            if not Path(safe_name).suffix:
                suffix = Path(url.split("?", 1)[0]).suffix
                safe_name += suffix if suffix else ".bin"
            root.mkdir(parents=True, exist_ok=True)
            stored_name = f"{source_id[:8]}-{uuid.uuid4().hex[:8]}-{safe_name}"
            (root / stored_name).write_bytes(body)
            saved.append((name, f"/attachments/{date_text}/{stored_name}", kind))
        return saved

    @staticmethod
    def _write_raw(directory: Path, source_id: str, payload: dict[str, Any], now: str) -> Path:
        path = directory / f"{source_id[:8]}-{uuid.uuid4().hex[:12]}.json"
        temp = path.with_suffix(".tmp")
        temp.write_text(
            json.dumps(
                {"source_id": source_id, "collected_at": now, "payload": payload},
                ensure_ascii=False,
                indent=2,
            ),
            encoding="utf-8",
        )
        temp.replace(path)
        return path

