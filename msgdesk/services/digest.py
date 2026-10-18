"""Idempotent daily Markdown archive generation."""

from __future__ import annotations

import hashlib
import os
import re
from collections import Counter, defaultdict
from datetime import datetime
from pathlib import Path
from typing import Any

from ..config import AppConfig
from ..repository import Repository
from ..timeutil import local_date_of, local_zone, parse_datetime, utc_now

PRIORITY_RANK = {"urgent": 0, "high": 1, "normal": 2, "low": 3}
PRIORITY_LABEL = {"urgent": "紧急", "high": "高", "normal": "普通", "low": "低"}


class DigestService:
    def __init__(self, config: AppConfig, repo: Repository):
        self.config = config
        self.repo = repo

    def generate(self, date_text: str | None = None) -> dict[str, Any]:
        date_text = date_text or utc_now().astimezone(
            local_zone(self.config.system.timezone)
        ).date().isoformat()
        messages = self.repo.messages_for_day(date_text, self.config.system.timezone)
        markdown = self._render(date_text, messages)
        digest = hashlib.sha256(markdown.encode("utf-8")).hexdigest()
        path = self._path_for(date_text)
        path.parent.mkdir(parents=True, exist_ok=True)
        previous = None
        if path.exists():
            previous = hashlib.sha256(path.read_bytes()).hexdigest()
        if previous != digest:
            temp = path.with_suffix(".tmp")
            temp.write_text(markdown, encoding="utf-8", newline="\n")
            os.replace(temp, path)
        urgent = sum(1 for m in messages if self._priority(m) == "urgent")
        self.repo.upsert_digest(
            date_text,
            file_path=str(path),
            content_hash=digest,
            message_count=len(messages),
            urgent_count=urgent,
        )
        return {
            "date": date_text,
            "file_path": str(path),
            "message_count": len(messages),
            "urgent_count": urgent,
            "changed": previous != digest,
            "content_hash": digest,
        }

    def generate_missing(self, limit_days: int = 60) -> int:
        """Create digests for every day that has messages but no digest yet."""
        zone = self.config.system.timezone
        existing = {
            str(item.get("date")) for item in self.repo.list_digests(400)
        }
        days: list[str] = []
        for timestamp in self.repo.recent_message_timestamps():
            day = local_date_of(timestamp, zone)
            if day and day not in existing and day not in days:
                days.append(day)
        for day in days[:limit_days]:
            self.generate(day)
        return min(len(days), limit_days)

    def _render(self, date_text: str, messages: list[dict[str, Any]]) -> str:
        zone = self.config.system.timezone
        updated = max((m.get("updated_at") for m in messages if m.get("updated_at")),
                      default=None)
        updated_text = self._local_time(updated, zone) if updated else "暂无消息"
        ordered = sorted(
            messages,
            key=lambda m: (PRIORITY_RANK.get(self._priority(m), 2), m["published_at"]),
        )
        by_category: dict[str, list[dict[str, Any]]] = defaultdict(list)
        by_source: Counter[str] = Counter()
        conversations: dict[str, list[dict[str, Any]]] = {}
        others: list[dict[str, Any]] = []
        for message in ordered:
            by_source[message.get("source_name") or "未知来源"] += 1
            if message.get("source_type") == "qq_onebot":
                key = str(message.get("conversation_id") or "")
                conversations.setdefault(key, []).append(message)
            else:
                others.append(message)
                by_category[message.get("category") or "其他"].append(message)
        pending = [
            m for m in others
            if m.get("processing_status") in ("pending", "processing", "failed")
            or (m.get("processing_result") in ("fallback", "failed"))
        ]

        lines: list[str] = [
            f"# {date_text} 消息摘要",
            "",
            f"数据更新时间：{updated_text}",
            "",
            "## 概览",
            "",
            f"- 消息总数：{len(messages)}",
            f"- 待处理或处理失败：{len(pending)}",
            f"- 覆盖来源：{len(by_source)}",
            "",
        ]
        lines.extend(["## 今日待办", ""])
        todos = self._todos(others)
        lines.extend(todos or ["- 暂无提取到待办事项"])
        lines.extend(["", "## 群聊摘要", ""])
        summaries = {
            str(item.get("conversation_id")): item
            for item in self.repo.conversation_digests_for_day(date_text)
        }
        if not conversations:
            lines.extend(["- 无", ""])
        for conversation_id, items in conversations.items():
            name = next(
                (item.get("conversation_name") for item in items if item.get("conversation_name")),
                "群聊",
            )
            lines.append(f"### {self._escape(str(name))}（{len(items)} 条）")
            lines.append("")
            summary = (summaries.get(conversation_id) or {}).get("summary_md") or "（摘要生成中）"
            lines.extend(str(summary).strip().splitlines() or ["（摘要生成中）"])
            lines.append("")
        lines.extend(["## 按主题汇总", ""])
        if not by_category:
            lines.extend(["- 无", ""])
        for category in sorted(by_category):
            lines.append(f"### {category}")
            lines.append("")
            lines.extend(self._message_lines(by_category[category], zone, terse=True))
            lines.append("")
        lines.extend(["## 来源索引", ""])
        for source, count in sorted(by_source.items()):
            lines.append(f"- {source}：{count} 条")
        lines.extend(["", "## 待处理与失败", ""])
        if pending:
            for message in pending:
                reason = message.get("processing_result") or message.get("processing_status")
                lines.append(
                    f"- `{message['id'][:8]}` {self._plain(message)}（{reason}）"
                )
        else:
            lines.append("- 无")
        lines.append("")
        return "\n".join(lines)

    def _message_lines(self, messages: list[dict[str, Any]], zone: str,
                       terse: bool = False) -> list[str]:
        if not messages:
            return ["- 无"]
        lines: list[str] = []
        for message in messages:
            summary = message.get("summary_short") or self._plain(message)
            category = message.get("category") or "其他"
            source = message.get("source_name") or "未知来源"
            when = self._local_time(message["published_at"], zone)
            tags = message.get("tags_json") or []
            tag_text = f"　标签：{'、'.join(tags[:5])}" if tags else ""
            lines.append(f"- [{category}] {self._escape(summary)}")
            if not terse:
                lines.append(f"  - 来源：{source}　原文时间：{when}{tag_text}")
                link = message.get("source_url")
                if link:
                    lines.append(f"  - 原文：{link}")
        return lines

    def _todos(self, messages: list[dict[str, Any]]) -> list[str]:
        lines: list[str] = []
        seen: set[str] = set()
        for message in messages:
            actions = message.get("action_items_json") or []
            for action in actions[:3]:
                normalized = re.sub(r"\s+", "", str(action))
                if normalized in seen:
                    continue
                seen.add(normalized)
                action_text = re.sub(r"[。！？!?，,；;：:\s]+$", "", str(action))
                lines.append(f"- [ ] {self._escape(action_text)}")
        return lines[:30]

    def _priority(self, message: dict[str, Any]) -> str:
        if message.get("urgency") == "urgent":
            return "urgent"
        return message.get("priority") or "normal"

    def _plain(self, message: dict[str, Any]) -> str:
        text = (message.get("content_text") or "").strip().replace("\n", " ")
        return self._escape(text[:80] + ("..." if len(text) > 80 else "")) or "（无文本）"

    @staticmethod
    def _escape(value: str) -> str:
        return value.replace("[", "\\[").replace("]", "\\]").replace("|", "\\|")

    @staticmethod
    def _local_time(value: str, zone_name: str) -> str:
        parsed = parse_datetime(value)
        if not parsed:
            return value
        return parsed.astimezone(local_zone(zone_name)).strftime("%Y-%m-%d %H:%M")

    def _path_for(self, date_text: str) -> Path:
        year, month, _ = date_text.split("-")
        return self.config.digest_dir / year / f"{year}-{month}" / f"{date_text}.md"

