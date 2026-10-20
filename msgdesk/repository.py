"""Persistence helpers built directly on sqlite3."""

from __future__ import annotations

import json
import sqlite3
import uuid
from datetime import timedelta
from typing import Any, Iterable

from .db import Database
from .timeutil import iso_utc, utc_now


def new_id() -> str:
    return str(uuid.uuid4())


def dumps(value: Any) -> str:
    return json.dumps(value if value is not None else {}, ensure_ascii=False)


def loads(value: str | None, default: Any = None) -> Any:
    if not value:
        return default
    try:
        return json.loads(value)
    except json.JSONDecodeError:
        return default


def row_to_dict(row: sqlite3.Row | None) -> dict[str, Any] | None:
    return dict(row) if row is not None else None


class Repository:
    def __init__(self, db: Database):
        self.db = db

    # Sources -------------------------------------------------------------
    def list_sources(self, include_deleted: bool = False) -> list[dict[str, Any]]:
        sql = "SELECT * FROM sources"
        if not include_deleted:
            sql += " WHERE deleted_at IS NULL"
        sql += " ORDER BY created_at"
        with self.db.connection() as conn:
            return [dict(r) for r in conn.execute(sql)]

    def enabled_sources(self) -> list[dict[str, Any]]:
        with self.db.connection() as conn:
            rows = conn.execute(
                "SELECT * FROM sources WHERE enabled = 1 AND deleted_at IS NULL "
                "ORDER BY created_at"
            )
            return [dict(r) for r in rows]

    def get_source(self, source_id: str) -> dict[str, Any] | None:
        with self.db.connection() as conn:
            row = conn.execute(
                "SELECT * FROM sources WHERE id = ? AND deleted_at IS NULL", (source_id,)
            ).fetchone()
            return row_to_dict(row)

    def create_source(self, data: dict[str, Any]) -> dict[str, Any]:
        now = iso_utc()
        source_id = data.get("id") or new_id()
        with self.db.connection() as conn:
            conn.execute(
                "INSERT INTO sources(id, type, name, adapter_type, account_id, enabled, "
                "config_json, secret_ref, default_category, default_priority, "
                "interval_seconds, backfill_since, created_at, updated_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    source_id,
                    data["type"],
                    data["name"],
                    data.get("adapter_type") or data["type"],
                    data.get("account_id"),
                    1 if data.get("enabled", True) else 0,
                    dumps(data.get("config", {})),
                    data.get("secret_ref"),
                    data.get("default_category"),
                    data.get("default_priority", "normal"),
                    int(data.get("interval_seconds", 600)),
                    data.get("backfill_since"),
                    now,
                    now,
                ),
            )
        created = self.get_source(source_id)
        assert created is not None
        return created

    def update_source(self, source_id: str, data: dict[str, Any]) -> dict[str, Any] | None:
        allowed = {
            "type",
            "adapter_type",
            "name",
            "enabled",
            "default_category",
            "default_priority",
            "interval_seconds",
            "backfill_since",
            "account_id",
        }
        sets: list[str] = []
        values: list[Any] = []
        for key in allowed:
            if key in data:
                value = data[key]
                if key == "enabled":
                    value = 1 if value else 0
                sets.append(f"{key} = ?")
                values.append(value)
        if "config" in data:
            sets.append("config_json = ?")
            values.append(dumps(data["config"]))
        if not sets:
            return self.get_source(source_id)
        sets.append("updated_at = ?")
        values.extend([iso_utc(), source_id])
        with self.db.connection() as conn:
            conn.execute(
                f"UPDATE sources SET {', '.join(sets)} WHERE id = ? AND deleted_at IS NULL",
                values,
            )
        return self.get_source(source_id)

    def reset_cursor(self, source_id: str) -> None:
        """Forget the incremental cursor so the next sync re-reads everything."""
        with self.db.connection() as conn:
            conn.execute("DELETE FROM sync_cursors WHERE source_id = ?", (source_id,))

    def soft_delete_source(self, source_id: str) -> None:
        now = iso_utc()
        with self.db.connection() as conn:
            conn.execute(
                "UPDATE sources SET deleted_at = ?, enabled = 0, updated_at = ? WHERE id = ?",
                (now, now, source_id),
            )

    def purge_source(self, source_id: str) -> None:
        with self.db.connection() as conn:
            conn.execute("DELETE FROM sources WHERE id = ?", (source_id,))

    def mark_source_success(self, source_id: str) -> None:
        with self.db.connection() as conn:
            conn.execute(
                "UPDATE sources SET last_success_at = ?, last_error_at = NULL, "
                "last_error_code = NULL, updated_at = ? WHERE id = ?",
                (iso_utc(), iso_utc(), source_id),
            )

    def mark_source_error(self, source_id: str, code: str) -> None:
        with self.db.connection() as conn:
            conn.execute(
                "UPDATE sources SET last_error_at = ?, last_error_code = ?, updated_at = ? "
                "WHERE id = ?",
                (iso_utc(), code, iso_utc(), source_id),
            )

    # Cursors -------------------------------------------------------------
    def get_cursor_row(self, source_id: str, conn: sqlite3.Connection | None = None) -> Any:
        if conn is not None:
            return conn.execute(
                "SELECT * FROM sync_cursors WHERE source_id = ?", (source_id,)
            ).fetchone()
        with self.db.connection() as own:
            return own.execute(
                "SELECT * FROM sync_cursors WHERE source_id = ?", (source_id,)
            ).fetchone()

    def save_cursor(
        self,
        conn: sqlite3.Connection,
        source_id: str,
        *,
        cursor_type: str,
        cursor_value: str | None,
        last_message_time: str | None,
        last_platform_message_id: str | None,
        etag: str | None,
        last_modified: str | None,
    ) -> None:
        conn.execute(
            "INSERT INTO sync_cursors(source_id, cursor_type, cursor_value, "
            "last_message_time, last_platform_message_id, etag, last_modified, updated_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?) "
            "ON CONFLICT(source_id) DO UPDATE SET cursor_type=excluded.cursor_type, "
            "cursor_value=excluded.cursor_value, "
            "last_message_time=excluded.last_message_time, "
            "last_platform_message_id=excluded.last_platform_message_id, "
            "etag=excluded.etag, last_modified=excluded.last_modified, "
            "updated_at=excluded.updated_at",
            (
                source_id,
                cursor_type,
                cursor_value,
                last_message_time,
                last_platform_message_id,
                etag,
                last_modified,
                iso_utc(),
            ),
        )

    # Conversations -------------------------------------------------------
    def upsert_conversation(
        self,
        conn: sqlite3.Connection,
        source_id: str,
        platform_conversation_id: str | None,
        name: str,
        kind: str,
    ) -> str:
        if platform_conversation_id:
            row = conn.execute(
                "SELECT id FROM conversations WHERE source_id = ? "
                "AND platform_conversation_id = ?",
                (source_id, platform_conversation_id),
            ).fetchone()
            if row:
                return row["id"]
        else:
            row = conn.execute(
                "SELECT id FROM conversations WHERE source_id = ? AND name = ? "
                "AND platform_conversation_id IS NULL",
                (source_id, name),
            ).fetchone()
            if row:
                return row["id"]
        conversation_id = new_id()
        now = iso_utc()
        conn.execute(
            "INSERT INTO conversations(id, source_id, platform_conversation_id, name, kind, "
            "enabled, config_json, created_at, updated_at) VALUES (?, ?, ?, ?, ?, 1, '{}', ?, ?)",
            (conversation_id, source_id, platform_conversation_id, name, kind, now, now),
        )
        return conversation_id

    # Messages ------------------------------------------------------------
    def find_message_by_platform(
        self, conn: sqlite3.Connection, source_id: str, conversation_id: str | None,
        platform_message_id: str,
    ) -> Any:
        if conversation_id is None:
            return conn.execute(
                "SELECT * FROM messages WHERE source_id = ? AND conversation_id IS NULL "
                "AND platform_message_id = ? AND deleted_at IS NULL",
                (source_id, platform_message_id),
            ).fetchone()
        return conn.execute(
            "SELECT * FROM messages WHERE source_id = ? AND conversation_id = ? "
            "AND platform_message_id = ? AND deleted_at IS NULL",
            (source_id, conversation_id, platform_message_id),
        ).fetchone()

    def find_recent_by_hash(
        self,
        conn: sqlite3.Connection,
        content_hash: str,
        since_iso: str,
        cross_source: bool,
        source_id: str,
    ) -> Any:
        sql = (
            "SELECT * FROM messages WHERE content_hash = ? AND published_at >= ? "
            "AND deleted_at IS NULL "
        )
        params: list[Any] = [content_hash, since_iso]
        if not cross_source:
            sql += "AND source_id = ? "
            params.append(source_id)
        sql += "ORDER BY published_at DESC LIMIT 1"
        return conn.execute(sql, params).fetchone()

    def insert_message(self, conn: sqlite3.Connection, data: dict[str, Any]) -> str:
        message_id = data.get("id") or new_id()
        now = iso_utc()
        conn.execute(
            "INSERT INTO messages(id, source_id, account_id, conversation_id, sender_id, "
            "sender_name, platform_message_id, published_at, collected_at, content_type, "
            "content_text, content_completeness, source_url, raw_payload_ref, content_hash, "
            "processing_status, created_at, updated_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                message_id,
                data["source_id"],
                data.get("account_id"),
                data.get("conversation_id"),
                data.get("sender_id"),
                data.get("sender_name"),
                data.get("platform_message_id"),
                data["published_at"],
                data.get("collected_at") or now,
                data.get("content_type", "text"),
                data.get("content_text"),
                data.get("content_completeness", "complete"),
                data.get("source_url"),
                data.get("raw_payload_ref"),
                data["content_hash"],
                data.get("processing_status", "pending"),
                now,
                now,
            ),
        )
        self.add_message_source(
            conn, message_id, data["source_id"], message_id, data.get("source_url")
        )
        return message_id

    def add_message_source(
        self,
        conn: sqlite3.Connection,
        message_id: str,
        source_id: str,
        source_message_id: str | None,
        source_url: str | None,
    ) -> None:
        conn.execute(
            "INSERT OR IGNORE INTO message_sources(message_id, source_id, source_message_id, "
            "source_url, created_at) VALUES (?, ?, ?, ?, ?)",
            (message_id, source_id, source_message_id, source_url, iso_utc()),
        )

    def get_message(self, message_id: str) -> dict[str, Any] | None:
        with self.db.connection() as conn:
            row = conn.execute(
                "SELECT m.*, s.name AS source_name, s.type AS source_type, "
                "c.name AS conversation_name, c.kind AS conversation_kind "
                "FROM messages m JOIN sources s ON s.id = m.source_id "
                "LEFT JOIN conversations c ON c.id = m.conversation_id "
                "WHERE m.id = ?",
                (message_id,),
            ).fetchone()
            if row is None:
                return None
            result = dict(row)
            processing = conn.execute(
                "SELECT * FROM message_processing WHERE message_id = ? AND is_current = 1",
                (message_id,),
            ).fetchone()
            result["processing"] = _decode_processing(processing)
            sources = conn.execute(
                "SELECT ms.*, s.name AS source_name FROM message_sources ms "
                "JOIN sources s ON s.id = ms.source_id WHERE ms.message_id = ?",
                (message_id,),
            ).fetchall()
            result["sources"] = [dict(r) for r in sources]
            return result

    def list_messages(
        self,
        *,
        limit: int = 50,
        offset: int = 0,
        source_id: str | None = None,
        status: str | None = None,
        category: str | None = None,
        date_text: str | None = None,
        query_text: str | None = None,
        priorities: list[str] | None = None,
        unread_only: bool = False,
        todo_only: bool = False,
        ignored_only: bool = False,
        zone_name: str = "Asia/Shanghai",
    ) -> list[dict[str, Any]]:
        sql = (
            "SELECT m.*, s.name AS source_name, s.type AS source_type, "
            "c.name AS conversation_name, COALESCE(p.category, s.default_category) AS category, "
            "p.priority, p.urgency, "
            "p.summary_short, p.deadline_at, p.status AS processing_result "
            "FROM messages m JOIN sources s ON s.id = m.source_id "
            "LEFT JOIN conversations c ON c.id = m.conversation_id "
            "LEFT JOIN message_processing p ON p.message_id = m.id AND p.is_current = 1 "
            "WHERE m.deleted_at IS NULL "
        )
        params: list[Any] = []
        if source_id:
            sql += "AND m.source_id = ? "
            params.append(source_id)
        if status:
            sql += "AND m.processing_status = ? "
            params.append(status)
        if category:
            sql += "AND COALESCE(p.category, s.default_category) = ? "
            params.append(category)
        if date_text:
            from .timeutil import local_day_bounds

            start, end = local_day_bounds(date_text, zone_name)
            sql += "AND m.published_at >= ? AND m.published_at < ? "
            params.extend([start, end])
        if query_text:
            pattern = f"%{query_text.strip()}%"
            sql += (
                "AND (m.content_text LIKE ? OR p.summary_short LIKE ? "
                "OR p.summary_long LIKE ? OR m.sender_name LIKE ? OR c.name LIKE ?) "
            )
            params.extend([pattern] * 5)
        if unread_only:
            sql += "AND m.is_read = 0 "
        if priorities:
            cleaned = [p for p in priorities if p in ("urgent", "high", "normal", "low")]
            if cleaned:
                placeholders = ", ".join("?" for _ in cleaned)
                sql += (
                    "AND (CASE WHEN p.urgency = 'urgent' THEN 'urgent' "
                    "ELSE COALESCE(p.priority, 'normal') END) "
                    f"IN ({placeholders}) "
                )
                params.extend(cleaned)
        if todo_only:
            sql += (
                "AND (p.deadline_at IS NOT NULL OR "
                "(p.action_items_json IS NOT NULL AND p.action_items_json NOT IN ('[]', ''))) "
                "AND m.ignored_at IS NULL "
            )
        if ignored_only:
            sql += "AND m.ignored_at IS NOT NULL "
        sql += "ORDER BY m.published_at DESC LIMIT ? OFFSET ?"
        params.extend([min(limit, 200), offset])
        with self.db.connection() as conn:
            return [_decode_row(dict(r)) for r in conn.execute(sql, params)]

    def messages_for_day(self, date_text: str, zone_name: str) -> list[dict[str, Any]]:
        from .timeutil import local_day_bounds

        start, end = local_day_bounds(date_text, zone_name)
        with self.db.connection() as conn:
            rows = conn.execute(
                "SELECT m.*, s.name AS source_name, s.type AS source_type, "
                "c.name AS conversation_name, COALESCE(p.category, s.default_category) AS category, "
                "p.priority, p.urgency, "
                "p.summary_short, p.summary_long, p.tags_json, p.action_items_json, "
                "p.deadline_at, p.facts_json, p.status AS processing_result, "
                "p.confidence, p.model_name "
                "FROM messages m JOIN sources s ON s.id = m.source_id "
                "LEFT JOIN conversations c ON c.id = m.conversation_id "
                "LEFT JOIN message_processing p ON p.message_id = m.id AND p.is_current = 1 "
                "WHERE m.deleted_at IS NULL AND m.ignored_at IS NULL "
                "AND COALESCE(m.published_at, m.collected_at) >= ? "
                "AND COALESCE(m.published_at, m.collected_at) < ? "
                "ORDER BY m.published_at",
                (start, end),
            ).fetchall()
            return [_decode_row(dict(r)) for r in rows]

    def recent_message_timestamps(self, limit: int = 5000) -> list[str]:
        """Timestamps of visible messages, newest first."""
        with self.db.connection() as conn:
            rows = conn.execute(
                "SELECT COALESCE(published_at, collected_at) AS ts FROM messages "
                "WHERE deleted_at IS NULL AND ignored_at IS NULL "
                "ORDER BY ts DESC LIMIT ?",
                (limit,),
            ).fetchall()
        return [str(row["ts"]) for row in rows if row["ts"]]

    def messages_for_conversation_day(
        self, source_id: str, conversation_id: str, date_text: str, zone_name: str
    ) -> list[dict[str, Any]]:
        from .timeutil import local_day_bounds

        start, end = local_day_bounds(date_text, zone_name)
        with self.db.connection() as conn:
            rows = conn.execute(
                "SELECT m.*, c.name AS conversation_name FROM messages m "
                "LEFT JOIN conversations c ON c.id = m.conversation_id "
                "WHERE m.deleted_at IS NULL AND m.ignored_at IS NULL "
                "AND m.source_id = ? AND m.conversation_id = ? "
                "AND COALESCE(m.published_at, m.collected_at) >= ? "
                "AND COALESCE(m.published_at, m.collected_at) < ? "
                "ORDER BY m.published_at",
                (source_id, conversation_id, start, end),
            ).fetchall()
            return [_decode_row(dict(r)) for r in rows]

    def upsert_conversation_digest(
        self, *, source_id: str, conversation_id: str, conversation_name: str,
        date_text: str, message_count: int, summary_md: str, model_name: str,
    ) -> None:
        now = iso_utc()
        with self.db.connection() as conn:
            conn.execute(
                "INSERT INTO conversation_digests(id, source_id, conversation_id, "
                "conversation_name, date_text, message_count, summary_md, model_name, "
                "created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?) "
                "ON CONFLICT(source_id, conversation_id, date_text) DO UPDATE SET "
                "conversation_name = excluded.conversation_name, "
                "message_count = excluded.message_count, "
                "summary_md = excluded.summary_md, "
                "model_name = excluded.model_name, "
                "updated_at = excluded.updated_at",
                (new_id(), source_id, conversation_id, conversation_name, date_text,
                 message_count, summary_md, model_name, now, now),
            )

    def conversation_digests_for_day(self, date_text: str) -> list[dict[str, Any]]:
        with self.db.connection() as conn:
            rows = conn.execute(
                "SELECT d.*, s.name AS source_name FROM conversation_digests d "
                "LEFT JOIN sources s ON s.id = d.source_id "
                "WHERE d.date_text = ? ORDER BY d.source_id, d.conversation_name",
                (date_text,),
            ).fetchall()
            return [dict(row) for row in rows]

    def open_job_exists(self, job_type: str, source_id: str, **match: Any) -> bool:
        """True when a matching pending/running job is already queued."""
        with self.db.connection() as conn:
            rows = conn.execute(
                "SELECT payload_json FROM jobs WHERE type = ? AND source_id = ? "
                "AND status IN ('pending', 'running')",
                (job_type, source_id),
            ).fetchall()
        for row in rows:
            payload = loads(row["payload_json"]) or {}
            if all(str(payload.get(key)) == str(value) for key, value in match.items()):
                return True
        return False

    def open_digest_job_exists(self, date_text: str) -> bool:
        with self.db.connection() as conn:
            rows = conn.execute(
                "SELECT payload_json FROM jobs WHERE type = 'generate_digest' "
                "AND status IN ('pending', 'running')"
            ).fetchall()
        return any(
            str((loads(row["payload_json"]) or {}).get("date") or "") == date_text
            for row in rows
        )

    def update_message_status(self, message_id: str, status: str) -> None:
        with self.db.connection() as conn:
            conn.execute(
                "UPDATE messages SET processing_status = ?, updated_at = ? WHERE id = ?",
                (status, iso_utc(), message_id),
            )

    def update_message_fields(self, message_id: str, data: dict[str, Any]) -> None:
        allowed = {"processing_status", "content_text", "content_type"}
        sets = [f"{k} = ?" for k in data if k in allowed]
        values = [v for k, v in data.items() if k in allowed]
        if not sets:
            return
        sets.append("updated_at = ?")
        values.extend([iso_utc(), message_id])
        with self.db.connection() as conn:
            conn.execute(f"UPDATE messages SET {', '.join(sets)} WHERE id = ?", values)

    def set_message_read(self, message_id: str, read: bool) -> None:
        with self.db.connection() as conn:
            conn.execute(
                "UPDATE messages SET is_read = ?, updated_at = ? "
                "WHERE id = ? AND deleted_at IS NULL",
                (1 if read else 0, iso_utc(), message_id),
            )

    def set_message_ignored(self, message_id: str, ignored: bool) -> None:
        """Ignored messages stay visible but are left out of digests."""
        with self.db.connection() as conn:
            conn.execute(
                "UPDATE messages SET ignored_at = ?, updated_at = ? "
                "WHERE id = ? AND deleted_at IS NULL",
                (iso_utc() if ignored else None, iso_utc(), message_id),
            )

    def reassign_message_category(self, source_id: str, category: str) -> int:
        """Re-apply a source's category to its already processed messages."""
        with self.db.connection() as conn:
            cursor = conn.execute(
                "UPDATE message_processing SET category = ?, updated_at = ? "
                "WHERE is_current = 1 AND message_id IN "
                "(SELECT id FROM messages WHERE source_id = ? AND deleted_at IS NULL)",
                (category, iso_utc(), source_id),
            )
            return cursor.rowcount or 0

    def mark_all_messages_read(self) -> int:
        """Mark every visible unread message as read; returns rows changed."""
        with self.db.connection() as conn:
            cursor = conn.execute(
                "UPDATE messages SET is_read = 1, updated_at = ? "
                "WHERE is_read = 0 AND deleted_at IS NULL",
                (iso_utc(),),
            )
            return cursor.rowcount or 0

    def hide_message(self, message_id: str) -> None:
        """Keep the message and its processing result, but hide it from lists."""
        now = iso_utc()
        with self.db.connection() as conn:
            conn.execute(
                "UPDATE messages SET deleted_at = ?, updated_at = ? "
                "WHERE id = ? AND deleted_at IS NULL",
                (now, now, message_id),
            )

    def delete_message(self, message_id: str, *, purge: bool = False) -> int:
        """Hide a message, or permanently remove it and its dependent records."""
        if not purge:
            self.hide_message(message_id)
            return 1
        with self.db.connection() as conn:
            conn.execute("DELETE FROM notification_records WHERE message_id = ?", (message_id,))
            conn.execute("DELETE FROM jobs WHERE message_id = ?", (message_id,))
            cursor = conn.execute("DELETE FROM messages WHERE id = ?", (message_id,))
            return int(cursor.rowcount or 0)

    def delete_messages(self, message_ids: list[str], *, purge: bool = False) -> int:
        ids = [str(value) for value in message_ids if value]
        if not ids:
            return 0
        placeholders = ", ".join("?" for _ in ids)
        if not purge:
            now = iso_utc()
            with self.db.connection() as conn:
                cursor = conn.execute(
                    f"UPDATE messages SET deleted_at = ?, updated_at = ? "
                    f"WHERE id IN ({placeholders}) AND deleted_at IS NULL",
                    [now, now, *ids],
                )
                return int(cursor.rowcount or 0)
        with self.db.connection() as conn:
            conn.execute(
                f"DELETE FROM notification_records WHERE message_id IN ({placeholders})", ids
            )
            conn.execute(f"DELETE FROM jobs WHERE message_id IN ({placeholders})", ids)
            cursor = conn.execute(
                f"DELETE FROM messages WHERE id IN ({placeholders})", ids
            )
            return int(cursor.rowcount or 0)

    # Processing ----------------------------------------------------------
    def save_processing(self, message_id: str, result: dict[str, Any]) -> str:
        processing_id = new_id()
        now = iso_utc()
        with self.db.connection() as conn:
            conn.execute(
                "UPDATE message_processing SET is_current = 0 WHERE message_id = ? AND is_current = 1",
                (message_id,),
            )
            conn.execute(
                "INSERT INTO message_processing(id, message_id, processor_version, rule_version, "
                "model_provider, model_name, category, subcategory, tags_json, summary_short, "
                "summary_long, facts_json, action_items_json, deadline_at, priority, urgency, "
                "confidence, status, error_code, is_current, created_at, updated_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 1, ?, ?)",
                (
                    processing_id,
                    message_id,
                    result.get("processor_version", "0.1.0"),
                    result.get("rule_version"),
                    result.get("model_provider"),
                    result.get("model_name"),
                    result.get("category"),
                    result.get("subcategory"),
                    dumps(result.get("tags", [])),
                    result.get("summary_short"),
                    result.get("summary_long"),
                    dumps(result.get("facts", [])),
                    dumps(result.get("action_items", [])),
                    result.get("deadline_at"),
                    result.get("priority", "normal"),
                    result.get("urgency", "normal"),
                    result.get("confidence"),
                    result.get("status", "success"),
                    result.get("error_code"),
                    now,
                    now,
                ),
            )
            conn.execute(
                "UPDATE messages SET processing_status = ?, updated_at = ? WHERE id = ?",
                ("failed" if result.get("status") == "failed" else "processed", now, message_id),
            )
        return processing_id

    def override_processing(self, message_id: str, fields: dict[str, Any]) -> dict[str, Any]:
        message = self.get_message(message_id)
        if message is None:
            raise ValueError("message not found")
        current = message.get("processing") or {}
        merged = {
            "processor_version": "manual",
            "rule_version": current.get("rule_version"),
            "model_provider": current.get("model_provider"),
            "model_name": current.get("model_name"),
            "category": fields.get("category", current.get("category")),
            "subcategory": fields.get("subcategory", current.get("subcategory")),
            "tags": fields.get("tags", current.get("tags") or []),
            "summary_short": fields.get("summary_short", current.get("summary_short")),
            "summary_long": fields.get("summary_long", current.get("summary_long")),
            "facts": current.get("facts") or [],
            "action_items": fields.get("action_items", current.get("action_items") or []),
            "deadline_at": fields.get("deadline_at", current.get("deadline_at")),
            "priority": fields.get("priority", current.get("priority") or "normal"),
            "urgency": fields.get("urgency", current.get("urgency") or "normal"),
            "confidence": current.get("confidence"),
            "status": "success",
        }
        self.save_processing(message_id, merged)
        if fields.get("processing_status"):
            self.update_message_status(message_id, fields["processing_status"])
        result = self.get_message(message_id)
        assert result is not None
        return result

    # Jobs ----------------------------------------------------------------
    def enqueue_job(
        self,
        job_type: str,
        *,
        source_id: str | None = None,
        message_id: str | None = None,
        payload: dict[str, Any] | None = None,
        priority: int = 100,
        max_attempts: int = 3,
        delay_seconds: int = 0,
        trace_id: str | None = None,
        conn: sqlite3.Connection | None = None,
    ) -> str:
        job_id = new_id()
        now = iso_utc()
        available = iso_utc(utc_now() + timedelta(seconds=delay_seconds))
        sql = (
            "INSERT INTO jobs(id, type, source_id, message_id, payload_json, status, priority, "
            "attempts, max_attempts, available_at, trace_id, created_at, updated_at) "
            "VALUES (?, ?, ?, ?, ?, 'pending', ?, 0, ?, ?, ?, ?, ?)"
        )
        params = (
            job_id,
            job_type,
            source_id,
            message_id,
            dumps(payload or {}),
            priority,
            max_attempts,
            available,
            trace_id or job_id,
            now,
            now,
        )
        if conn is not None:
            try:
                conn.execute(sql, params)
            except sqlite3.IntegrityError:
                row = conn.execute(
                    "SELECT id FROM jobs WHERE type = ? AND message_id = ? AND payload_json = ? "
                    "AND status IN ('pending', 'running', 'retry_waiting') LIMIT 1",
                    (job_type, message_id, dumps(payload or {})),
                ).fetchone()
                return row["id"] if row else job_id
            return job_id
        with self.db.connection() as own:
            try:
                own.execute(sql, params)
            except sqlite3.IntegrityError:
                row = own.execute(
                    "SELECT id FROM jobs WHERE type = ? AND message_id = ? AND payload_json = ? "
                    "AND status IN ('pending', 'running', 'retry_waiting') LIMIT 1",
                    (job_type, message_id, dumps(payload or {})),
                ).fetchone()
                return row["id"] if row else job_id
        return job_id

    def has_active_job(self, job_type: str, *, source_id: str | None = None) -> bool:
        sql = "SELECT 1 FROM jobs WHERE type = ? AND status IN ('pending','running','retry_waiting')"
        params: list[Any] = [job_type]
        if source_id:
            sql += " AND source_id = ?"
            params.append(source_id)
        with self.db.connection() as conn:
            return conn.execute(sql + " LIMIT 1", params).fetchone() is not None

    def claim_next_job(self) -> dict[str, Any] | None:
        now = iso_utc()
        with self.db.connection() as conn:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute(
                "SELECT * FROM jobs WHERE status IN ('pending', 'retry_waiting') "
                "AND available_at <= ? ORDER BY priority, available_at LIMIT 1",
                (now,),
            ).fetchone()
            if row is None:
                return None
            conn.execute(
                "UPDATE jobs SET status = 'running', attempts = attempts + 1, started_at = ?, "
                "updated_at = ? WHERE id = ?",
                (now, now, row["id"]),
            )
            result = dict(row)
            result["attempts"] = int(row["attempts"]) + 1
            result["status"] = "running"
            return result

    def recover_interrupted_jobs(self) -> int:
        """Put jobs left running by a previous process back into the queue."""
        now = iso_utc()
        with self.db.connection() as conn:
            cursor = conn.execute(
                "UPDATE jobs SET status = 'retry_waiting', error_code = 'INTERRUPTED', "
                "error_message = '上次运行被中断', available_at = ?, updated_at = ? "
                "WHERE status = 'running'",
                (now, now),
            )
            return cursor.rowcount or 0

    def finish_job(self, job_id: str, *, error_code: str | None = None,
                   error_message: str | None = None, retryable: bool = True) -> None:
        now = iso_utc()
        with self.db.connection() as conn:
            row = conn.execute("SELECT * FROM jobs WHERE id = ?", (job_id,)).fetchone()
            if row is None:
                return
            if error_code:
                if retryable and row["attempts"] < row["max_attempts"]:
                    delay = min(600, 30 * (2 ** max(0, int(row["attempts"]) - 1)))
                    available = iso_utc(utc_now() + timedelta(seconds=delay))
                    conn.execute(
                        "UPDATE jobs SET status = 'retry_waiting', error_code = ?, "
                        "error_message = ?, available_at = ?, updated_at = ? WHERE id = ?",
                        (error_code, error_message, available, now, job_id),
                    )
                else:
                    conn.execute(
                        "UPDATE jobs SET status = 'failed', error_code = ?, error_message = ?, "
                        "finished_at = ?, updated_at = ? WHERE id = ?",
                        (error_code, error_message, now, now, job_id),
                    )
            else:
                conn.execute(
                    "UPDATE jobs SET status = 'succeeded', finished_at = ?, updated_at = ?, "
                    "error_code = NULL, error_message = NULL WHERE id = ?",
                    (now, now, job_id),
                )

    def has_active_digest_job(self, date_text: str) -> bool:
        with self.db.connection() as conn:
            row = conn.execute(
                "SELECT 1 FROM jobs WHERE type = 'generate_digest' "
                "AND status IN ('pending','running','retry_waiting') "
                "AND payload_json LIKE ? LIMIT 1",
                (f'%"date": "{date_text}"%',),
            ).fetchone()
            return row is not None

    def cancel_job(self, job_id: str) -> None:
        with self.db.connection() as conn:
            conn.execute(
                "UPDATE jobs SET status = 'cancelled', finished_at = ?, updated_at = ? "
                "WHERE id = ? AND status IN ('pending', 'retry_waiting')",
                (iso_utc(), iso_utc(), job_id),
            )

    def retry_job(self, job_id: str) -> None:
        now = iso_utc()
        with self.db.connection() as conn:
            conn.execute(
                "UPDATE jobs SET status = 'pending', available_at = ?, updated_at = ?, "
                "finished_at = NULL, error_code = NULL, error_message = NULL "
                "WHERE id = ? AND status IN ('failed', 'cancelled')",
                (now, now, job_id),
            )

    def list_jobs(self, limit: int = 50, status: str | None = None) -> list[dict[str, Any]]:
        sql = "SELECT * FROM jobs "
        params: list[Any] = []
        if status:
            sql += "WHERE status = ? "
            params.append(status)
        sql += "ORDER BY created_at DESC LIMIT ?"
        params.append(min(limit, 200))
        with self.db.connection() as conn:
            return [dict(r) for r in conn.execute(sql, params)]

    def get_job(self, job_id: str) -> dict[str, Any] | None:
        with self.db.connection() as conn:
            return row_to_dict(conn.execute("SELECT * FROM jobs WHERE id = ?", (job_id,)).fetchone())

    # Digests -------------------------------------------------------------
    def upsert_digest(
        self,
        date_text: str,
        *,
        file_path: str,
        content_hash: str,
        message_count: int,
        urgent_count: int,
        status: str = "generated",
        error_code: str | None = None,
    ) -> None:
        now = iso_utc()
        with self.db.connection() as conn:
            conn.execute(
                "INSERT INTO digests(date, file_path, content_hash, message_count, urgent_count, "
                "status, error_code, generated_at, updated_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?) "
                "ON CONFLICT(date) DO UPDATE SET file_path=excluded.file_path, "
                "content_hash=excluded.content_hash, message_count=excluded.message_count, "
                "urgent_count=excluded.urgent_count, status=excluded.status, "
                "error_code=excluded.error_code, generated_at=excluded.generated_at, "
                "updated_at=excluded.updated_at",
                (date_text, file_path, content_hash, message_count, urgent_count,
                 status, error_code, now, now),
            )

    def get_digest(self, date_text: str) -> dict[str, Any] | None:
        with self.db.connection() as conn:
            return row_to_dict(
                conn.execute("SELECT * FROM digests WHERE date = ?", (date_text,)).fetchone()
            )

    def list_digests(self, limit: int = 30) -> list[dict[str, Any]]:
        with self.db.connection() as conn:
            return [
                dict(r)
                for r in conn.execute(
                    "SELECT * FROM digests ORDER BY date DESC LIMIT ?", (limit,)
                )
            ]

    # Stats / audit -------------------------------------------------------
    def stats(self) -> dict[str, Any]:
        with self.db.connection() as conn:
            def scalar(sql: str, params: Iterable[Any] = ()) -> int:
                row = conn.execute(sql, tuple(params)).fetchone()
                return int(row[0] or 0)

            return {
                "sources_enabled": scalar(
                    "SELECT COUNT(*) FROM sources WHERE enabled = 1 AND deleted_at IS NULL"
                ),
                "sources_error": scalar(
                    "SELECT COUNT(*) FROM sources WHERE last_error_code IS NOT NULL "
                    "AND deleted_at IS NULL"
                ),
                "messages_pending": scalar(
                    "SELECT COUNT(*) FROM messages WHERE processing_status = 'pending' "
                    "AND deleted_at IS NULL"
                ),
                "messages_failed": scalar(
                    "SELECT COUNT(*) FROM messages WHERE processing_status = 'failed' "
                    "AND deleted_at IS NULL"
                ),
                "messages_24h": scalar(
                    "SELECT COUNT(*) FROM messages WHERE collected_at >= ? "
                    "AND deleted_at IS NULL",
                    (iso_utc(utc_now() - timedelta(hours=24)),),
                ),
                "messages_unread": scalar(
                    "SELECT COUNT(*) FROM messages WHERE is_read = 0 "
                    "AND deleted_at IS NULL"
                ),
                "jobs_pending": scalar(
                    "SELECT COUNT(*) FROM jobs WHERE status IN ('pending','retry_waiting')"
                ),
                "jobs_failed": scalar("SELECT COUNT(*) FROM jobs WHERE status = 'failed'"),
            }

    def audit(self, action: str, target_type: str, target_id: str | None,
              detail: dict[str, Any] | None = None, trace_id: str | None = None) -> None:
        with self.db.connection() as conn:
            conn.execute(
                "INSERT INTO audit_logs(id, action, target_type, target_id, detail_json, "
                "trace_id, created_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
                (new_id(), action, target_type, target_id, dumps(detail or {}), trace_id,
                 iso_utc()),
            )

    def record_model_call(self, *, message_id: str | None, provider: str, model_name: str,
                          input_chars: int, output_chars: int, duration_ms: int,
                          success: bool, error_code: str | None = None) -> None:
        with self.db.connection() as conn:
            conn.execute(
                "INSERT INTO model_calls(id, message_id, provider, model_name, input_chars, "
                "output_chars, duration_ms, success, error_code, created_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (new_id(), message_id, provider, model_name, input_chars, output_chars,
                 duration_ms, 1 if success else 0, error_code, iso_utc()),
            )

    def model_calls_today(self, since_iso: str) -> int:
        with self.db.connection() as conn:
            row = conn.execute(
                "SELECT COUNT(*) FROM model_calls WHERE created_at >= ?", (since_iso,)
            ).fetchone()
            return int(row[0] or 0)

def _decode_processing(row: sqlite3.Row | None) -> dict[str, Any] | None:
    if row is None:
        return None
    result = dict(row)
    result["tags"] = loads(result.pop("tags_json", "[]"), [])
    result["facts"] = loads(result.pop("facts_json", "[]"), [])
    result["action_items"] = loads(result.pop("action_items_json", "[]"), [])
    return result


def _decode_row(row: dict[str, Any]) -> dict[str, Any]:
    for key in ("tags_json", "action_items_json", "facts_json"):
        if key in row:
            row[key] = loads(row[key], [])
    return row

