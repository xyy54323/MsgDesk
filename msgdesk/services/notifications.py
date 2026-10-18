"""Notification channels with quiet hours, rate limiting and dedupe."""

from __future__ import annotations

import json
import urllib.error
import urllib.request
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from ..config import AppConfig, NotificationChannelConfig
from ..errors import AppError
from ..repository import Repository
from ..timeutil import iso_utc, local_zone, utc_now

PRIORITY_RANK = {"low": 0, "normal": 1, "high": 2, "urgent": 3}
PRIORITY_LABEL = {"low": "低", "normal": "普通", "high": "高", "urgent": "紧急"}


def _clip(value: str, length: int) -> str:
    text = (value or "").strip()
    return text[:length]


def render_message(payload: dict[str, Any]) -> tuple[str, str]:
    """Render a notification payload into a title and Markdown body."""
    kind = payload.get("type")
    if kind == "digest":
        title = str(payload.get("title") or "消息摘要")[:60]
        body = "\n".join(
            [
                f"**{title}**",
                "",
                f"- 消息数：{payload.get('message_count', 0)}",
                f"- 紧急：{payload.get('urgent_count', 0)}",
            ]
        )
        if payload.get("file_path"):
            body += f"\n- 日报文件：{payload['file_path']}"
        return title, body
    if kind == "test":
        title = str(payload.get("title") or "MsgDesk 通知测试")[:60]
        return title, str(payload.get("message") or "这是一条测试通知")

    source = str(payload.get("source") or payload.get("source_name") or "新消息").strip()
    summary = str(
        payload.get("summary")
        or payload.get("summary_short")
        or payload.get("title")
        or ""
    ).strip()
    content = str(payload.get("content") or payload.get("content_text") or "").strip()
    priority = str(payload.get("priority") or "normal")
    urgency = str(payload.get("urgency") or "normal")

    if summary:
        snippet = _clip(summary, 30)
    elif content:
        snippet = _clip(content, 60)
    else:
        snippet = ""
    prefix = ""
    if priority == "urgent" or urgency == "urgent":
        prefix = "[紧急] "
    elif priority == "high":
        prefix = "[重要] "
    if snippet:
        title = f"{prefix}{source}：{snippet}"[:60]
    else:
        title = f"{prefix}新消息"[:60]

    lines: list[str] = []
    body_text = summary or content
    if body_text:
        lines.append(body_text)
        if content and summary and content != summary:
            lines.extend(["", content])
    else:
        lines.append("（无正文）")
    lines.append("")
    lines.append(f"- 来源：{source}")
    if payload.get("conversation"):
        lines.append(f"- 会话：{payload['conversation']}")
    if payload.get("category"):
        lines.append(f"- 分类：{payload['category']}")
    if priority:
        lines.append(f"- 优先级：{PRIORITY_LABEL.get(priority, priority)}")
    if payload.get("deadline_at"):
        lines.append(f"- 截止：{payload['deadline_at']}")
    if payload.get("source_url"):
        lines.append(f"- 原文：{payload['source_url']}")
    if payload.get("message_id"):
        lines.append(f"- 消息 ID：{payload['message_id']}")
    return title, "\n".join(lines)


def _post_json(
    url: str,
    data: dict[str, Any],
    timeout: float,
    label: str,
) -> dict[str, Any] | None:
    """POST JSON and map transport or service failures to stable errors.

    The label never contains credentials, so exception text stays safe to log.
    """
    request = urllib.request.Request(
        url,
        data=json.dumps(data, ensure_ascii=False).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            status = getattr(response, "status", 200)
            raw = response.read(2 * 1024 * 1024)
    except urllib.error.HTTPError as exc:
        raise AppError(
            "REMOTE_TEMPORARY_ERROR", f"{label}推送失败：HTTP {exc.code}"
        ) from None
    except (TimeoutError, OSError):
        raise AppError(
            "REMOTE_TEMPORARY_ERROR", f"{label}推送失败：网络异常"
        ) from None
    if not 200 <= status < 300:
        raise AppError(
            "REMOTE_TEMPORARY_ERROR", f"{label}推送失败：HTTP {status}"
        )
    if not raw.strip():
        return None
    try:
        parsed = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return None
    if isinstance(parsed, dict):
        for key in ("code", "errcode"):
            if key in parsed and str(parsed[key]) not in ("0", "200"):
                raise AppError(
                    "REMOTE_TEMPORARY_ERROR",
                    f"{label}推送失败：服务返回错误码 {parsed[key]}",
                )
        return parsed
    return None


class NotificationChannel:
    def send(self, payload: dict[str, Any]) -> None:
        raise NotImplementedError


class MockChannel(NotificationChannel):
    def send(self, payload: dict[str, Any]) -> None:
        return None


class FileChannel(NotificationChannel):
    def __init__(self, directory: Path):
        self.directory = directory
        self.directory.mkdir(parents=True, exist_ok=True)

    def send(self, payload: dict[str, Any]) -> None:
        path = self.directory / (
            datetime.now().astimezone().date().isoformat() + ".jsonl"
        )
        with path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(payload, ensure_ascii=False) + "\n")


class WebhookChannel(NotificationChannel):
    def __init__(self, url: str, timeout: float = 10.0):
        self.url = url
        self.timeout = timeout

    def send(self, payload: dict[str, Any]) -> None:
        request = urllib.request.Request(
            self.url,
            data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        try:
            with urllib.request.urlopen(request, timeout=self.timeout):
                return None
        except Exception as exc:
            raise AppError("REMOTE_TEMPORARY_ERROR", "通知 Webhook 发送失败") from exc


class PushPlusChannel(NotificationChannel):
    def __init__(self, token: str, endpoint: str, timeout: float = 10.0):
        self.token = token
        self.endpoint = endpoint
        self.timeout = timeout

    def send(self, payload: dict[str, Any]) -> None:
        title, body = render_message(payload)
        _post_json(
            self.endpoint,
            {
                "token": self.token,
                "title": title,
                "content": body,
                "template": "markdown",
            },
            self.timeout,
            "PushPlus ",
        )


class ServerChanChannel(NotificationChannel):
    def __init__(self, sendkey: str, endpoint: str, timeout: float = 10.0):
        self.sendkey = sendkey
        self.endpoint = endpoint
        self.timeout = timeout

    def send(self, payload: dict[str, Any]) -> None:
        title, body = render_message(payload)
        endpoint = self.endpoint
        if "{sendkey}" in endpoint:
            endpoint = endpoint.format(sendkey=self.sendkey)
        _post_json(
            endpoint,
            {"title": title, "desp": body},
            self.timeout,
            "Server酱",
        )


class WecomBotChannel(NotificationChannel):
    def __init__(self, webhook_url: str, timeout: float = 10.0):
        self.webhook_url = webhook_url
        self.timeout = timeout

    def send(self, payload: dict[str, Any]) -> None:
        title, body = render_message(payload)
        _post_json(
            self.webhook_url,
            {
                "msgtype": "markdown",
                "markdown": {"content": f"**{title}**\n\n{body}"},
            },
            self.timeout,
            "企业微信机器人",
        )


class BarkChannel(NotificationChannel):
    LEVELS = {"urgent": "critical", "high": "timeSensitive", "normal": "active", "low": "active"}

    def __init__(
        self,
        device_key: str,
        endpoint: str,
        timeout: float = 10.0,
        group: str = "MsgDesk",
    ):
        self.device_key = device_key
        self.endpoint = endpoint
        self.timeout = timeout
        self.group = group

    def send(self, payload: dict[str, Any]) -> None:
        title, body = render_message(payload)
        priority = str(payload.get("priority") or "normal")
        if payload.get("urgency") == "urgent":
            priority = "urgent"
        _post_json(
            self.endpoint,
            {
                "device_key": self.device_key,
                "title": title,
                "body": body,
                "group": self.group,
                "level": self.LEVELS.get(priority, "active"),
            },
            self.timeout,
            "Bark ",
        )


def build_channel(config: NotificationChannelConfig, data_dir: Path) -> NotificationChannel:
    values = config.config or {}
    timeout = float(values.get("timeout_seconds", 10) or 10)
    if timeout <= 0:
        raise AppError("CONFIG_INVALID", "通知渠道 timeout_seconds 必须大于 0")
    if config.type == "file":
        return FileChannel(data_dir / "notifications")
    if config.type == "webhook":
        url = values.get("url") or values.get("webhook_url")
        if not url:
            raise AppError("CONFIG_INVALID", "Webhook 通知缺少 url")
        return WebhookChannel(url, timeout)
    if config.type == "mock":
        return MockChannel()
    if config.type == "pushplus":
        token = str(values.get("token") or "").strip()
        if not token:
            raise AppError("CONFIG_INVALID", "PushPlus 通知缺少 token")
        return PushPlusChannel(
            token,
            str(values.get("endpoint") or "https://www.pushplus.plus/send"),
            timeout,
        )
    if config.type == "serverchan":
        sendkey = str(values.get("sendkey") or "").strip()
        if not sendkey:
            raise AppError("CONFIG_INVALID", "Server酱通知缺少 sendkey")
        return ServerChanChannel(
            sendkey,
            str(
                values.get("endpoint")
                or "https://sctapi.ftqq.com/{sendkey}.send"
            ),
            timeout,
        )
    if config.type == "wecom_bot":
        webhook_url = str(
            values.get("endpoint") or values.get("webhook_url") or ""
        ).strip()
        if not webhook_url:
            raise AppError("CONFIG_INVALID", "企业微信机器人通知缺少 webhook_url")
        return WecomBotChannel(webhook_url, timeout)
    if config.type == "bark":
        device_key = str(values.get("device_key") or "").strip()
        if not device_key:
            raise AppError("CONFIG_INVALID", "Bark 通知缺少 device_key")
        return BarkChannel(
            device_key,
            str(values.get("endpoint") or "https://api.day.app/push"),
            timeout,
            str(values.get("group") or "MsgDesk"),
        )
    raise AppError("CONFIG_INVALID", f"未知通知渠道类型: {config.type}")


class NotificationService:
    def __init__(self, config: AppConfig, repo: Repository):
        self.config = config
        self.repo = repo
        self.zone = local_zone(config.system.timezone)

    def notify_message(self, message_id: str, channel_id: str) -> dict[str, Any]:
        channel_config = self._channel(channel_id)
        if channel_config is None or not channel_config.enabled:
            return {"status": "skipped", "reason": "channel_disabled"}
        message = self.repo.get_message(message_id)
        if message is None:
            raise AppError("NOT_FOUND", f"消息不存在: {message_id}")
        processing = message.get("processing") or {}
        priority = processing.get("priority") or "normal"
        urgency = processing.get("urgency") or "normal"
        urgent = priority == "urgent" or urgency == "urgent"
        if channel_config.config.get("notify_urgent_only") and not urgent:
            return {"status": "suppressed", "reason": "urgent_only"}
        threshold = (
            channel_config.config.get("min_priority")
            or self.config.notifications.min_priority
        )
        effective_priority = max(
            PRIORITY_RANK.get(priority, 1),
            PRIORITY_RANK.get(urgency, 1),
        )
        if effective_priority < PRIORITY_RANK.get(threshold, 2):
            return {"status": "suppressed", "reason": "priority_below_threshold"}
        if self._in_quiet_hours(urgency):
            return {"status": "suppressed", "reason": "quiet_hours"}
        if self._hourly_count() >= self.config.notifications.hourly_limit:
            return {"status": "suppressed", "reason": "hourly_limit"}
        dedupe_key = f"message:{message_id}"
        payload = self._message_payload(message, processing)
        record_id = self.repo.reserve_notification(
            message_id=message_id,
            digest_date=None,
            channel=channel_id,
            dedupe_key=dedupe_key,
            payload=payload,
        )
        if record_id is None:
            return {"status": "suppressed", "reason": "duplicate"}
        try:
            build_channel(channel_config, self.config.data_dir).send(payload)
        except AppError as exc:
            self.repo.update_notification(record_id, "failed", exc.code)
            raise
        self.repo.update_notification(record_id, "sent")
        return {"status": "sent", "channel": channel_id, "dedupe_key": dedupe_key}

    def notify_digest(self, date_text: str, summary: dict[str, Any]) -> list[dict[str, Any]]:
        results = []
        for channel in self.config.notifications.channels:
            if not channel.enabled or not channel.config.get("notify_digest"):
                continue
            dedupe_key = f"digest:{date_text}"
            payload = {
                "type": "digest",
                "date": date_text,
                "title": f"{date_text} 消息摘要",
                "message_count": summary.get("message_count", 0),
                "urgent_count": summary.get("urgent_count", 0),
                "file_path": summary.get("file_path"),
            }
            record_id = self.repo.reserve_notification(
                message_id=None,
                digest_date=date_text,
                channel=channel.id,
                dedupe_key=dedupe_key,
                payload=payload,
            )
            if record_id is None:
                results.append({"channel": channel.id, "status": "suppressed",
                                "reason": "duplicate"})
                continue
            try:
                build_channel(channel, self.config.data_dir).send(payload)
                self.repo.update_notification(record_id, "sent")
                results.append({"channel": channel.id, "status": "sent"})
            except AppError as exc:
                self.repo.update_notification(record_id, "failed", exc.code)
                results.append({"channel": channel.id, "status": "failed",
                                "error": exc.code})
        return results

    def test_channel(self, channel_id: str) -> dict[str, Any]:
        channel = self._channel(channel_id)
        if channel is None:
            raise AppError("NOT_FOUND", f"通知渠道不存在: {channel_id}")
        build_channel(channel, self.config.data_dir).send(
            {"type": "test", "title": "MsgDesk 通知测试", "message": "这是一条测试通知"}
        )
        return {"channel": channel_id, "status": "sent"}

    def _channel(self, channel_id: str) -> NotificationChannelConfig | None:
        return next(
            (c for c in self.config.notifications.channels if c.id == channel_id), None
        )

    def _in_quiet_hours(self, urgency: str) -> bool:
        quiet = self.config.scheduling.quiet_hours
        if urgency == "urgent" and quiet.allow_urgent:
            return False
        now = utc_now().astimezone(self.zone).time()
        try:
            start = datetime.strptime(quiet.start, "%H:%M").time()
            end = datetime.strptime(quiet.end, "%H:%M").time()
        except ValueError:
            return False
        if start <= end:
            return start <= now <= end
        return now >= start or now <= end

    def _hourly_count(self) -> int:
        return self.repo.notification_count_since(iso_utc(utc_now() - timedelta(hours=1)))

    @staticmethod
    def _message_payload(message: dict[str, Any], processing: dict[str, Any]) -> dict[str, Any]:
        content = message.get("content_text") or ""
        return {
            "type": "message",
            "message_id": message["id"],
            "priority": processing.get("priority"),
            "urgency": processing.get("urgency"),
            "category": processing.get("category"),
            "source": message.get("source_name"),
            "conversation": message.get("conversation_name"),
            "summary": processing.get("summary_short"),
            "content": content,
            "title": processing.get("summary_short") or content[:80],
            "deadline_at": processing.get("deadline_at"),
            "source_url": message.get("source_url"),
        }

