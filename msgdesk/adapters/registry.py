"""Adapter registry keeps platform details out of the core pipeline."""

from __future__ import annotations

from typing import Any

from ..errors import AppError
from ..models import AdapterCapabilities
from .base import Fetcher, SourceAdapter
from .chat_import import ChatImportAdapter
from .mock_chat import MockChatAdapter, PendingChatAdapter
from .qq_onebot import QQOneBotAdapter
from .rss import RSSAdapter
from .web import WebAdapter


class AIWebAdapter(WebAdapter):
    """Same validation as a web page, executed by the model-driven agent."""

    adapter_type = "ai_web"


def _pending(platform: str):
    return type(
        f"Pending{platform.title()}Adapter",
        (PendingChatAdapter,),
        {"adapter_type": platform, "platform": platform},
    )


ADAPTERS: dict[str, type] = {
    "rss": RSSAdapter,
    "web": WebAdapter,
    "ai_web": AIWebAdapter,
    "mock_chat": MockChatAdapter,
    "qq_onebot": QQOneBotAdapter,
    "chat_import": ChatImportAdapter,
    "qq": _pending("qq"),
    "wechat": _pending("wechat"),
    "wecom": _pending("wecom"),
}


def create_adapter(adapter_type: str, fetcher: Fetcher | None = None) -> SourceAdapter:
    if ":" in adapter_type:
        adapter_type = adapter_type.split(":", 1)[0]
    cls = ADAPTERS.get(adapter_type)
    if cls is None:
        raise AppError("CONFIG_INVALID", f"未知适配器类型: {adapter_type}")
    return cls(fetcher=fetcher)


def list_adapter_capabilities() -> list[dict[str, Any]]:
    result = []
    for name in sorted(ADAPTERS):
        adapter = ADAPTERS[name]()
        caps: AdapterCapabilities = adapter.get_capabilities()
        result.append(caps.to_dict())
    return result

