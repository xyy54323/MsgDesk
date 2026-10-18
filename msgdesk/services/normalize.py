"""Message normalization and fingerprints."""

from __future__ import annotations

import hashlib
import re
import unicodedata
from typing import Any

ZERO_WIDTH = dict.fromkeys(map(ord, "\u200b\u200c\u200d\ufeff"), None)
SPACE_RE = re.compile(r"[ \t\u00a0]+")
NEWLINE_RE = re.compile(r"\n{3,}")


def clean_text(value: str | None) -> str:
    if not value:
        return ""
    text = unicodedata.normalize("NFC", value).translate(ZERO_WIDTH)
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    text = SPACE_RE.sub(" ", text)
    lines = [line.strip() for line in text.split("\n")]
    text = "\n".join(lines)
    return NEWLINE_RE.sub("\n\n", text).strip()


def content_hash(text: str) -> str:
    normalized = clean_text(text).lower()
    normalized = re.sub(r"\s+", " ", normalized)
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()


def infer_content_type(raw: dict[str, Any]) -> str:
    explicit = raw.get("content_type")
    if explicit:
        return explicit
    attachments = raw.get("attachments") or []
    text = raw.get("content_text") or ""
    if attachments and text:
        return "mixed"
    if attachments:
        return "file"
    if raw.get("source_url") and text:
        return "link"
    return "text"

