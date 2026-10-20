"""Static HTML reader for single pages and article list pages."""

from __future__ import annotations

import hashlib
import json
import re
from html import unescape
from html.parser import HTMLParser
from typing import Any
from urllib.parse import urldefrag, urljoin, urlsplit

from ..models import AdapterCapabilities, Cursor, FetchResult, RawMessage, ValidationResult
from ..timeutil import iso_utc, parse_datetime
from .base import BaseAdapter, FetchOptions, SourceContext


class _PageParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.title_parts: list[str] = []
        self.description = ""
        self.meta: dict[str, str] = {}
        self.paragraphs: list[str] = []
        self.links: list[tuple[str, str]] = []
        self.images: list[str] = []
        self.time_values: list[str] = []
        self._in_title = False
        self._in_article_depth = 0
        self._skip_depth = 0
        self._current_link: str | None = None
        self._link_text: list[str] = []
        self._current_block: list[str] | None = None
        self._last_text = ""

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        attrs_dict = {k.lower(): (v or "") for k, v in attrs}
        if tag == "title":
            self._in_title = True
        if tag in ("script", "style", "noscript"):
            self._skip_depth += 1
        if tag in ("article", "main"):
            self._in_article_depth += 1
        if tag == "meta":
            name = (attrs_dict.get("property") or attrs_dict.get("name") or "").lower()
            content = attrs_dict.get("content", "")
            if name and content:
                self.meta[name] = content
                if name in ("description", "og:description") and not self.description:
                    self.description = content
        if tag == "time" and attrs_dict.get("datetime"):
            self.time_values.append(attrs_dict["datetime"])
        if tag == "a" and attrs_dict.get("href"):
            self._current_link = attrs_dict["href"]
            self._link_text = []
        if tag == "img":
            src = (attrs_dict.get("src") or attrs_dict.get("data-src") or "").strip()
            if src and not src.startswith("data:") and src not in self.images:
                self.images.append(src)
        if tag in ("p", "h1", "h2", "h3", "li", "blockquote"):
            self._current_block = []

    def handle_endtag(self, tag: str) -> None:
        if tag == "title":
            self._in_title = False
        if tag in ("script", "style", "noscript") and self._skip_depth:
            self._skip_depth -= 1
        if tag in ("article", "main") and self._in_article_depth:
            self._in_article_depth -= 1
        if tag == "a" and self._current_link:
            text = _clean(" ".join(self._link_text))
            if text:
                self.links.append((self._current_link, text))
            self._current_link = None
            self._link_text = []
        if tag in ("p", "h1", "h2", "h3", "li", "blockquote") and self._current_block is not None:
            text = _clean(" ".join(self._current_block))
            if text:
                self.paragraphs.append(text)
            self._current_block = None

    def handle_data(self, data: str) -> None:
        if self._skip_depth:
            return
        text = data.strip()
        if not text:
            return
        if self._in_title:
            self.title_parts.append(text)
        if self._current_link:
            self._link_text.append(text)
        if self._current_block is not None:
            self._current_block.append(text)
        self._last_text = text


def _clean(value: str) -> str:
    value = unescape(value)
    return re.sub(r"\s+", " ", value).strip()


def _parse_page(body: bytes, url: str) -> _PageParser:
    parser = _PageParser()
    head = body[:2048].decode("ascii", errors="ignore").lower()
    match = re.search(r"charset=[\"']?([a-z0-9_-]+)", head)
    declared = match.group(1) if match else ""
    candidates = [name for name in (declared, "utf-8", "gb18030", "gbk") if name]
    text = ""
    for charset in candidates:
        try:
            text = body.decode(charset)
        except (LookupError, UnicodeDecodeError):
            continue
        break
    if not text:
        text = body.decode(declared or "utf-8", errors="replace")
    parser.feed(text)
    parser.close()
    return parser


class WebAdapter(BaseAdapter):
    adapter_type = "web"

    @staticmethod
    def _request_headers(config: dict[str, Any]) -> dict[str, str]:
        """Optional login cookie so members-only pages can be read."""
        cookie = str(config.get("cookie") or "").strip()
        if not cookie or cookie == "***":
            return {}
        return {"Cookie": cookie}

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
        mode = str(config.get("mode") or "auto")
        if mode not in ("page", "list", "auto"):
            return ValidationResult(False, "mode 只能是 page、list 或 auto")
        return ValidationResult(True, "配置有效")

    def fetch_incremental(
        self, source: SourceContext, cursor: Cursor, options: FetchOptions
    ) -> FetchResult:
        config = source.config
        url = str(config.get("url", "")).strip()
        mode = str(config.get("mode") or "auto").strip().lower()
        if mode not in ("page", "list"):
            mode = self._detect_mode(url, config, options)
        if mode == "list":
            return self._fetch_list(source, cursor, options, url, config)
        return self._fetch_page(source, cursor, options, url, config)

    def _detect_mode(
        self, url: str, config: dict[str, Any], options: FetchOptions
    ) -> str:
        """Decide page vs list without asking the user to configure rules."""
        try:
            response = self.fetcher.fetch(
                url,
                timeout=options.timeout_seconds,
                headers=self._request_headers(config),
            )
            parser = _parse_page(response.body, response.url or url)
        except Exception:  # noqa: BLE001 - fall back to single page
            return "page"
        return "list" if self._looks_like_list(parser, url) else "page"

    @staticmethod
    def _looks_like_list(parser: "_PageParser", url: str) -> bool:
        base_host = urlsplit(url).netloc
        seen: set[str] = set()
        candidates = 0
        for href, text in parser.links:
            if len(text.strip()) < 6:
                continue
            absolute, _ = urldefrag(urljoin(url, href))
            if urlsplit(absolute).netloc != base_host:
                continue
            if absolute.rstrip("/") == url.rstrip("/"):
                continue
            if re.search(r"\.(jpg|jpeg|png|gif|svg|css|js|pdf|zip|mp4)$", absolute, re.I):
                continue
            if absolute in seen:
                continue
            seen.add(absolute)
            candidates += 1
        return candidates >= 4

    def _fetch_page(self, source: SourceContext, cursor: Cursor, options: FetchOptions,
                    url: str, config: dict[str, Any]) -> FetchResult:
        response = self.fetcher.fetch(
            url, etag=cursor.etag, last_modified=cursor.last_modified,
            timeout=options.timeout_seconds,
            headers=self._request_headers(config),
        )
        if response.status == 304:
            return FetchResult(next_cursor=cursor, stats={"fetched": 0, "skipped": 0, "failed": 0})
        parser = _parse_page(response.body, response.url or url)
        title = _clean(" ".join(parser.title_parts)) or parser.meta.get("og:title") or source.name
        body_text = "\n\n".join(parser.paragraphs) or parser.description
        max_chars = int(config.get("max_chars", 20000))
        body_text = body_text[:max_chars]
        if not body_text.strip():
            raise ValueError("页面没有可提取的正文")
        content_hash = hashlib.sha256(body_text.encode("utf-8")).hexdigest()
        previous = cursor.cursor_value
        if previous and previous.startswith("{") is False and previous == content_hash:
            return FetchResult(next_cursor=cursor, stats={"fetched": 0, "skipped": 1, "failed": 0})
        try:
            previous_hash = json.loads(previous or "{}").get("hash")
        except json.JSONDecodeError:
            previous_hash = previous
        if previous_hash == content_hash:
            return FetchResult(next_cursor=cursor, stats={"fetched": 0, "skipped": 1, "failed": 0})
        published = (
            parse_datetime(parser.meta.get("article:published_time"))
            or parse_datetime(parser.meta.get("og:updated_time"))
            or (parse_datetime(parser.time_values[0]) if parser.time_values else None)
        )
        message = RawMessage(
            platform_message_id=f"{url}#{content_hash[:16]}",
            conversation_id=url,
            conversation_name=title,
            conversation_kind="web",
            published_at=iso_utc(published),
            content_text=f"{title}\n\n{body_text}" if title not in body_text else body_text,
            content_type="link",
            source_url=url,
            raw_payload={"title": title, "url": url, "hash": content_hash},
        )
        next_cursor = Cursor(
            cursor_type="mixed",
            cursor_value=json.dumps({"hash": content_hash}),
            last_message_time=message.published_at,
            last_platform_message_id=message.platform_message_id,
            etag=response.headers.get("etag") or cursor.etag,
            last_modified=response.headers.get("last-modified") or cursor.last_modified,
        )
        return FetchResult(
            messages=[message], next_cursor=next_cursor,
            stats={"fetched": 1, "skipped": 0, "failed": 0},
        )

    def _fetch_list(self, source: SourceContext, cursor: Cursor, options: FetchOptions,
                    url: str, config: dict[str, Any]) -> FetchResult:
        response = self.fetcher.fetch(
            url,
            timeout=options.timeout_seconds,
            headers=self._request_headers(config),
        )
        parser = _parse_page(response.body, response.url or url)
        pattern = config.get("link_pattern")
        regex = re.compile(pattern) if pattern else None
        try:
            seen = set(json.loads(cursor.cursor_value or "[]").get("seen", []))
        except (json.JSONDecodeError, AttributeError):
            seen = set()
        limit = max(1, min(int(config.get("max_items", 10)), 50))
        candidates: list[tuple[str, str]] = []
        seen_now: set[str] = set()
        for href, text in parser.links:
            absolute, _ = urldefrag(urljoin(url, href))
            if not absolute.startswith(("http://", "https://")):
                continue
            # Navigation entries have short labels ("首页", "理论学习"); real
            # article titles are noticeably longer, so skip the menu bar.
            if len(text.strip()) < 8:
                continue
            if absolute.rstrip("/") == url.rstrip("/"):
                continue
            if re.search(r"\.(jpg|jpeg|png|gif|svg|css|js|pdf|zip|mp4)$", absolute, re.I):
                continue
            if regex and not regex.search(absolute) and not regex.search(text):
                continue
            if absolute in seen and absolute not in seen_now:
                continue
            if absolute in seen_now:
                continue
            seen_now.add(absolute)
            candidates.append((absolute, text))
            if len(candidates) >= limit:
                break

        messages: list[RawMessage] = []
        failed = 0
        warnings: list[str] = []
        for absolute, text in candidates:
            try:
                article = self.fetcher.fetch(absolute, timeout=options.timeout_seconds)
                article_parser = _parse_page(article.body, absolute)
                title = (
                    _clean(" ".join(article_parser.title_parts))
                    or article_parser.meta.get("og:title")
                    or text
                )
                body_text = "\n\n".join(article_parser.paragraphs) or article_parser.description
                published = (
                    parse_datetime(article_parser.meta.get("article:published_time"))
                    or parse_datetime(article_parser.meta.get("og:updated_time"))
                    or (parse_datetime(article_parser.time_values[0])
                        if article_parser.time_values else None)
                )
                if not body_text:
                    warnings.append(f"正文未提取: {absolute}")
                    body_text = text
                messages.append(
                    RawMessage(
                        platform_message_id=absolute,
                        conversation_id=url,
                        conversation_name=source.name,
                        conversation_kind="web",
                        published_at=iso_utc(published),
                        content_text=f"{title}\n\n{body_text}",
                        content_type="link",
                        source_url=absolute,
                        raw_payload={
                            "title": title, "url": absolute,
                            "hash": hashlib.sha256(body_text.encode("utf-8")).hexdigest()[:32],
                        },
                    )
                )
            except Exception as exc:  # keep the list moving
                failed += 1
                warnings.append(f"{absolute}: {type(exc).__name__}")
        seen_list = list(seen | seen_now)[-300:]
        next_cursor = Cursor(
            cursor_type="mixed",
            cursor_value=json.dumps({"seen": seen_list}),
            last_message_time=iso_utc(),
            etag=response.headers.get("etag") or cursor.etag,
            last_modified=response.headers.get("last-modified") or cursor.last_modified,
        )
        return FetchResult(
            messages=messages,
            next_cursor=next_cursor,
            partial=failed > 0,
            warnings=warnings[:20],
            stats={"fetched": len(messages), "skipped": max(0, len(parser.links) - len(candidates)),
                   "failed": failed},
        )

