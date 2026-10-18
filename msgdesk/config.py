"""Configuration loading and path resolution."""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .errors import AppError

PROJECT_ROOT = Path(__file__).resolve().parent.parent


@dataclass
class SystemConfig:
    timezone: str = "Asia/Shanghai"
    data_dir: str = "./data"
    digest_dir: str = "./digests"
    bind_host: str = "127.0.0.1"
    port: int = 8080
    log_level: str = "INFO"
    api_token: str | None = None
    require_auth: bool = True


@dataclass
class QuietHours:
    start: str = "23:00"
    end: str = "07:00"
    allow_urgent: bool = True


@dataclass
class SchedulingConfig:
    default_interval_minutes: int = 10
    max_parallel_jobs: int = 3
    digest_time: str = "22:00"
    poll_seconds: float = 2.0
    quiet_hours: QuietHours = field(default_factory=QuietHours)


@dataclass
class ModelProviderConfig:
    """A user-configured remote model endpoint.

    This is deliberately separate from the active ``ModelConfig`` fields so
    the workbench can maintain several providers without inventing a default
    one.  ``provider`` identifies the protocol implementation, not a fake
    local model.
    """

    id: str = ""
    display_name: str = ""
    note: str = ""
    official_url: str = ""
    provider: str = "openai_compatible"
    protocol: str = "chat_completions"
    base_url: str = ""
    model: str = ""
    model_list: str = ""
    api_key_env: str = "MSGDESK_MODEL_API_KEY"
    api_key: str = ""
    timeout_seconds: int = 60
    max_input_chars: int = 6000
    daily_call_limit: int = 500


@dataclass
class ModelConfig(ModelProviderConfig):
    """Active model settings plus the persisted provider collection."""

    id: str = ""
    provider: str = ""
    protocol: str = ""
    providers: list[ModelProviderConfig] = field(default_factory=list)
    default_provider_id: str = ""


@dataclass
class NotificationChannelConfig:
    id: str
    type: str
    enabled: bool = True
    config: dict[str, Any] = field(default_factory=dict)


@dataclass
class NotificationConfig:
    channels: list[NotificationChannelConfig] = field(default_factory=list)
    min_priority: str = "high"
    hourly_limit: int = 6


@dataclass
class AppConfig:
    system: SystemConfig = field(default_factory=SystemConfig)
    scheduling: SchedulingConfig = field(default_factory=SchedulingConfig)
    model: ModelConfig = field(default_factory=ModelConfig)
    notifications: NotificationConfig = field(default_factory=NotificationConfig)
    config_path: Path | None = None
    base_dir: Path = PROJECT_ROOT

    @property
    def data_dir(self) -> Path:
        return self._resolve(self.system.data_dir)

    @property
    def digest_dir(self) -> Path:
        return self._resolve(self.system.digest_dir)

    @property
    def db_path(self) -> Path:
        return self.data_dir / "messages.db"

    def _resolve(self, value: str) -> Path:
        path = Path(value)
        if not path.is_absolute():
            path = self.base_dir / path
        return path

    def ensure_dirs(self) -> None:
        for path in (
            self.data_dir,
            self.data_dir / "raw",
            self.data_dir / "logs",
            self.data_dir / "notifications",
            self.digest_dir,
        ):
            path.mkdir(parents=True, exist_ok=True)

    def public_dict(self) -> dict[str, Any]:
        model_public = _public_model_config(self.model)
        return {
            "system": {
                "timezone": self.system.timezone,
                "data_dir": str(self.data_dir),
                "digest_dir": str(self.digest_dir),
                "bind_host": self.system.bind_host,
                "port": self.system.port,
                "require_auth": self.system.require_auth,
            },
            "scheduling": {
                "default_interval_minutes": self.scheduling.default_interval_minutes,
                "max_parallel_jobs": self.scheduling.max_parallel_jobs,
                "digest_time": self.scheduling.digest_time,
                "quiet_hours": {
                    "start": self.scheduling.quiet_hours.start,
                    "end": self.scheduling.quiet_hours.end,
                    "allow_urgent": self.scheduling.quiet_hours.allow_urgent,
                },
            },
            "model": model_public,
            "notifications": {
                "min_priority": self.notifications.min_priority,
                "hourly_limit": self.notifications.hourly_limit,
                "channels": [
                    {
                        "id": c.id,
                        "type": c.type,
                        "enabled": c.enabled,
                        "config": _redact(c.config),
                    }
                    for c in self.notifications.channels
                ],
            },
        }


def _public_model_provider(provider: ModelProviderConfig) -> dict[str, Any]:
    """Return provider metadata without exposing API credentials."""
    return {
        "id": provider.id,
        "display_name": provider.display_name,
        "note": provider.note,
        "official_url": provider.official_url,
        "provider": provider.provider,
        "protocol": provider.protocol,
        "base_url": provider.base_url,
        "model": provider.model,
        "model_list": provider.model_list,
        "api_key_env": provider.api_key_env,
        "api_key_set": bool(provider.api_key or os.environ.get(provider.api_key_env)),
        "timeout_seconds": provider.timeout_seconds,
        "max_input_chars": provider.max_input_chars,
        "daily_call_limit": provider.daily_call_limit,
    }


def _public_model_config(model: ModelConfig) -> dict[str, Any]:
    result = _public_model_provider(model)
    result.update(
        {
            "providers": [_public_model_provider(item) for item in model.providers],
            "default_provider_id": model.default_provider_id,
        }
    )
    return result


def model_provider_dict(provider: ModelProviderConfig) -> dict[str, Any]:
    """Serialize a provider for persistence, including its local secret."""
    return {
        "id": provider.id,
        "display_name": provider.display_name,
        "note": provider.note,
        "official_url": provider.official_url,
        "provider": provider.provider,
        "protocol": provider.protocol,
        "base_url": provider.base_url,
        "model": provider.model,
        "model_list": provider.model_list,
        "api_key_env": provider.api_key_env,
        "api_key": provider.api_key,
        "timeout_seconds": provider.timeout_seconds,
        "max_input_chars": provider.max_input_chars,
        "daily_call_limit": provider.daily_call_limit,
    }


def model_config_from_dict(value: dict[str, Any], *, provider_id: str | None = None) -> ModelProviderConfig:
    """Build a provider from persisted/user supplied JSON."""
    provider = ModelProviderConfig()
    if provider_id is not None:
        provider.id = provider_id
    _merge_dataclass(provider, value)
    return provider


def activate_model_provider(model: ModelConfig, provider: ModelProviderConfig | None) -> None:
    """Copy a selected provider into the active model fields."""
    if provider is None:
        model.id = ""
        model.provider = ""
        model.display_name = ""
        model.note = ""
        model.official_url = ""
        model.protocol = ""
        model.base_url = ""
        model.model = ""
        model.model_list = ""
        model.api_key_env = "MSGDESK_MODEL_API_KEY"
        model.api_key = ""
        model.timeout_seconds = 60
        model.max_input_chars = 6000
        model.daily_call_limit = 500
        return
    for key in (
        "id", "display_name", "note", "official_url", "provider", "protocol",
        "base_url", "model", "model_list", "api_key_env", "api_key",
        "timeout_seconds", "max_input_chars", "daily_call_limit",
    ):
        setattr(model, key, getattr(provider, key))


SECRET_KEYS = {
    "token",
    "access_token",
    "cookie",
    "api_key",
    "password",
    "secret",
    "sendkey",
    "device_key",
    "webhook_url",
    "url",
    "endpoint",
}


def redact_secrets(value: dict[str, Any], *, hide_url: bool = True) -> dict[str, Any]:
    """Mask credential-bearing fields before logs or API responses."""
    hidden = SECRET_KEYS if hide_url else SECRET_KEYS - {"url", "endpoint"}
    return {
        key: ("***" if key.lower() in hidden and item else item)
        for key, item in value.items()
    }


def _redact(value: dict[str, Any]) -> dict[str, Any]:
    return redact_secrets(value)


def _deep_merge(target: dict[str, Any], updates: dict[str, Any]) -> None:
    for key, value in updates.items():
        if (
            isinstance(value, dict)
            and isinstance(target.get(key), dict)
        ):
            _deep_merge(target[key], value)
        else:
            target[key] = value


def merge_config_file(path: Path, updates: dict[str, Any]) -> None:
    """Merge updates into config.json using an atomic replacement.

    Callers are responsible for passing only fields that may be persisted.
    Credentials are never echoed in errors raised here.
    """
    raw: dict[str, Any] = {}
    if path.exists():
        try:
            value = json.loads(path.read_text(encoding="utf-8"))
        except json.JSONDecodeError as exc:
            raise AppError(
                "CONFIG_INVALID", "配置文件不是有效 JSON，无法保存设置"
            ) from exc
        if isinstance(value, dict):
            raw = value
    _deep_merge(raw, updates)
    temp = path.with_suffix(path.suffix + ".tmp")
    temp.write_text(
        json.dumps(raw, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    try:
        temp.chmod(0o600)
    except OSError:
        pass
    os.replace(temp, path)


def _merge_dataclass(instance: Any, data: dict[str, Any]) -> Any:
    for key, value in data.items():
        if not hasattr(instance, key):
            continue
        current = getattr(instance, key)
        if isinstance(current, QuietHours) and isinstance(value, dict):
            _merge_dataclass(current, value)
        elif isinstance(current, list):
            continue
        else:
            setattr(instance, key, value)
    return instance


def load_config(path: str | Path | None = None) -> AppConfig:
    config = AppConfig()
    if path is None:
        candidate = PROJECT_ROOT / "config.json"
        path = candidate if candidate.exists() else None
    if path is not None:
        config_path = Path(path).resolve()
        if not config_path.exists():
            raise AppError("CONFIG_INVALID", f"配置文件不存在: {config_path}")
        try:
            raw = json.loads(config_path.read_text(encoding="utf-8"))
        except json.JSONDecodeError as exc:
            raise AppError(
                "CONFIG_INVALID", f"配置文件不是有效 JSON: {config_path}", {"error": str(exc)}
            ) from exc
        config.config_path = config_path
        config.base_dir = config_path.parent
        _merge_dataclass(config.system, raw.get("system", {}))
        _merge_dataclass(config.scheduling, raw.get("scheduling", {}))
        quiet = raw.get("scheduling", {}).get("quiet_hours")
        if isinstance(quiet, dict):
            _merge_dataclass(config.scheduling.quiet_hours, quiet)
        raw_model = raw.get("model", {})
        if not isinstance(raw_model, dict):
            raw_model = {}
        # New format stores only real, user-configured providers.  The old
        # starter config used provider=rules/local-heuristics-v1; that is an
        # implementation fallback and must never appear as a configured
        # provider in the workbench.
        provider_rows = raw_model.get("providers")
        if isinstance(provider_rows, list):
            config.model.providers = [
                model_config_from_dict(item)
                for item in provider_rows
                if isinstance(item, dict) and str(item.get("id", "")).strip()
            ]
            config.model.default_provider_id = str(
                raw_model.get("default_provider_id", "") or ""
            ).strip()
            selected = next(
                (item for item in config.model.providers
                 if item.id == config.model.default_provider_id),
                None,
            )
            if selected is None and config.model.providers:
                selected = config.model.providers[0]
                config.model.default_provider_id = selected.id
            activate_model_provider(config.model, selected)
        else:
            # Compatibility with the previous single-provider file format.
            legacy_provider = str(raw_model.get("provider", "") or "").strip()
            legacy_base_url = str(raw_model.get("base_url", "") or "").strip()
            legacy_model = str(raw_model.get("model", "") or "").strip()
            if legacy_provider in ("openai", "openai_compatible") and legacy_base_url and legacy_model:
                legacy = model_config_from_dict(raw_model, provider_id="legacy")
                config.model.providers = [legacy]
                config.model.default_provider_id = legacy.id
                activate_model_provider(config.model, legacy)
            else:
                config.model.providers = []
                config.model.default_provider_id = ""
                activate_model_provider(config.model, None)
        _merge_dataclass(config.notifications, raw.get("notifications", {}))
        channels = raw.get("notifications", {}).get("channels")
        if isinstance(channels, list):
            config.notifications.channels = [
                NotificationChannelConfig(
                    id=item.get("id", ""),
                    type=item.get("type", ""),
                    enabled=bool(item.get("enabled", True)),
                    config=item.get("config", {}) or {},
                )
                for item in channels
                if item.get("id") and item.get("type")
            ]

    env_token = os.environ.get("MSGDESK_API_TOKEN")
    if env_token:
        config.system.api_token = env_token
    config.ensure_dirs()
    return config

