"""Render the personal profile into a reusable prompt block."""

from __future__ import annotations

from typing import Any

FIELDS = (
    ("identity", "我是谁"),
    ("interests", "我关心"),
    ("ignore", "我不关心"),
    ("todo_rule", "什么算我的待办"),
    ("notes", "补充说明"),
)


def profile_prompt(profile: Any) -> str:
    if profile is None:
        return ""
    lines: list[str] = []
    for attr, label in FIELDS:
        value = str(getattr(profile, attr, "") or "").strip()
        if value:
            lines.append(f"- {label}：{value}")
    if not lines:
        return ""
    return (
        "个人画像（判断信息是否与我有关时优先参考）：\n"
        + "\n".join(lines)
        + "\n判断待办时：只有需要我本人行动的事才算待办；"
        "转发给别人、其他单位负责、纯通知或只是聊天，不算我的待办。"
    )
