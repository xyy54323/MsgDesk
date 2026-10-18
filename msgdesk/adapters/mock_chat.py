"""Fixture-driven chat adapters used before real platform clients exist."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from ..models import AdapterCapabilities, Cursor, FetchResult, RawMessage, ValidationResult
from ..timeutil import iso_utc, parse_datetime
from .base import BaseAdapter, FetchOptions, SourceContext


class MockChatAdapter(BaseAdapter):
    adapter_type = "mock_chat"

    def get_capabilities(self) -> AdapterCapabilities:
        return AdapterCapabilities(
            adapter_type=self.adapter_type,
            adapter_version=self.adapter_version,
            supports_group=True,
            supports_private=True,
            supports_attachments=True,
            supports_stable_message_id=True,
            content_level="complete",
        )

    def validate_config(self, config: dict[str, Any]) -> ValidationResult:
        if not config.get("messages") and not config.get("fixture_path"):
            return ValidationResult(False, "需要 messages 数组或 fixture_path")
        return ValidationResult(True, "配置有效（模拟适配器）")

    def fetch_incremental(
        self, source: SourceContext, cursor: Cursor, options: FetchOptions
    ) -> FetchResult:
        items = source.config.get("messages") or []
        fixture = source.config.get("fixture_path")
        if fixture:
            path = Path(fixture)
            if not path.is_absolute():
                path = Path.cwd() / path
            items = json.loads(path.read_text(encoding="utf-8"))
        try:
            start = int(cursor.cursor_value or 0)
        except ValueError:
            start = 0
        messages: list[RawMessage] = []
        for index, item in enumerate(items[start:start + options.max_items], start=start):
            published = parse_datetime(item.get("published_at")) or parse_datetime(iso_utc())
            messages.append(
                RawMessage(
                    platform_message_id=item.get("platform_message_id") or f"mock-{index}",
                    conversation_id=item.get("conversation_id") or "mock-group",
                    conversation_name=item.get("conversation_name") or "模拟群",
                    conversation_kind=item.get("conversation_kind", "group"),
                    sender_id=item.get("sender_id"),
                    sender_name=item.get("sender_name", "模拟用户"),
                    published_at=iso_utc(published),
                    content_text=item.get("content_text", ""),
                    content_type=item.get("content_type", "text"),
                    content_completeness=item.get("content_completeness", "complete"),
                    source_url=item.get("source_url"),
                    raw_payload=item,
                )
            )
        next_cursor = Cursor(
            cursor_type="platform_id",
            cursor_value=str(min(len(items), start + len(messages))),
            last_message_time=messages[-1].published_at if messages else cursor.last_message_time,
            last_platform_message_id=(
                messages[-1].platform_message_id if messages else cursor.last_platform_message_id
            ),
        )
        return FetchResult(
            messages=messages,
            next_cursor=next_cursor,
            stats={"fetched": len(messages), "skipped": 0, "failed": 0},
        )


class PendingChatAdapter(BaseAdapter):
    """Declares capabilities for a chat platform whose client bridge is not built yet.

    Fetching fails with AUTH_REQUIRED so the workbench can show an actionable
    state. It deliberately exposes no send/reply methods.
    """

    adapter_type = "chat_pending"
    platform = "chat"

    def get_capabilities(self) -> AdapterCapabilities:
        return AdapterCapabilities(
            adapter_type=self.adapter_type,
            adapter_version=self.adapter_version,
            supports_login=True,
            supports_group=True,
            supports_private=False,
            supports_attachments=True,
            supports_stable_message_id=True,
            content_level="complete",
            requires_local_client=True,
        )

    def validate_config(self, config: dict[str, Any]) -> ValidationResult:
        return ValidationResult(
            True,
            "配置格式有效；真实平台桥接尚未接入，当前仅演示状态",
        )

    def fetch_incremental(
        self, source: SourceContext, cursor: Cursor, options: FetchOptions
    ) -> FetchResult:
        from ..errors import AppError

        raise AppError(
            "AUTH_REQUIRED",
            f"{self.platform} 适配器尚未接入本地客户端桥接，请先使用 RSS、网页或模拟来源",
        )

