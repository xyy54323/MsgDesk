"""Adapter registry keeps platform details out of the core pipeline."""

from __future__ import annotations

from typing import Any

from ..errors import AppError
from ..models import AdapterCapabilities
from .base import Fetcher, SourceAdapter
from .qq_onebot import QQOneBotAdapter
from .web import WebAdapter


class AIWebAdapter(WebAdapter):
    """Same validation as a web page, executed by the model-driven agent."""

    adapter_type = "ai_web"


ADAPTERS: dict[str, type] = {
    "web": WebAdapter,
    "ai_web": AIWebAdapter,
    "qq_onebot": QQOneBotAdapter,
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

