"""Deterministic crawler for static website list pages.

The model-driven collector is useful for unfamiliar sites, but it is a poor fit
for an archive that already exposes dated entries and pagination.  This crawler
follows those pages directly so a global date window is not truncated by a
model step/item budget.
"""

from __future__ import annotations

import hashlib
import re
from datetime import date, datetime, time
from pathlib import Path
from typing import Any
from urllib.parse import urldefrag, urljoin, urlsplit

from ..adapters.base import HttpFetcher, SourceContext
from ..adapters.web import _parse_page
from ..config import AppConfig
from ..errors import AppError
from ..models import Cursor, FetchResult, RawMessage
from ..timeutil import iso_utc, local_zone

DATE_RE = re.compile(r"(20\d{2})[-/年.](\d{1,2})[-/月.](\d{1,2})")
MAX_PAGES = 100
MAX_MEDIA_BYTES = 8 * 1024 * 1024
FILE_SUFFIXES = {
    ".pdf", ".doc", ".docx", ".xls", ".xlsx", ".ppt", ".pptx",
    ".zip", ".rar", ".7z", ".csv", ".txt", ".md",
}
_IMAGE_SUFFIXES = {".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp"}


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

        articles: list[dict[str, Any]] = []
        seen: set[str] = set()
        fetcher = HttpFetcher()
        for target, date_text in entries:
            if target in seen:
                continue
            seen.add(target)
            response = fetcher.fetch(target, timeout=30.0, headers=headers)
            parser = _parse_page(response.body, response.url or target)
            title = " ".join(parser.title_parts).strip()
            body = "\n\n".join(parser.paragraphs).strip() or parser.description.strip()
            if not title or not body:
                continue
            article_base = response.url or target
            images: list[str] = []
            for src in parser.images:
                image_url = urljoin(article_base, src)
                if image_url not in images:
                    images.append(image_url)
            articles.append(
                {
                    "target": target,
                    "date": date_text,
                    "title": title,
                    "body": body,
                    "images": images,
                    "links": list(parser.links),
                }
            )

        # Images shared by many articles are site chrome (logo, banners), not
        # article content, so drop anything that shows up across pages.
        frequency: dict[str, int] = {}
        for article in articles:
            for image_url in article["images"]:
                frequency[image_url] = frequency.get(image_url, 0) + 1
        common_limit = max(2, len(articles) // 4)

        messages: list[RawMessage] = []
        for article in articles:
            target = article["target"]
            date_text = article["date"]
            title = article["title"]
            body = article["body"]
            kept = [
                url for url in article["images"] if frequency[url] <= common_limit
            ]
            image_lines = self._image_lines(kept, headers, date_text)
            file_lines = self._attachment_lines(
                article["links"], target, headers, date_text
            )
            if image_lines:
                body += "\n\n" + "\n".join(image_lines)
            if file_lines:
                body += "\n\n## 附件\n\n" + "\n".join(file_lines)
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

    def _image_lines(
        self, images: list[str], headers: dict[str, str], date_text: str
    ) -> list[str]:
        lines: list[str] = []
        for index, remote in enumerate(images[:10]):
            local = self._save_media(remote, headers, date_text, index)
            lines.append(f"![图片]({local or remote})")
        return lines

    def _attachment_lines(
        self,
        links: list[tuple[str, str]],
        base_url: str,
        headers: dict[str, str],
        date_text: str,
    ) -> list[str]:
        lines: list[str] = []
        seen: set[str] = set()
        for href, text in links:
            target = urldefrag(urljoin(base_url, href))[0]
            suffix = Path(urlsplit(target).path).suffix.lower()
            label = text.strip()
            is_download_link = (
                "download.jsp" in target or "DownloadAttachUrl" in target
            )
            if (
                not is_download_link
                and suffix not in FILE_SUFFIXES
                and not any(suffix in label.lower() for suffix in FILE_SUFFIXES)
            ):
                continue
            if target in seen:
                continue
            seen.add(target)
            name = label or Path(urlsplit(target).path).name or "附件"
            local = self._save_attachment(target, name, headers, date_text)
            lines.append(f"- [{name}]({local or target})")
        return lines

    def _save_media(
        self, url: str, headers: dict[str, str], date_text: str, index: int
    ) -> str:
        """Download an article image so it survives the WebVPN session."""
        suffix = Path(urlsplit(url).path).suffix.lower()
        if suffix not in _IMAGE_SUFFIXES:
            suffix = ""
        digest = hashlib.md5(url.encode("utf-8")).hexdigest()[:10]
        root = Path(self.config.data_dir) / "media" / date_text
        for existing in root.glob(f"{digest}-{index}.*") if root.is_dir() else []:
            return f"/media/{date_text}/{existing.name}"
        try:
            response = HttpFetcher().fetch(url, timeout=30.0, headers=headers)
        except AppError:
            return ""
        if (
            not response.body
            or _looks_truncated(response)
            or not _is_image(response)
            or len(response.body) < 1024
        ):
            return ""
        if suffix not in _IMAGE_SUFFIXES:
            suffix = _image_suffix(response.headers.get("content-type", "")) or ".png"
        root.mkdir(parents=True, exist_ok=True)
        name = f"{digest}-{index}{suffix}"
        (root / name).write_bytes(response.body)
        return f"/media/{date_text}/{name}"

    def _save_attachment(
        self, url: str, label: str, headers: dict[str, str], date_text: str
    ) -> str:
        """Download an article attachment so the link is always clickable."""
        suffix = Path(label).suffix or Path(urlsplit(url).path).suffix or ".bin"
        digest = hashlib.md5(url.encode("utf-8")).hexdigest()[:10]
        root = Path(self.config.data_dir) / "attachments" / date_text
        for existing in root.glob(f"{digest}-*") if root.is_dir() else []:
            return f"/attachments/{date_text}/{existing.name}"
        try:
            response = HttpFetcher().fetch(url, timeout=60.0, headers=headers)
        except AppError:
            return ""
        if not response.body or _looks_truncated(response) or _is_html_page(response):
            return ""
        safe = re.sub(r"[^\w.\-]+", "_", Path(label).stem)[:40] or "attachment"
        root.mkdir(parents=True, exist_ok=True)
        name = f"{digest}-{safe}{suffix}"
        (root / name).write_bytes(response.body)
        return f"/attachments/{date_text}/{name}"

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


def _looks_truncated(response: Any) -> bool:
    if len(response.body) >= MAX_MEDIA_BYTES:
        return True
    length = str(response.headers.get("content-length") or "").strip()
    if not length.isdigit():
        return False
    return int(length) > len(response.body)


def _image_suffix(content_type: str) -> str:
    value = content_type.split(";", 1)[0].strip().lower()
    return {
        "image/png": ".png",
        "image/jpeg": ".jpg",
        "image/gif": ".gif",
        "image/webp": ".webp",
        "image/bmp": ".bmp",
    }.get(value, "")


def _is_html_page(response: Any) -> bool:
    """Some CMS attachment links return a captcha page instead of the file."""
    content_type = str(response.headers.get("content-type", "")).lower()
    if "text/html" in content_type:
        return True
    head = response.body[:256].lstrip().lower()
    return head.startswith(b"<!doctype html") or head.startswith(b"<html")


def _is_image(response: Any) -> bool:
    content_type = str(response.headers.get("content-type", "")).lower()
    if content_type.startswith("image/"):
        return True
    head = response.body[:12]
    return (
        head.startswith(b"\x89PNG")
        or head.startswith(b"\xff\xd8\xff")
        or head.startswith(b"GIF8")
        or (head[:4] == b"RIFF" and head[8:12] == b"WEBP")
    )
