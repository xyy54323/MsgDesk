"""Read-only import adapter for exported WeChat, WeCom and QQ chat files."""

from __future__ import annotations

import csv
import hashlib
import io
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from ..errors import AppError
from ..models import (
    AdapterCapabilities,
    Cursor,
    FetchResult,
    RawMessage,
    ValidationResult,
)
from ..timeutil import iso_utc, parse_datetime
from .base import BaseAdapter, FetchOptions, SourceContext

TIME_FIELDS = ("time", "timestamp", "create_time", "date", "datetime")
CONVERSATION_FIELDS = ("conversation", "chatroom", "group", "room_name")
SENDER_FIELDS = ("sender", "from", "nickname", "sender_name")
CONTENT_FIELDS = ("content", "text", "message", "msg")
ID_FIELDS = ("message_id", "msg_id", "id")
SUPPORTED_PLATFORMS = {"wechat", "wecom", "qq"}
DEFAULT_GLOBS = ["*.json", "*.jsonl", "*.csv"]


def _first(record: dict[str, Any], names: tuple[str, ...]) -> Any:
    lowered = {str(key).strip().lower(): value for key, value in record.items()}
    for name in names:
        value = lowered.get(name)
        if value is not None and str(value).strip() != "":
            return value
    return None


def _text(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, (dict, list)):
        return json.dumps(value, ensure_ascii=False, sort_keys=True)
    return str(value)


def parse_import_time(value: Any) -> datetime | None:
    """Parse seconds/milliseconds or common date strings into an aware UTC time."""
    if value is None or str(value).strip() == "":
        return None
    if isinstance(value, (int, float)):
        return _timestamp(value)
    text = str(value).strip()
    numeric = text.replace(".", "", 1)
    if numeric.isdigit() and len(numeric.split(".", 1)[0]) >= 10:
        try:
            return _timestamp(float(text))
        except ValueError:
            pass
    return parse_datetime(text)


def _timestamp(value: float) -> datetime | None:
    seconds = float(value)
    if seconds > 10_000_000_000:
        seconds /= 1000
    try:
        return datetime.fromtimestamp(seconds, timezone.utc)
    except (OverflowError, OSError, ValueError):
        return None


def _fingerprint(conversation: str, sender: str, published_at: str, content: str) -> str:
    material = f"{conversation}|{sender}|{published_at}|{content}"
    return "import-" + hashlib.sha1(material.encode("utf-8")).hexdigest()


class ChatImportAdapter(BaseAdapter):
    adapter_type = "chat_import"
    adapter_version = "0.2.0"

    def get_capabilities(self) -> AdapterCapabilities:
        return AdapterCapabilities(
            adapter_type=self.adapter_type,
            adapter_version=self.adapter_version,
            supports_login=False,
            supports_group=True,
            supports_private=False,
            supports_attachments=False,
            supports_stable_message_id=True,
            supports_incremental_cursor=True,
            content_level="complete",
            requires_local_client=False,
        )

    def validate_config(self, config: dict[str, Any]) -> ValidationResult:
        directory = str(config.get("directory") or "").strip()
        if not directory:
            return ValidationResult(False, "聊天记录导入缺少 directory")
        if not Path(directory).expanduser().is_dir():
            return ValidationResult(False, f"聊天记录导入目录不存在: {directory}")
        platform = str(config.get("platform") or "wechat").lower()
        if platform not in SUPPORTED_PLATFORMS:
            return ValidationResult(
                False,
                "platform 必须是 wechat、wecom 或 qq",
            )
        globs = config.get("file_globs", DEFAULT_GLOBS)
        if not isinstance(globs, list) or not all(isinstance(item, str) for item in globs):
            return ValidationResult(False, "file_globs 必须是字符串数组")
        return ValidationResult(True, "聊天记录导入配置有效")

    def fetch_incremental(
        self,
        source: SourceContext,
        cursor: Cursor,
        options: FetchOptions,
    ) -> FetchResult:
        validation = self.validate_config(source.config)
        if not validation.ok:
            raise AppError("CONFIG_INVALID", validation.message)
        platform = str(source.config.get("platform") or "wechat").lower()
        directory = Path(str(source.config["directory"])).expanduser()
        globs = source.config.get("file_globs") or DEFAULT_GLOBS

        files: list[Path] = []
        seen_files: set[str] = set()
        for pattern in globs:
            for path in directory.glob(pattern):
                key = str(path.resolve())
                if path.is_file() and key not in seen_files:
                    seen_files.add(key)
                    files.append(path)
        files.sort(key=lambda path: (path.stat().st_mtime_ns, str(path)))

        previous = self._load_states(cursor.cursor_value)
        states = dict(previous)
        messages: list[RawMessage] = []
        warnings: list[str] = []
        changed = False

        for path in files:
            key = str(path.resolve())
            stat = path.stat()
            state = previous.get(key) or {}
            if (
                int(state.get("size", -1)) == stat.st_size
                and int(state.get("mtime_ns", -1)) == stat.st_mtime_ns
                and int(state.get("offset", -1)) >= stat.st_size
            ):
                continue
            data = path.read_bytes()
            offset = max(0, int(state.get("offset") or 0))
            if offset > len(data):
                offset = 0
            if offset and state.get("prefix_sha1"):
                prefix_hash = hashlib.sha1(data[:offset]).hexdigest()
                if prefix_hash != state.get("prefix_sha1"):
                    offset = 0
            records, new_offset, file_warnings = self._read_records(
                path, data, offset, warnings
            )
            warnings.extend(file_warnings)
            fallback_time = datetime.fromtimestamp(stat.st_mtime, timezone.utc)
            seen_ids: set[str] = set()
            for record in records:
                converted = self._convert(record, platform, path, fallback_time, warnings)
                if converted is None:
                    continue
                message_id = str(converted.platform_message_id or "")
                if message_id and message_id in seen_ids:
                    continue
                if message_id:
                    seen_ids.add(message_id)
                messages.append(converted)
            states[key] = {
                "size": len(data),
                "mtime_ns": stat.st_mtime_ns,
                "offset": new_offset,
                "prefix_sha1": hashlib.sha1(data[:new_offset]).hexdigest(),
            }
            changed = True

        if not changed:
            return FetchResult(
                messages=[],
                next_cursor=cursor,
                stats={"fetched": 0, "skipped": 0, "failed": 0},
            )

        messages.sort(key=lambda item: item.published_at)
        latest = messages[-1] if messages else None
        next_cursor = Cursor(
            cursor_type="chat_import",
            cursor_value=json.dumps(states, ensure_ascii=False, sort_keys=True),
            last_message_time=latest.published_at if latest else cursor.last_message_time,
            last_platform_message_id=(
                latest.platform_message_id if latest else cursor.last_platform_message_id
            ),
        )
        return FetchResult(
            messages=messages,
            next_cursor=next_cursor,
            warnings=warnings,
            stats={"fetched": len(messages), "skipped": 0, "failed": 0},
        )

    # Parsing -----------------------------------------------------------
    def _read_records(
        self,
        path: Path,
        data: bytes,
        offset: int,
        warnings: list[str],
    ) -> tuple[list[dict[str, Any]], int, list[str]]:
        suffix = path.suffix.lower()
        if suffix == ".jsonl":
            return self._read_jsonl(data, offset)
        if suffix == ".csv":
            return self._read_csv(data, offset)
        if suffix == ".json":
            return self._read_json(data, offset, warnings)
        return [], offset, [f"{path.name}: 不支持的文件类型"]

    @staticmethod
    def _read_jsonl(data: bytes, offset: int) -> tuple[list[dict[str, Any]], int, list[str]]:
        start = offset if offset == 0 else _line_start(data, offset)
        tail = data[start:]
        records: list[dict[str, Any]] = []
        warnings: list[str] = []
        consumed = start
        for raw_line in tail.splitlines(keepends=True):
            line = raw_line.decode("utf-8-sig", errors="replace").strip()
            if not line:
                consumed += len(raw_line)
                continue
            try:
                value = json.loads(line)
            except json.JSONDecodeError:
                if not raw_line.endswith((b"\n", b"\r")):
                    break
                warnings.append("JSONL 行无法解析为 JSON 对象，已跳过")
                consumed += len(raw_line)
                continue
            if isinstance(value, dict):
                records.append(value)
            consumed += len(raw_line)
        return records, consumed, warnings

    @staticmethod
    def _read_csv(data: bytes, offset: int) -> tuple[list[dict[str, Any]], int, list[str]]:
        newline = data.find(b"\n")
        if newline < 0:
            return [], offset, ["CSV 缺少表头"]
        start = offset if offset == 0 else _line_start(data, offset)
        payload = data if start == 0 else data[: newline + 1] + data[start:]
        text = payload.decode("utf-8-sig", errors="replace")
        reader = csv.DictReader(io.StringIO(text))
        records: list[dict[str, Any]] = []
        warnings: list[str] = []
        try:
            for row in reader:
                if row and any(str(value or "").strip() for value in row.values()):
                    records.append(dict(row))
        except csv.Error:
            warnings.append("CSV 内容解析失败，已跳过异常行")
        return records, len(data), warnings

    @staticmethod
    def _read_json(
        data: bytes,
        offset: int,
        warnings: list[str],
    ) -> tuple[list[dict[str, Any]], int, list[str]]:
        text = data.decode("utf-8-sig", errors="replace")
        if offset == 0:
            try:
                value = json.loads(text)
            except json.JSONDecodeError:
                return [], 0, ["JSON 文件不是有效数组，未推进游标"]
            if isinstance(value, dict):
                for key in ("messages", "data", "records", "items"):
                    if isinstance(value.get(key), list):
                        value = value[key]
                        break
            if not isinstance(value, list):
                return [], 0, ["JSON 文件顶层必须是数组或包含 messages 数组"]
            return [item for item in value if isinstance(item, dict)], len(data), []

        tail = text[offset:].strip()
        if not tail or tail.startswith("]"):
            return [], len(data), []
        tail = tail.lstrip(",").strip()
        if tail.endswith("]"):
            tail = tail[:-1].rstrip()
        if not tail:
            return [], len(data), []
        try:
            value = json.loads("[" + tail + "]")
        except json.JSONDecodeError:
            return [], offset, ["JSON 追加内容无法解析，未推进游标"]
        return [item for item in value if isinstance(item, dict)], len(data), []

    def _convert(
        self,
        record: dict[str, Any],
        platform: str,
        path: Path,
        fallback_time: datetime,
        warnings: list[str],
    ) -> RawMessage | None:
        content = _text(_first(record, CONTENT_FIELDS)).strip()
        if not content:
            return None
        conversation = _text(_first(record, CONVERSATION_FIELDS)).strip() or "导入记录"
        sender = _text(_first(record, SENDER_FIELDS)).strip() or "未知发送者"
        parsed_time = parse_import_time(_first(record, TIME_FIELDS))
        if parsed_time is None:
            parsed_time = fallback_time
            warnings.append(f"{path.name}: 时间无法解析，使用文件修改时间")
        published_at = iso_utc(parsed_time)
        message_id = _text(_first(record, ID_FIELDS)).strip()
        if not message_id:
            message_id = _fingerprint(conversation, sender, published_at, content)
        return RawMessage(
            platform_message_id=message_id,
            conversation_id=conversation,
            conversation_name=conversation,
            conversation_kind="group",
            sender_name=sender,
            published_at=published_at,
            content_text=content,
            content_type="text",
            content_completeness="complete",
            raw_payload={
                **record,
                "platform": platform,
                "import_file": str(path),
            },
        )

    def _convert_row(
        self,
        record: dict[str, Any],
        path: Path,
        platform: str,
        fallback_time: datetime | float,
    ) -> tuple[RawMessage | None, list[str]]:
        """Compatibility helper used by import tests and callers."""
        if isinstance(fallback_time, (int, float)):
            fallback = datetime.fromtimestamp(float(fallback_time), timezone.utc)
        else:
            fallback = fallback_time
        warnings: list[str] = []
        return self._convert(record, platform, path, fallback, warnings), warnings

    @staticmethod
    def _load_states(value: str | None) -> dict[str, dict[str, Any]]:
        if not value:
            return {}
        try:
            parsed = json.loads(value)
        except json.JSONDecodeError:
            return {}
        if not isinstance(parsed, dict):
            return {}
        # Accept the nested form written by early prototypes as well.
        if isinstance(parsed.get("files"), dict):
            parsed = parsed["files"]
        return {
            str(key): item
            for key, item in parsed.items()
            if isinstance(item, dict)
        }


def _line_start(data: bytes, offset: int) -> int:
    if offset <= 0:
        return 0
    previous = data.rfind(b"\n", 0, offset)
    return previous + 1 if previous >= 0 else 0
