"""Deterministic crawler for static website list pages.

The model-driven collector is useful for unfamiliar sites, but it is a poor fit
for an archive that already exposes dated entries and pagination.  This crawler
follows those pages directly so a global date window is not truncated by a
model step/item budget.
"""

from __future__ import annotations

import re
from datetime import date, datetime, time
from typing import Any
from urllib.parse import urldefrag, urljoin

from ..adapters.base import HttpFetcher, SourceContext
from ..adapters.web import _parse_page
from ..config import AppConfig
from ..models import Cursor, FetchResult, RawMessage
from ..timeutil import iso_utc, local_zone

DATE_RE = re.compile(r"(20\d{2})[-/年.](\d{1,2})[-/月.](\d{1,2})")
MAX_PAGES = 100


class StaticListUnavailable(Exception):
    """Raised when a page does not look like a dated static list."""


class StaticListCollector:
    def __init__(self, config: AppConfig):
        self.config = config
        self.zone = local_zone(config.system.timezone)
        self.range_start, self.range_end = config.effective_range()

    def collect(self, source: SourceContext) -> FetchResult:
        url = str(source.config.get("url") or "").strip()
        if not url.startswith(("http://", "https://")):
            raise StaticListUnavailable("not a static website URL")
        if not self.range_start:
            raise StaticListUnavailable("no global range")

        headers = {}
        cookie = str(source.config.get("cookie") or "").strip()
        if cookie and cookie != "***":
            headers["Cookie"] = cookie

        entries: list[tuple[str, str]] = []
        visited: set[str] = set()
        queue = [url]
        found_dates = False
        while queue and len(visited) < MAX_PAGES:
            page_url = queue.pop(0)
            page_url, _ = urldefrag(page_url)
            if page_url in visited:
                continue
            visited.add(page_url)
            response = HttpFetcher().fetch(
                page_url, timeout=30.0, headers=headers
            )
            parser = _parse_page(response.body, response.url or page_url)
            dated = self._dated_entries(parser)
            found_dates = found_dates or bool(dated)
            oldest: str | None = None
            for title, date_text in dated:
                if oldest is None or date_text < oldest:
                    oldest = date_text
                if self.range_start <= date_text <= self.range_end:
                    target = self._matching_link(parser, page_url, title)
                    if target:
                        entries.append((target, date_text))
            if oldest is not None and oldest < self.range_start:
                break
            next_url = self._next_page(parser, page_url, visited)
            if next_url:
                queue.append(next_url)

        if not found_dates:
            raise StaticListUnavailable("no dated list entries")

        messages: list[RawMessage] = []
        seen: set[str] = set()
        for target, date_text in entries:
            if target in seen:
                continue
            seen.add(target)
            response = HttpFetcher().fetch(target, timeout=30.0, headers=headers)
            parser = _parse_page(response.body, response.url or target)
            title = " ".join(parser.title_parts).strip()
            body = "\n\n".join(parser.paragraphs).strip() or parser.description.strip()
            if not title or not body:
                continue
            published_at = datetime.combine(
                date.fromisoformat(date_text), time.min, self.zone
            )
            messages.append(
                RawMessage(
                    platform_message_id=target,
                    conversation_id=url,
                    conversation_name=title,
                    conversation_kind="web",
                    published_at=iso_utc(published_at),
                    content_text=f"{title}\n\n{body}",
                    content_type="markdown",
                    source_url=target,
                    raw_payload={"title": title, "url": target, "date": date_text},
                )
            )
        return FetchResult(
            messages=messages,
            next_cursor=Cursor(cursor_type="static_list", cursor_value=iso_utc()),
            stats={"fetched": len(messages), "skipped": 0, "failed": 0},
        )

    @staticmethod
    def _dated_entries(parser: Any) -> list[tuple[str, str]]:
        entries: list[tuple[str, str]] = []
        for paragraph in parser.paragraphs:
            match = DATE_RE.search(paragraph)
            if not match:
                continue
            title = paragraph[:match.start()].strip(" -|·")
            if not title:
                continue
            date_text = f"{match.group(1)}-{int(match.group(2)):02d}-{int(match.group(3)):02d}"
            entries.append((title, date_text))
        return entries

    @staticmethod
    def _matching_link(parser: Any, page_url: str, title: str) -> str:
        normalized = _normalize(title)
        links = [
            (urldefrag(urljoin(page_url, href))[0], text)
            for href, text in parser.links
            if href and not href.startswith("#")
        ]
        for target, text in links:
            candidate = _normalize(text)
            if candidate and (
                candidate == normalized
                or candidate in normalized
                or normalized in candidate
            ):
                return target
        return ""

    @staticmethod
    def _next_page(parser: Any, page_url: str, visited: set[str]) -> str:
        numeric: list[str] = []
        for href, text in parser.links:
            label = text.strip()
            if label.isdigit():
                target = urldefrag(urljoin(page_url, href))[0]
                if target not in visited and target not in numeric:
                    numeric.append(target)
            elif label in ("下一页", "下页", "›", "»"):
                target = urldefrag(urljoin(page_url, href))[0]
                if target not in visited and target not in numeric:
                    numeric.append(target)
        return numeric[0] if numeric else ""


def _normalize(value: str) -> str:
    return re.sub(r"\s+", "", value).replace("…", "").replace("...", "")
