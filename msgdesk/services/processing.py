"""Model-backed message processing."""

from __future__ import annotations

import json
from datetime import timedelta
from typing import Any

from .. import __version__
from ..config import AppConfig
from ..errors import AppError
from ..repository import Repository
from ..timeutil import iso_utc, local_date_of, utc_now
from .model import ModelInput, ModelProvider
from .profile_prompt import profile_prompt
from .scheduler import digest_due_at

PRIORITY_RANK = {"low": 0, "normal": 1, "high": 2, "urgent": 3}


class ProcessingService:
    def __init__(self, config: AppConfig, repo: Repository, model: ModelProvider):
        self.config = config
        self.repo = repo
        self.model = model

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
        self.repo.update_message_status(message_id, "processing")
        model_input = ModelInput(
            text=text,
            source_name=message.get("source_name") or source["name"],
            source_type=message.get("source_type") or source["type"],
            conversation_name=message.get("conversation_name"),
            published_at=message["published_at"],
            source_url=message.get("source_url"),
        )

        calls_today = self.repo.model_calls_today(
            iso_utc(utc_now() - timedelta(days=1))
        )
        if calls_today >= self.config.model.daily_call_limit:
            return self._fail(
                message_id,
                AppError("MODEL_OR_ADAPTER_BUSY", "今日模型调用次数已达上限"),
                raise_on_model_error,
            )

        try:
            result = self.model.analyze(
                model_input, {"profile": profile_prompt(self.config.profile)}
            )
            self.repo.record_model_call(
                message_id=message_id,
                provider=self.model.name,
                model_name=self.model.model_name,
                input_chars=len(text),
                output_chars=len(json.dumps(result, ensure_ascii=False)),
                duration_ms=int(result.pop("_duration_ms", 0)),
                success=True,
            )
        except AppError as exc:
            self.repo.record_model_call(
                message_id=message_id,
                provider=self.model.name,
                model_name=self.model.model_name,
                input_chars=len(text),
                output_chars=0,
                duration_ms=0,
                success=False,
                error_code=exc.code,
            )
            return self._fail(message_id, exc, raise_on_model_error)
        except Exception as exc:  # noqa: BLE001 - preserve the raw message
            return self._fail(
                message_id,
                AppError("REMOTE_TEMPORARY_ERROR", f"模型调用异常: {type(exc).__name__}"),
                raise_on_model_error,
            )

        merged = dict(result)
        merged["status"] = "success"
        merged["error_code"] = None
        merged["processor_version"] = __version__
        merged["model_provider"] = self.model.name
        merged["model_name"] = self.model.model_name
        if source.get("default_category"):
            merged["category"] = source["default_category"]

        source_priority = source.get("default_priority") or "normal"
        if PRIORITY_RANK.get(source_priority, 1) > PRIORITY_RANK.get(
            merged.get("priority", "normal"), 1
        ):
            merged["priority"] = source_priority

        self.repo.save_processing(message_id, merged)
        date_text = local_date_of(message["collected_at"], self.config.system.timezone)
        if (
            digest_due_at(self.config, date_text) is not None
            and not self.repo.has_active_digest_job(date_text)
        ):
            self.repo.enqueue_job(
                "generate_digest",
                payload={"date": date_text},
                priority=150,
            )
        return {
            "message_id": message_id,
            "status": "success",
            "priority": merged["priority"],
            "category": merged["category"],
            "summary_short": merged.get("summary_short"),
            "error_code": None,
        }

    def _fail(
        self,
        message_id: str,
        error: AppError,
        raise_on_model_error: bool,
    ) -> dict[str, Any]:
        self.repo.update_message_status(message_id, "failed")
        if raise_on_model_error:
            raise error
        return {
            "message_id": message_id,
            "status": "failed",
            "error_code": error.code,
            "error_message": error.message,
        }
