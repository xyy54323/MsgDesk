"""Transport objects shared by adapters and services."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass
class AdapterCapabilities:
    adapter_type: str
    adapter_version: str = "0.1.0"
    supports_login: bool = False
    supports_group: bool = False
    supports_private: bool = False
    supports_attachments: bool = False
    supports_stable_message_id: bool = True
    supports_incremental_cursor: bool = True
    content_level: str = "complete"
    requires_local_client: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {
            "adapter_type": self.adapter_type,
            "adapter_version": self.adapter_version,
            "supports_login": self.supports_login,
            "supports_group": self.supports_group,
            "supports_private": self.supports_private,
            "supports_attachments": self.supports_attachments,
            "supports_stable_message_id": self.supports_stable_message_id,
            "supports_incremental_cursor": self.supports_incremental_cursor,
            "content_level": self.content_level,
            "requires_local_client": self.requires_local_client,
        }


@dataclass
class ValidationResult:
    ok: bool
    message: str = ""
    details: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {"ok": self.ok, "message": self.message, "details": self.details}


@dataclass
class ConnectionStatus:
    status: str = "unknown"
    message: str = ""
    client_version: str | None = None
    adapter_version: str = "0.1.0"
    details: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "status": self.status,
            "message": self.message,
            "client_version": self.client_version,
            "adapter_version": self.adapter_version,
            "details": self.details,
        }


@dataclass
class Cursor:
    cursor_type: str = "mixed"
    cursor_value: str | None = None
    last_message_time: str | None = None
    last_platform_message_id: str | None = None
    etag: str | None = None
    last_modified: str | None = None

    @classmethod
    def from_row(cls, row: Any) -> "Cursor":
        if row is None:
            return cls()
        return cls(
            cursor_type=row["cursor_type"] or "mixed",
            cursor_value=row["cursor_value"],
            last_message_time=row["last_message_time"],
            last_platform_message_id=row["last_platform_message_id"],
            etag=row["etag"],
            last_modified=row["last_modified"],
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "cursor_type": self.cursor_type,
            "cursor_value": self.cursor_value,
            "last_message_time": self.last_message_time,
            "last_platform_message_id": self.last_platform_message_id,
            "etag": self.etag,
            "last_modified": self.last_modified,
        }


@dataclass
class RawMessage:
    content_text: str
    published_at: str
    published_time_known: bool = True
    platform_message_id: str | None = None
    conversation_id: str | None = None
    conversation_name: str | None = None
    conversation_kind: str = "web"
    sender_id: str | None = None
    sender_name: str | None = None
    content_type: str = "text"
    content_completeness: str = "complete"
    source_url: str | None = None
    attachments: list[dict[str, Any]] = field(default_factory=list)
    raw_payload: dict[str, Any] = field(default_factory=dict)


@dataclass
class FetchResult:
    messages: list[RawMessage] = field(default_factory=list)
    next_cursor: Cursor | None = None
    has_more: bool = False
    partial: bool = False
    warnings: list[str] = field(default_factory=list)
    stats: dict[str, int] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "messages": [m.__dict__ for m in self.messages],
            "next_cursor": self.next_cursor.to_dict() if self.next_cursor else None,
            "has_more": self.has_more,
            "partial": self.partial,
            "warnings": self.warnings,
            "stats": self.stats,
        }

