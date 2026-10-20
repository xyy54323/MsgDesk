"""Pluggable model providers with a deterministic local fallback."""

from __future__ import annotations

import json
import os
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from typing import Any

from ..config import ModelConfig
from ..errors import AppError
from ..timeutil import iso_utc, parse_datetime

CATEGORY_KEYWORDS: dict[str, list[str]] = {
    "学校通知": ["学校", "老师", "班主任", "家长", "班级", "教务", "辅导员", "学院", "课程安排",
               "作业", "考试", "报名", "讲座", "校历", "开学", "选课", "家长会"],
    "学习": ["学习", "复习", "笔记", "论文", "阅读", "教材", "习题", "知识点", "研究方向"],
    "技术": ["ai", "人工智能", "模型", "开源", "python", "代码", "api", "数据库", "算法",
           "llm", "github", "框架", "编程", "论文", "sqlite", "检索", "索引", "分词",
           "知识库"],
    "项目": ["项目", "需求", "上线", "发布", "里程碑", "评审", "迭代", "交付", "bug", "版本"],
    "行业": ["融资", "市场", "行业", "公司", "营收", "产品发布", "政策", "趋势"],
    "生活": ["快递", "天气", "吃饭", "活动", "聚会", "体检", "缴费", "购物", "电影",
           "社区", "周末", "读书"],
    "待办": ["待办", "记得", "务必", "尽快", "截止", "提交", "完成", "处理", "回复"],
}

@dataclass
class ModelInput:
    text: str
    source_name: str
    source_type: str
    conversation_name: str | None
    published_at: str
    source_url: str | None


class ModelProvider:
    name = "base"
    model_name = ""

    def analyze(self, message: ModelInput, context: dict[str, Any]) -> dict[str, Any]:
        raise NotImplementedError

    def classify(self, message: ModelInput, context: dict[str, Any]) -> dict[str, Any]:
        result = self.analyze(message, context)
        return {"category": result.get("category"), "tags": result.get("tags", [])}

    def summarize(self, message: ModelInput, context: dict[str, Any]) -> dict[str, Any]:
        result = self.analyze(message, context)
        return {
            "summary_short": result.get("summary_short"),
            "summary_long": result.get("summary_long"),
        }

    def extract_actions(self, message: ModelInput, context: dict[str, Any]) -> dict[str, Any]:
        result = self.analyze(message, context)
        return {
            "action_items": result.get("action_items", []),
            "deadline_at": result.get("deadline_at"),
        }


class UnconfiguredModel(ModelProvider):
    """Provider used while the user has not configured a remote model."""

    name = "unconfigured"
    model_name = ""

    def analyze(self, message: ModelInput, context: dict[str, Any]) -> dict[str, Any]:
        raise AppError("CONFIG_INVALID", "尚未配置模型，请先添加模型服务商")


class OpenAICompatibleModel(ModelProvider):
    name = "openai_compatible"

    def __init__(self, config: ModelConfig):
        self.config = config
        self.model_name = config.model
        self.api_key = config.api_key or os.environ.get(config.api_key_env, "")

    def analyze(self, message: ModelInput, context: dict[str, Any]) -> dict[str, Any]:
        if not self.config.base_url or not self.config.model:
            raise AppError("CONFIG_INVALID", "模型 base_url 或 model 未配置")
        if not self.api_key:
            raise AppError(
                "AUTH_REQUIRED",
                f"模型密钥未配置（可保存到本机配置或设置环境变量 {self.config.api_key_env}）",
            )
        prompt = (
            "你是个人信息整理助手。只输出 JSON 对象，字段为 category, subcategory, tags, "
            "summary_short, summary_long, facts, action_items, confidence。"
            "category 从 [学习,学校通知,技术,项目,行业,生活,待办,其他] 选择；"
            "summary_short 用一句话概括；action_items 是字符串数组。"
        )
        profile = str(context.get("profile") or "").strip()
        if profile:
            prompt += "\n\n" + profile
        text = message.text[: self.config.max_input_chars]
        payload = {
            "model": self.config.model,
            "messages": [
                {"role": "system", "content": prompt},
                {
                    "role": "user",
                    "content": (
                        f"来源：{message.source_name}\n会话：{message.conversation_name or ''}\n"
                        f"发布时间：{message.published_at}\n链接：{message.source_url or ''}\n\n{text}"
                    ),
                },
            ],
            "temperature": 0.1,
            "response_format": {"type": "json_object"},
        }
        request = urllib.request.Request(
            self.config.base_url.rstrip("/") + "/chat/completions",
            data=json.dumps(payload).encode("utf-8"),
            headers={
                "Content-Type": "application/json",
                "Authorization": f"Bearer {self.api_key}",
            },
            method="POST",
        )
        started = time.monotonic()
        try:
            with urllib.request.urlopen(request, timeout=self.config.timeout_seconds) as response:
                body = json.loads(response.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            code = "RATE_LIMITED" if exc.code == 429 else "REMOTE_TEMPORARY_ERROR"
            if exc.code in (401, 403):
                code = "AUTH_EXPIRED"
            raise AppError(code, f"模型请求失败: HTTP {exc.code}") from exc
        except TimeoutError as exc:
            raise AppError("NETWORK_TIMEOUT", "模型请求超时") from exc
        except urllib.error.URLError as exc:
            raise AppError("NETWORK_UNAVAILABLE", "模型服务不可达") from exc
        except json.JSONDecodeError as exc:
            raise AppError("SCHEMA_INVALID", "模型返回不是 JSON") from exc
        elapsed = int((time.monotonic() - started) * 1000)
        content = body.get("choices", [{}])[0].get("message", {}).get("content", "")
        try:
            result = json.loads(content)
        except json.JSONDecodeError as exc:
            raise AppError("SCHEMA_INVALID", "模型内容不是 JSON 对象") from exc
        result["_duration_ms"] = elapsed
        return validate_model_output(result)


def validate_model_output(result: dict[str, Any]) -> dict[str, Any]:
    if not isinstance(result, dict):
        raise AppError("SCHEMA_INVALID", "模型输出不是对象")
    allowed_categories = set(CATEGORY_KEYWORDS) | {"其他"}
    category = result.get("category") if result.get("category") in allowed_categories else "其他"
    priority = result.get("priority")
    priority = priority if priority in ("urgent", "high", "normal", "low") else "normal"
    urgency = result.get("urgency")
    urgency = urgency if urgency in ("urgent", "normal", "low") else "normal"
    tags = result.get("tags") if isinstance(result.get("tags"), list) else []
    actions = result.get("action_items") if isinstance(result.get("action_items"), list) else []
    facts = result.get("facts") if isinstance(result.get("facts"), list) else []
    confidence = result.get("confidence")
    try:
        confidence = max(0.0, min(1.0, float(confidence))) if confidence is not None else None
    except (TypeError, ValueError):
        confidence = None
    deadline = result.get("deadline_at")
    if deadline and not parse_datetime(str(deadline)):
        deadline = None
    return {
        "category": category,
        "subcategory": result.get("subcategory"),
        "tags": [str(t) for t in tags][:12],
        "summary_short": str(result.get("summary_short") or "")[:300] or None,
        "summary_long": str(result.get("summary_long") or "")[:3000] or None,
        "facts": [str(f) for f in facts][:20],
        "action_items": [str(a) for a in actions][:20],
        "deadline_at": iso_utc(parse_datetime(str(deadline))) if deadline else None,
        "priority": priority,
        "urgency": urgency,
        "confidence": confidence,
    }


def create_model_provider(config: ModelConfig, zone_name: str = "Asia/Shanghai") -> ModelProvider:
    if config.provider in ("openai_compatible", "openai"):
        return OpenAICompatibleModel(config)
    if config.provider in ("", "rules"):
        return UnconfiguredModel()
    raise AppError("CONFIG_INVALID", f"不支持的模型服务商: {config.provider}")


_VISION_CACHE: dict[str, bool] = {}

_PIXEL_PNG = (
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8"
    "z8BQDwAEhQGAhKmMIQAAAABJRU5ErkJggg=="
)


def probe_vision(config: ModelConfig) -> bool:
    """Ask the provider whether it accepts image parts; cached per endpoint."""
    key = f"{config.base_url}|{config.model}"
    if key in _VISION_CACHE:
        return _VISION_CACHE[key]
    payload = {
        "model": config.model,
        "messages": [
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": "这张图是什么颜色？只回一个字。"},
                    {
                        "type": "image_url",
                        "image_url": {"url": f"data:image/png;base64,{_PIXEL_PNG}"},
                    },
                ],
            }
        ],
        "max_tokens": 8,
    }
    api_key = config.api_key or os.environ.get(config.api_key_env, "")
    request = urllib.request.Request(
        config.base_url.rstrip("/") + "/chat/completions",
        data=json.dumps(payload).encode("utf-8"),
        headers={
            "Content-Type": "application/json",
            "Authorization": f"Bearer {api_key}",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=config.timeout_seconds) as response:
            response.read()
        supported = True
    except urllib.error.HTTPError as exc:
        supported = exc.code not in (400, 404, 415, 422)
    except Exception:  # noqa: BLE001 - treat unknown as unsupported
        supported = False
    _VISION_CACHE[key] = supported
    return supported


def chat_text(config: ModelConfig, system: str, user: str) -> str:
    """Plain chat completion for summarisation (no JSON schema)."""
    api_key = config.api_key or os.environ.get(config.api_key_env, "")
    if not api_key:
        raise AppError("AUTH_REQUIRED", "模型密钥未配置，无法生成群聊摘要")
    if not config.base_url or not config.model:
        raise AppError("CONFIG_INVALID", "模型 base_url 或 model 未配置")
    payload = {
        "model": config.model,
        "messages": [
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ],
        "temperature": 0.2,
    }
    request = urllib.request.Request(
        config.base_url.rstrip("/") + "/chat/completions",
        data=json.dumps(payload).encode("utf-8"),
        headers={
            "Content-Type": "application/json",
            "Authorization": f"Bearer {api_key}",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=config.timeout_seconds) as response:
            body = json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        code = "AUTH_EXPIRED" if exc.code in (401, 403) else "REMOTE_TEMPORARY_ERROR"
        raise AppError(code, f"模型调用失败 HTTP {exc.code}") from exc
    except TimeoutError as exc:
        raise AppError("NETWORK_TIMEOUT", "群聊摘要生成超时") from exc
    except urllib.error.URLError as exc:
        raise AppError("NETWORK_UNAVAILABLE", "模型接口不可达") from exc
    choices = body.get("choices") or []
    if not choices:
        raise AppError("SCHEMA_INVALID", "模型未返回内容")
    return str((choices[0].get("message") or {}).get("content") or "").strip()

