"""Stable application error codes shared by services and the API."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


RETRYABLE_CODES = {
    "NETWORK_TIMEOUT",
    "NETWORK_UNAVAILABLE",
    "RATE_LIMITED",
    "REMOTE_TEMPORARY_ERROR",
    "MODEL_OR_ADAPTER_BUSY",
}

USER_ACTION_CODES = {
    "AUTH_REQUIRED",
    "AUTH_EXPIRED",
    "PERMISSION_DENIED",
    "CLIENT_NOT_RUNNING",
    "CLIENT_VERSION_UNSUPPORTED",
    "CONFIG_INVALID",
    "DEPENDENCY_MISSING",
}

NON_RETRYABLE_CODES = {
    "SCHEMA_INVALID",
    "CONTENT_PARSE_FAILED",
    "SOURCE_REMOVED",
    "DATA_CORRUPTED",
    "NOT_FOUND",
    "VALIDATION_ERROR",
    "INTERNAL_ERROR",
}


@dataclass
class AppError(Exception):
    code: str
    message: str
    details: dict[str, Any] = field(default_factory=dict)
    trace_id: str | None = None

    @property
    def retryable(self) -> bool:
        return self.code in RETRYABLE_CODES

    @property
    def needs_user_action(self) -> bool:
        return self.code in USER_ACTION_CODES

    def to_dict(self) -> dict[str, Any]:
        return {
            "code": self.code,
            "message": self.message,
            "details": self.details,
            "trace_id": self.trace_id,
        }

    def __str__(self) -> str:
        return f"{self.code}: {self.message}"

