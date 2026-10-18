"""Rule + model processing pipeline with a persisted fallback."""

from __future__ import annotations

import json
from datetime import timedelta
from typing import Any

from ..config import AppConfig
from ..errors import AppError
from ..repository import Repository, loads
from ..timeutil import iso_utc, local_date_of, utc_now
from .model import ModelInput, ModelProvider, RuleBasedModel
from .rules import RuleEngine

PRIORITY_RANK = {"low": 0, "normal": 1, "high": 2, "urgent": 3}


class ProcessingService:
    def __init__(
        self,
        config: AppConfig,
        repo: Repository,
        model: ModelProvider,
        fallback: RuleBasedModel | None = None,
    ):
        self.config = config
        self.repo = repo
        self.model = model
        self.fallback = fallback or RuleBasedModel(config.system.timezone)

    def process(
        self,
        message_id: str,
        *,
        force: bool = False,
        raise_on_model_error: bool = False,
    ) -> dict[str, Any]:
        message = self.repo.get_message(message_id)
        if message is None:
            raise AppError("NOT_FOUND", f"消息不存在: {message_id}")
        if message.get("processing_status") == "ignored" and not force:
            return {"message_id": message_id, "status": "ignored"}
        source = self.repo.get_source(message["source_id"])
        if source is None:
            raise AppError("NOT_FOUND", "消息来源已删除")

        text = message.get("content_text") or ""
        rules = RuleEngine(self.repo.list_rules(enabled_only=True)).apply(
            source_id=message["source_id"],
            content_type=message.get("content_type") or "text",
            text=text,
        )
        if rules["ignore"]:
            self.repo.update_message_status(message_id, "ignored")
            return {"message_id": message_id, "status": "ignored", "rules": rules["matched"]}

        self.repo.update_message_status(message_id, "processing")
        model_input = ModelInput(
            text=text,
            source_name=message.get("source_name") or source["name"],
            source_type=message.get("source_type") or source["type"],
            conversation_name=message.get("conversation_name"),
            published_at=message["published_at"],
            source_url=message.get("source_url"),
        )
        source_config = loads(source.get("config_json"), {}) or {}
        allow_remote = bool(source_config.get("allow_remote_model", True))
        provider = self.model if allow_remote else self.fallback
        model_result: dict[str, Any] | None = None
        error: AppError | None = None

        if provider.name != "rules":
            calls_today = self.repo.model_calls_today(
                iso_utc(utc_now() - timedelta(days=1))
            )
            if calls_today >= self.config.model.daily_call_limit:
                error = AppError("MODEL_OR_ADAPTER_BUSY", "今日模型调用次数已达上限")
            else:
                try:
                    self.repo.add_trace(
                        "prompt",
                        f"→ {provider.model_name} 处理消息",
                        f"[source] {message.get('source_name') or ''}\n\n{text[:4000]}",
                    )
                    model_result = provider.analyze(model_input, {"rules": rules})
                    self.repo.add_trace(
                        "reply",
                        f"← {provider.model_name}",
                        json.dumps(
                            {k: v for k, v in model_result.items() if not k.startswith("_")},
                            ensure_ascii=False, indent=2,
                        )[:4000],
                    )
                    self.repo.record_model_call(
                        message_id=message_id,
                        provider=provider.name,
                        model_name=provider.model_name,
                        input_chars=len(text),
                        output_chars=len(json.dumps(model_result, ensure_ascii=False)),
                        duration_ms=int(model_result.pop("_duration_ms", 0)),
                        success=True,
                    )
                except AppError as exc:
                    error = exc
                    self.repo.record_model_call(
                        message_id=message_id,
                        provider=provider.name,
                        model_name=provider.model_name,
                        input_chars=len(text),
                        output_chars=0,
                        duration_ms=0,
                        success=False,
                        error_code=exc.code,
                    )
                except Exception as exc:  # never lose the raw message
                    error = AppError("REMOTE_TEMPORARY_ERROR", f"模型调用异常: {type(exc).__name__}")
        if model_result is None:
            model_result = self.fallback.analyze(model_input, {"rules": rules})

        merged = dict(model_result)
        merged["status"] = "fallback" if error else "success"
        merged["error_code"] = error.code if error else None
        merged["processor_version"] = "0.1.0"
        merged["rule_version"] = str(len(rules["matched"]))
        merged["model_provider"] = provider.name
        merged["model_name"] = getattr(provider, "model_name", "")
        merged["tags"] = list(dict.fromkeys(
            list(model_result.get("tags", [])) + list(rules["tags"])
        ))
        if rules["category"]:
            merged["category"] = rules["category"]
        # The source's configured category is authoritative so the工作台 shows
        # what the user actually set instead of a model-invented bucket.
        if source.get("default_category"):
            merged["category"] = source["default_category"]

        priority = model_result.get("priority", "normal")
        if rules["priority"]:
            priority = rules["priority"]
        source_priority = source.get("default_priority") or "normal"
        if PRIORITY_RANK.get(source_priority, 1) > PRIORITY_RANK.get(priority, 1):
            priority = source_priority
        merged["priority"] = priority
        if rules["urgency"]:
            merged["urgency"] = rules["urgency"]
        if merged.get("urgency") == "urgent":
            merged["priority"] = "urgent"

        self.repo.save_processing(message_id, merged)
        date_text = local_date_of(message["collected_at"], self.config.system.timezone)
        if not self.repo.has_active_digest_job(date_text):
            self.repo.enqueue_job(
                "generate_digest",
                payload={"date": date_text},
                priority=150,
            )

        should_notify = rules["notify"]
        if should_notify is None:
            should_notify = PRIORITY_RANK.get(merged["priority"], 1) >= PRIORITY_RANK.get(
                self.config.notifications.min_priority, 2
            )
        if should_notify:
            for channel in self.config.notifications.channels:
                if not channel.enabled:
                    continue
                self.repo.enqueue_job(
                    "send_notification",
                    message_id=message_id,
                    payload={"channel": channel.id},
                    priority=50 if merged.get("urgency") == "urgent" else 120,
                )

        if error and raise_on_model_error:
            raise error
        return {
            "message_id": message_id,
            "status": merged["status"],
            "priority": merged["priority"],
            "category": merged["category"],
            "summary_short": merged.get("summary_short"),
            "error_code": merged.get("error_code"),
            "matched_rules": rules["matched"],
        }

