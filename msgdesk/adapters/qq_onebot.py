"""Read-only QQ adapter for OneBot 11 compatible clients (NapCat/Lagrange)."""

from __future__ import annotations

import json
import urllib.error
import urllib.request
from datetime import datetime, timezone
from typing import Any

from ..errors import AppError
from ..models import (
    AdapterCapabilities,
    ConnectionStatus,
    Cursor,
    FetchResult,
    RawMessage,
    ValidationResult,
)
from ..timeutil import iso_utc, parse_datetime
from .base import BaseAdapter, FetchOptions, SourceContext

MAX_PAGES = 5


def mask_account(value: str) -> str:
    text = str(value or "")
    if len(text) <= 4:
        return text[:1] + "***" + text[-1:] if len(text) > 1 else "***"
    return text[:3] + "***" + text[-2:]


class QQOneBotAdapter(BaseAdapter):
    adapter_type = "qq_onebot"
    adapter_version = "0.2.0"

    def get_capabilities(self) -> AdapterCapabilities:
        return AdapterCapabilities(
            adapter_type=self.adapter_type,
            adapter_version=self.adapter_version,
            supports_login=True,
            supports_group=True,
            supports_private=True,
            supports_attachments=True,
            supports_stable_message_id=True,
            supports_incremental_cursor=True,
            content_level="complete",
            requires_local_client=True,
        )

    def validate_config(self, config: dict[str, Any]) -> ValidationResult:
        base_url = str(config.get("base_url") or "").strip()
        if not base_url:
            return ValidationResult(False, "OneBot 配置缺少 base_url")
        if not base_url.startswith(("http://", "https://")):
            return ValidationResult(False, "OneBot base_url 必须以 http:// 或 https:// 开头")
        conversations = config.get("selected_conversations")
        if conversations is not None and not isinstance(conversations, list):
            return ValidationResult(False, "selected_conversations 必须是数组")
        return ValidationResult(True, "OneBot 配置格式有效")

    def get_status(self, source: SourceContext | None = None) -> ConnectionStatus:
        if source is None:
            return ConnectionStatus(
                status="unknown",
                message="未提供来源配置",
                adapter_version=self.adapter_version,
            )
        try:
            payload = self._call(source, "get_status", {})
        except AppError as exc:
            return ConnectionStatus(
                status=exc.code,
                message=exc.message,
                adapter_version=self.adapter_version,
                details={"error_code": exc.code},
            )
        data = payload.get("data") if isinstance(payload.get("data"), dict) else payload
        online = bool(data.get("online", data.get("good", False)))
        app_name = data.get("app_name") or data.get("appName")
        app_version = data.get("app_version") or data.get("appVersion")
        return ConnectionStatus(
            status="ready" if online else "offline",
            message=str(data.get("message") or ("已连接" if online else "OneBot 客户端离线")),
            client_version=f"{app_name} {app_version}".strip() if app_name or app_version else None,
            adapter_version=self.adapter_version,
            details={"online": online, "app_name": app_name, "app_version": app_version},
        )

    def login(self, source: SourceContext | None = None) -> dict[str, Any]:
        if source is None:
            raise AppError("CONFIG_INVALID", "登录检查缺少来源配置")
        payload = self._call(source, "get_login_info", {})
        data = payload.get("data") if isinstance(payload.get("data"), dict) else payload
        user_id = str(data.get("user_id") or data.get("userId") or "")
        nickname = str(data.get("nickname") or data.get("nick") or "")
        if not user_id and not nickname:
            raise AppError("AUTH_REQUIRED", "OneBot 未返回登录账号信息")
        return {
            "nickname": nickname,
            "account": mask_account(user_id),
            "online": True,
        }

    def fetch_incremental(
        self,
        source: SourceContext,
        cursor: Cursor,
        options: FetchOptions,
    ) -> FetchResult:
        conversations = source.config.get("selected_conversations") or []
        if not isinstance(conversations, list) or not conversations:
            return FetchResult(
                messages=[],
                next_cursor=None,
                warnings=["未选择 QQ 群，本次不采集"],
                stats={"fetched": 0, "skipped": 0, "failed": 0},
            )
        count = int(
            source.config.get("count_per_poll")
            or options.max_items
            or 100
        )
        count = max(1, min(count, 1000))
        cursor_time = parse_datetime(cursor.last_message_time)
        # First sync should not backfill old history: only today's messages.
        zone_name = str(source.config.get("timezone") or "Asia/Shanghai")
        from ..timeutil import local_zone
        from datetime import datetime

        today_start = (
            datetime.now(local_zone(zone_name))
            .replace(hour=0, minute=0, second=0, microsecond=0)
        )
        messages: list[RawMessage] = []
        warnings: list[str] = []
        failures = 0
        first_error: AppError | None = None

        for conversation in conversations:
            conversation_id = str(conversation)
            try:
                items = self._history(source, conversation_id, cursor, count)
            except AppError as exc:
                failures += 1
                first_error = first_error or exc
                warnings.append(f"{conversation_id}: {exc.code}")
                continue
            items.sort(key=lambda item: int(item.get("time") or 0))
            for item in items:
                timestamp = self._timestamp(item.get("time"))
                if cursor_time is not None:
                    if timestamp is None or timestamp <= cursor_time:
                        continue
                elif timestamp is not None and timestamp < today_start:
                    continue
                converted = self._convert(item, conversation_id)
                if converted is not None:
                    messages.append(converted)

        if failures and failures == len(conversations) and first_error is not None:
            raise first_error

        messages.sort(key=lambda item: item.published_at)
        if cursor_time is None and len(messages) > count:
            messages = messages[-count:]

        if first_error is not None:
            # Keep the old cursor when any conversation failed: advancing a
            # global timestamp could skip the failed group's messages.
            return FetchResult(
                messages=messages,
                next_cursor=None,
                partial=True,
                warnings=warnings,
                stats={"fetched": len(messages), "skipped": 0, "failed": failures},
            )

        next_cursor = None
        if messages:
            latest = messages[-1]
            next_cursor = Cursor(
                cursor_type="onebot",
                cursor_value=str(latest.platform_message_id or ""),
                last_message_time=latest.published_at,
                last_platform_message_id=str(latest.platform_message_id or ""),
            )
        elif cursor.last_message_time or cursor.cursor_value:
            next_cursor = cursor
        return FetchResult(
            messages=messages,
            next_cursor=next_cursor,
            partial=False,
            warnings=warnings,
            stats={"fetched": len(messages), "skipped": 0, "failed": failures},
        )

    # Helpers ------------------------------------------------------------
    def _history(
        self,
        source: SourceContext,
        conversation_id: str,
        cursor: Cursor,
        count: int,
    ) -> list[dict[str, Any]]:
        return self._history_range(
            source,
            conversation_id,
            count=count,
            start_sequence=cursor.last_platform_message_id or cursor.cursor_value or 0,
            reverse=False,
        )

    def fetch_range(
        self,
        source: SourceContext,
        start: "datetime",
        end: "datetime",
        *,
        count: int = 50,
        max_pages: int = MAX_PAGES,
    ) -> FetchResult:
        """Pull history between two local datetimes, newest page first."""
        conversations = source.config.get("selected_conversations") or []
        if not isinstance(conversations, list) or not conversations:
            return FetchResult(
                messages=[],
                next_cursor=None,
                warnings=["未选择 QQ 群，本次不采集"],
                stats={"fetched": 0},
            )
        start_ts = start.timestamp()
        end_ts = end.timestamp()
        messages: list[RawMessage] = []
        warnings: list[str] = []
        for conversation in conversations:
            conversation_id = str(conversation)
            collected: dict[str, dict[str, Any]] = {}
            sequence: Any = 0
            reverse = False
            try:
                for _ in range(max_pages):
                    batch = self._history_page(
                        source,
                        conversation_id,
                        sequence=sequence,
                        count=count,
                        reverse=reverse,
                    )
                    if not batch:
                        break
                    fresh = 0
                    for item in batch:
                        key = str(item.get("message_id") or "")
                        if key and key in collected:
                            continue
                        collected[key] = item
                        fresh += 1
                    times = [
                        self._timestamp(item.get("time"))
                        for item in batch
                        if self._timestamp(item.get("time")) is not None
                    ]
                    if not times:
                        break
                    if min(times).timestamp() <= start_ts:
                        break
                    if fresh == 0:
                        break
                    earliest = min(
                        batch,
                        key=lambda item: (
                            self._timestamp(item.get("time")) or start
                        ).timestamp(),
                    )
                    sequence = int(earliest.get("message_id") or 0)
                    reverse = True
                    if sequence <= 0:
                        break
            except AppError as exc:
                warnings.append(f"{conversation_id}: {exc.code}")
                continue
            for item in collected.values():
                timestamp = self._timestamp(item.get("time"))
                if timestamp is None:
                    continue
                if timestamp.timestamp() < start_ts or timestamp.timestamp() > end_ts:
                    continue
                converted = self._convert(item, conversation_id)
                if converted is not None:
                    messages.append(converted)
        messages.sort(key=lambda item: item.published_at)
        return FetchResult(
            messages=messages,
            next_cursor=None,
            warnings=warnings,
            stats={"fetched": len(messages)},
        )

    def _history_page(
        self,
        source: SourceContext,
        conversation_id: str,
        *,
        sequence: Any,
        count: int,
        reverse: bool,
    ) -> list[dict[str, Any]]:
        private = conversation_id.startswith("private:")
        target = conversation_id.split(":", 1)[1] if private else conversation_id
        action = "get_friend_msg_history" if private else "get_group_msg_history"
        try:
            sequence = int(sequence or 0)
        except (TypeError, ValueError):
            sequence = 0
        params: dict[str, Any] = {
            "count": count,
            "message_seq": sequence,
            "reverseOrder": reverse,
        }
        params["user_id" if private else "group_id"] = target
        payload = self._call(source, action, params)
        data = payload.get("data")
        if isinstance(data, dict):
            batch = data.get("messages") or data.get("message") or []
        elif isinstance(data, list):
            batch = data
        else:
            batch = []
        return [item for item in batch if isinstance(item, dict)] if isinstance(batch, list) else []

    def _history_range(
        self,
        source: SourceContext,
        conversation_id: str,
        *,
        count: int,
        start_sequence: Any,
        reverse: bool,
    ) -> list[dict[str, Any]]:
        private = conversation_id.startswith("private:")
        target = conversation_id.split(":", 1)[1] if private else conversation_id
        action = "get_friend_msg_history" if private else "get_group_msg_history"
        sequence = start_sequence or 0
        try:
            sequence = int(sequence)
        except (TypeError, ValueError):
            sequence = 0
        collected: list[dict[str, Any]] = []
        seen: set[str] = set()
        for _ in range(MAX_PAGES):
            params: dict[str, Any] = {
                "count": count,
                "message_seq": sequence,
                "reverseOrder": reverse,
            }
            params["user_id" if private else "group_id"] = target
            payload = self._call(source, action, params)
            data = payload.get("data")
            if isinstance(data, dict):
                batch = data.get("messages") or data.get("message") or []
            elif isinstance(data, list):
                batch = data
            else:
                batch = []
            if not isinstance(batch, list) or not batch:
                break
            fresh = 0
            for item in batch:
                if not isinstance(item, dict):
                    continue
                key = str(item.get("message_id") or item.get("time") or "")
                if key and key in seen:
                    continue
                if key:
                    seen.add(key)
                collected.append(item)
                fresh += 1
            if len(batch) < count or fresh == 0:
                break
            page_seq = [
                int(item.get("message_id") or 0)
                for item in batch
                if isinstance(item, dict)
            ]
            sequence = max(page_seq or [0])
            if sequence <= 0:
                break
        return collected

    def list_groups(self, source: SourceContext) -> list[dict[str, str]]:
        """Groups the logged-in account has joined, for the picker."""
        parsed = self._call(source, "get_group_list", {})
        rows = parsed.get("data")
        if not isinstance(rows, list):
            return []
        groups: list[dict[str, str]] = []
        for row in rows:
            if not isinstance(row, dict):
                continue
            group_id = str(row.get("group_id") or "").strip()
            if not group_id:
                continue
            groups.append(
                {
                    "id": group_id,
                    "name": str(row.get("group_name") or group_id),
                    "member_count": str(row.get("member_count") or ""),
                }
            )
        groups.sort(key=lambda item: item["name"])
        return groups

    def _call(
        self,
        source: SourceContext,
        action: str,
        params: dict[str, Any],
    ) -> dict[str, Any]:
        base_url = str(source.config.get("base_url") or "").strip().rstrip("/")
        if not base_url:
            raise AppError("CONFIG_INVALID", "OneBot 配置缺少 base_url")
        timeout = float(source.config.get("timeout_seconds", 10) or 10)
        headers = {"Content-Type": "application/json"}
        token = str(source.config.get("access_token") or "").strip()
        if token:
            headers["Authorization"] = f"Bearer {token}"
        request = urllib.request.Request(
            f"{base_url}/{action}",
            data=json.dumps(params, ensure_ascii=False).encode("utf-8"),
            headers=headers,
            method="POST",
        )
        try:
            with urllib.request.urlopen(request, timeout=timeout) as response:
                raw = response.read(4 * 1024 * 1024)
        except urllib.error.HTTPError as exc:
            if exc.code in (401, 403):
                raise AppError("AUTH_REQUIRED", "OneBot 访问令牌无效或缺失") from None
            if exc.code == 429:
                raise AppError("RATE_LIMITED", "OneBot 请求过于频繁") from None
            raise AppError(
                "REMOTE_TEMPORARY_ERROR", f"OneBot 请求失败：HTTP {exc.code}"
            ) from None
        except urllib.error.URLError as exc:
            reason = exc.reason
            if isinstance(reason, ConnectionRefusedError):
                raise AppError(
                    "CLIENT_NOT_RUNNING", "OneBot 客户端未启动或端口不可达"
                ) from None
            if isinstance(reason, (TimeoutError, OSError)) and "timed out" in str(reason):
                raise AppError("NETWORK_TIMEOUT", "OneBot 请求超时") from None
            raise AppError("NETWORK_UNAVAILABLE", "无法连接 OneBot 客户端") from None
        except TimeoutError:
            raise AppError("NETWORK_TIMEOUT", "OneBot 请求超时") from None
        except OSError:
            raise AppError("NETWORK_UNAVAILABLE", "无法连接 OneBot 客户端") from None
        try:
            parsed = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            raise AppError("REMOTE_TEMPORARY_ERROR", "OneBot 返回了非 JSON 响应") from None
        if not isinstance(parsed, dict):
            raise AppError("REMOTE_TEMPORARY_ERROR", "OneBot 响应格式不正确")
        retcode = parsed.get("retcode", 0)
        if str(retcode) not in ("0", "200"):
            if str(retcode) in ("401", "403"):
                raise AppError("AUTH_REQUIRED", "OneBot 访问令牌无效或缺失")
            raise AppError(
                "REMOTE_TEMPORARY_ERROR",
                f"OneBot 调用失败：retcode={retcode}",
            )
        return parsed

    def _convert(self, item: dict[str, Any], fallback_conversation: str) -> RawMessage | None:
        message_type = str(item.get("message_type") or "group")
        group_id = item.get("group_id")
        user_id = item.get("user_id")
        conversation_kind = "private" if message_type == "private" else "group"
        conversation_id = (
            str(user_id)
            if conversation_kind == "private" and user_id
            else str(group_id or fallback_conversation)
        )
        conversation_name = None
        if isinstance(item.get("sender"), dict):
            sender = item["sender"]
            sender_name = sender.get("card") or sender.get("nickname")
        else:
            sender_name = None
        sender_name = sender_name or str(item.get("sender") or "未知用户")

        text_parts: list[str] = []
        attachments: list[dict[str, Any]] = []
        segments = item.get("message")
        if isinstance(segments, list):
            for segment in segments:
                if not isinstance(segment, dict):
                    continue
                segment_type = str(segment.get("type") or "unknown")
                data = segment.get("data") if isinstance(segment.get("data"), dict) else {}
                if segment_type == "text":
                    text_parts.append(str(data.get("text") or ""))
                elif segment_type == "image":
                    text_parts.append("[图片]")
                    attachments.append(
                        {
                            "type": "image",
                            "url": data.get("url") or data.get("file"),
                        }
                    )
                elif segment_type == "file":
                    text_parts.append("[文件]")
                    attachments.append(
                        {
                            "type": "file",
                            "name": data.get("file") or data.get("name"),
                            "url": data.get("url"),
                        }
                    )
                elif segment_type == "at":
                    name = data.get("name") or data.get("qq") or ""
                    text_parts.append(f"@{name}")
                elif segment_type == "reply":
                    continue
                else:
                    text_parts.append(f"[{segment_type}]")
        text = "".join(text_parts).strip()
        if not text:
            text = str(item.get("raw_message") or "").strip()
        if not text:
            return None

        published_at = self._timestamp(item.get("time"))
        if published_at is None:
            return None
        message_id = str(item.get("message_id") or "")
        return RawMessage(
            platform_message_id=message_id or None,
            conversation_id=conversation_id,
            conversation_name=conversation_name,
            conversation_kind=conversation_kind,
            sender_id=str(user_id) if user_id is not None else None,
            sender_name=sender_name,
            published_at=iso_utc(published_at),
            content_text=text,
            content_type="text" if not attachments else "mixed",
            content_completeness="complete",
            attachments=[a for a in attachments if a.get("url") or a.get("name")],
            raw_payload={"platform": "qq", **item},
        )

    @staticmethod
    def _timestamp(value: Any) -> datetime | None:
        if value is None or value == "":
            return None
        try:
            seconds = float(value)
        except (TypeError, ValueError):
            return parse_datetime(str(value))
        if seconds > 10_000_000_000:
            seconds /= 1000
        try:
            return datetime.fromtimestamp(seconds, timezone.utc)
        except (OverflowError, OSError, ValueError):
            return None
