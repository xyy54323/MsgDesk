"""RSS 2.0 and Atom reader with timestamp cursor and conditional requests."""

from __future__ import annotations

import hashlib
import re
import xml.etree.ElementTree as ET
from typing import Any
from urllib.parse import urljoin

from ..errors import AppError
from ..models import AdapterCapabilities, Cursor, FetchResult, RawMessage, ValidationResult
from ..timeutil import iso_utc, parse_datetime
from .base import BaseAdapter, FetchOptions, SourceContext

TAG_RE = re.compile(r"<[^>]+>")
SPACE_RE = re.compile(r"[ \t\r\f\v]+")


def strip_html(value: str | None) -> str:
    if not value:
        return ""
    text = re.sub(r"(?is)<(script|style).*?</\1>", " ", value)
    text = re.sub(r"(?i)<br\s*/?>", "\n", text)
    text = re.sub(r"(?i)</p>", "\n", text)
    text = TAG_RE.sub(" ", text)
    text = SPACE_RE.sub(" ", text)
    text = re.sub(r"\n\s*\n+", "\n\n", text)
    return text.strip()


class RSSAdapter(BaseAdapter):
    adapter_type = "rss"

    def get_capabilities(self) -> AdapterCapabilities:
        return AdapterCapabilities(
            adapter_type=self.adapter_type,
            adapter_version=self.adapter_version,
            supports_stable_message_id=True,
            supports_incremental_cursor=True,
        )

    def validate_config(self, config: dict[str, Any]) -> ValidationResult:
        url = str(config.get("url", "")).strip()
        if not url:
            return ValidationResult(False, "缺少 url 配置")
        if not url.startswith(("http://", "https://", "file://")):
            return ValidationResult(False, "url 必须是 http、https 或 file 地址")
        return ValidationResult(True, "配置有效")

    def fetch_incremental(
        self, source: SourceContext, cursor: Cursor, options: FetchOptions
    ) -> FetchResult:
        url = str(source.config.get("url", "")).strip()
        response = self.fetcher.fetch(
            url,
            etag=cursor.etag,
            last_modified=cursor.last_modified,
            timeout=options.timeout_seconds,
        )
        if response.status == 304:
            return FetchResult(
                next_cursor=cursor, stats={"fetched": 0, "skipped": 0, "failed": 0}
            )
        try:
            root = ET.fromstring(response.body)
        except ET.ParseError as exc:
            raise AppError("CONTENT_PARSE_FAILED", f"RSS 解析失败: {exc}") from exc

        feed_title = _first_text(root, ("channel/title", "title")) or source.name
        entries = _entries(root)
        cursor_time = parse_datetime(cursor.last_message_time)
        messages: list[RawMessage] = []
        latest_time: str | None = cursor.last_message_time
        skipped = 0
        max_items = max(1, min(int(source.config.get("max_items", options.max_items)), 200))

        parsed = []
        for item in entries:
            data = _parse_entry(item, feed_title, url)
            if data is None:
                skipped += 1
                continue
            parsed.append(data)
        parsed.sort(key=lambda m: m.published_at, reverse=True)

        for message in parsed[:max_items]:
            published = parse_datetime(message.published_at)
            if cursor_time and published and published < cursor_time:
                skipped += 1
                continue
            messages.append(message)
            if not latest_time or (published and parse_datetime(latest_time) and
                                   published > parse_datetime(latest_time)):
                latest_time = message.published_at

        next_cursor = Cursor(
            cursor_type="mixed",
            cursor_value=latest_time,
            last_message_time=latest_time,
            last_platform_message_id=messages[0].platform_message_id if messages else cursor.last_platform_message_id,
            etag=response.headers.get("etag") or cursor.etag,
            last_modified=response.headers.get("last-modified") or cursor.last_modified,
        )
        return FetchResult(
            messages=messages,
            next_cursor=next_cursor,
            stats={"fetched": len(messages), "skipped": skipped, "failed": 0},
        )


def _entries(root: ET.Element) -> list[ET.Element]:
    entries = [el for el in root.iter() if _local(el.tag) in ("item", "entry")]
    return entries


def _parse_entry(item: ET.Element, feed_title: str, feed_url: str) -> RawMessage | None:
    values: dict[str, str] = {}
    link = ""
    for child in list(item):
        name = _local(child.tag)
        text = (child.text or "").strip()
        if name == "link":
            href = child.attrib.get("href")
            rel = child.attrib.get("rel", "alternate")
            if href and rel in ("alternate", ""):
                link = href
            elif text and not link:
                link = text
        elif name in ("guid", "id") and not values.get("guid"):
            values["guid"] = text
        elif name in ("title", "pubDate", "published", "updated", "summary",
                      "description", "content", "encoded", "author", "creator"):
            if text and not values.get(name):
                values[name] = text
            elif name in ("content", "encoded") and text:
                values.setdefault("content_body", text)
    if not values.get("title") and not values.get("description") and not values.get("content_body"):
        return None
    published = (
        parse_datetime(values.get("published"))
        or parse_datetime(values.get("pubDate"))
        or parse_datetime(values.get("updated"))
        or parse_datetime(values.get("date"))
    )
    published_at = iso_utc(published) if published else iso_utc()
    title = strip_html(values.get("title", ""))
    body = strip_html(
        values.get("content_body")
        or values.get("content")
        or values.get("description")
        or values.get("summary")
        or ""
    )
    content = title
    if body and body not in title:
        content = f"{title}\n\n{body}" if title else body
    if not content:
        return None
    resolved_link = urljoin(feed_url, link) if link else feed_url
    identity = values.get("guid") or resolved_link or hashlib.sha256(
        (title + published_at).encode("utf-8")
    ).hexdigest()
    return RawMessage(
        platform_message_id=identity,
        conversation_id=feed_url,
        conversation_name=feed_title,
        conversation_kind="web",
        sender_name=strip_html(values.get("author") or values.get("creator") or ""),
        published_at=published_at,
        content_text=content,
        content_type="link" if resolved_link else "text",
        source_url=resolved_link,
        raw_payload={
            "title": title,
            "guid": values.get("guid"),
            "link": resolved_link,
            "published": published_at,
            "description": body,
        },
    )


def _local(tag: str) -> str:
    return tag.rsplit("}", 1)[-1].split(":")[-1]


def _first_text(root: ET.Element, paths: tuple[str, ...]) -> str | None:
    for path in paths:
        for part in path.split("/"):
            found = next((el for el in root.iter() if _local(el.tag) == part), None)
            if found is not None and found.text:
                return found.text.strip()
            if found is None:
                break
    for el in root.iter():
        if _local(el.tag) == "title" and el.text:
            return el.text.strip()
    return None

