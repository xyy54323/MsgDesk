"""One model call per group per day, instead of one call per message."""

from __future__ import annotations

import logging
from typing import Any

from ..errors import AppError
from ..timeutil import local_date_of
from .model import chat_text

log = logging.getLogger("msgdesk.conversation_digest")

MAX_TRANSCRIPT_CHARS = 8000

SYSTEM_PROMPT = (
    "你在帮我整理群聊。给你的是某个群一天的聊天记录（可能很零散）。\n"
    "请输出 Markdown 要点：\n"
    "1. 先用一句话说明这个群今天主要在聊什么；\n"
    "2. 再列 3~8 条要点，合并同类话题，忽略寒暄和表情；\n"
    "3. 如果有明确的通知、时间、截止日期、链接或需要我做的事，单独列出来。\n"
    "不要编造记录里没有的信息；没有实质内容就直说。"
)


class ConversationDigestService:
    def __init__(self, config: Any, repo: Any) -> None:
        self.config = config
        self.repo = repo

    def summarize(self, source_id: str, conversation_id: str, date_text: str) -> dict[str, Any]:
        messages = self.repo.messages_for_conversation_day(
            source_id, conversation_id, date_text, self.config.system.timezone
        )
        if not messages:
            return {"messages": 0, "summary": ""}
        name = next(
            (item.get("conversation_name") for item in messages if item.get("conversation_name")),
            conversation_id,
        )
        lines: list[str] = []
        for item in messages:
            when = str(item.get("published_at") or "")
            clock = when[11:16] if len(when) >= 16 else ""
            sender = item.get("sender_name") or "未知"
            text = str(item.get("content_text") or "").replace("\n", " ").strip()
            if not text:
                continue
            lines.append(f"[{clock}] {sender}: {text}")
        transcript = "\n".join(lines)[:MAX_TRANSCRIPT_CHARS]
        if not transcript:
            return {"messages": len(messages), "summary": ""}
        user = f"群名：{name}\n日期：{date_text}\n消息数：{len(messages)}\n\n{transcript}"
        try:
            summary = chat_text(self.config.model, SYSTEM_PROMPT, user)
        except AppError as exc:
            log.warning("conversation summary failed: %s", exc.message)
            raise
        self.repo.upsert_conversation_digest(
            source_id=source_id,
            conversation_id=conversation_id,
            conversation_name=str(name),
            date_text=date_text,
            message_count=len(messages),
            summary_md=summary,
            model_name=str(getattr(self.config.model, "model", "") or ""),
        )
        return {"messages": len(messages), "summary": summary}
