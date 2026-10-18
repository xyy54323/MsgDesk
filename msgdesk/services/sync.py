"""Incremental source synchronization with platform and content dedupe."""

from __future__ import annotations

import json
import uuid
from datetime import timedelta
from pathlib import Path
from typing import Any, Callable

from ..adapters import create_adapter
from ..adapters.base import FetchOptions, SourceContext
from ..config import AppConfig
from ..errors import AppError
from ..models import Cursor
from ..repository import Repository, dumps, loads, new_id
from ..timeutil import iso_utc, utc_now
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
            backfill_since=source_row.get("backfill_since"),
        )
        trace_id = str(uuid.uuid4())
        if source_row["adapter_type"] == "ai_web":
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

                collector = AgentCollector(
                    self.config, trace=self.repo.add_trace, on_cookies=save_cookies
                )
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
        else:
            adapter = self.adapter_factory(source_row["adapter_type"])
            validation = adapter.validate_config(config)
            if not validation.ok:
                self.repo.mark_source_error(source_id, "CONFIG_INVALID")
                raise AppError("CONFIG_INVALID", validation.message)
            cursor_row = self.repo.get_cursor_row(source_id)
            cursor = Cursor.from_row(cursor_row)
            try:
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
                 "merged": 0, "failed": 0}
        raw_dir = self._raw_dir()
        now = iso_utc()
        with self.repo.db.connection() as conn:
            for raw in result.messages:
                try:
                    text = clean_text(raw.content_text)
                    if not text:
                        stats["failed"] += 1
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
                        and source_row["type"] in ("rss", "web", "frontier")
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
        self.repo.mark_source_success(source_id)
        return {
            "trace_id": trace_id,
            "source_id": source_id,
            "source_name": source_row["name"],
            **stats,
            "warnings": result.warnings,
            "partial": result.partial,
        }

    def _raw_dir(self) -> Path:
        now = utc_now().astimezone()
        path = self.config.data_dir / "raw" / now.strftime("%Y") / now.strftime("%m") / now.strftime("%d")
        path.mkdir(parents=True, exist_ok=True)
        return path

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

