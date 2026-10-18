"""Model-driven collector.

The model is the one deciding which pages to read: it receives a
``fetch_page`` tool, calls it as often as it needs, and finally returns the
items that belong to today. Code only executes the tool calls, downloads
images and persists the result as Markdown.
"""

from __future__ import annotations

import json
import re
import urllib.error
import urllib.request
import base64
import mimetypes
from datetime import datetime
from pathlib import Path
from typing import Any
from urllib.parse import urljoin, urlsplit

from ..adapters.base import FetchOptions, HttpFetcher, SourceContext
from ..adapters.web import _parse_page
from ..errors import AppError
from ..models import Cursor, FetchResult, RawMessage
from ..timeutil import iso_utc, local_zone, parse_datetime
from .profile_prompt import profile_prompt

MAX_STEPS = 12
MAX_ITEMS = 15
PAGE_CHAR_LIMIT = 6000
LINK_LIMIT = 60

TOOL_SCHEMA = [
    {
        "type": "function",
        "function": {
            "name": "fetch_page",
            "description": "抓取一个网页，返回标题、正文、图片地址和页面内链接。",
            "parameters": {
                "type": "object",
                "properties": {"url": {"type": "string", "description": "要抓取的完整地址"}},
                "required": ["url"],
            },
        },
    }
]


class AgentCollector:
    def __init__(self, config: Any, trace: Any = None, on_cookies: Any = None) -> None:
        self.config = config
        self.trace = trace
        self.on_cookies = on_cookies
        self.fetcher = HttpFetcher()
        self.zone = local_zone(config.system.timezone)
        self.today = datetime.now(self.zone).date()
        self.media_root = Path(config.data_dir) / "media" / self.today.isoformat()
        self._browser = None

    def _ensure_context(self):
        """Open the saved login profile once per run so the session refreshes."""
        if getattr(self, "_context_checked", False):
            return getattr(self, "_context", None)
        self._context_checked = True
        profile = Path(self.config.data_dir) / "browser" / str(self._source_id or "")
        if not self._source_id or not profile.is_dir():
            return None
        try:
            from playwright.sync_api import sync_playwright

            self._browser = sync_playwright().start()
            self._context = self._browser.chromium.launch_persistent_context(
                str(profile), headless=True
            )
        except Exception as exc:  # noqa: BLE001 - fall back to plain HTTP
            if self.trace:
                self.trace("tool", "浏览器配置不可用", str(exc))
            self.close()
            self._context = None
        return self._context

    def close(self) -> None:
        context = getattr(self, "_context", None)
        browser = getattr(self, "_browser", None)
        if context is not None and self.on_cookies and not getattr(self, "_auth_failed", False):
            try:
                from .browser_login import _build_cookie_header, _cookie_expiry

                cookies = context.cookies()
                header = _build_cookie_header(cookies, self._source_url or "")
                if header:
                    self.on_cookies(header, _cookie_expiry(cookies, self._source_url or "") or "")
            except Exception:  # noqa: BLE001 - cookie persistence is best effort
                pass
        self._context = None
        self._browser = None
        if context is not None:
            try:
                context.close()
            except Exception:  # noqa: BLE001
                pass
        if browser is not None:
            try:
                browser.stop()
            except Exception:  # noqa: BLE001
                pass

    # -- model plumbing -------------------------------------------------
    def _api_key(self) -> str:
        model = self.config.model
        import os

        key = model.api_key or os.environ.get(model.api_key_env, "")
        if not key:
            raise AppError("AUTH_REQUIRED", "未配置模型 API Key，无法启动智能体采集")
        return key

    def _chat(self, messages: list[dict[str, Any]], tools: list[dict[str, Any]] | None) -> dict[str, Any]:
        model = self.config.model
        if not model.base_url or not model.model:
            raise AppError("CONFIG_INVALID", "模型 base_url 或 model 未配置")
        if self.trace:
            self.trace("prompt", f"→ {model.model}", _render_prompt(messages))
        payload: dict[str, Any] = {
            "model": model.model,
            "messages": messages,
            "temperature": 0.1,
        }
        if tools:
            payload["tools"] = tools
            payload["tool_choice"] = "auto"
        request = urllib.request.Request(
            model.base_url.rstrip("/") + "/chat/completions",
            data=json.dumps(payload).encode("utf-8"),
            headers={
                "Content-Type": "application/json",
                "Authorization": f"Bearer {self._api_key()}",
            },
            method="POST",
        )
        try:
            with urllib.request.urlopen(request, timeout=model.timeout_seconds) as response:
                body = json.loads(response.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", errors="replace")[:300]
            code = "AUTH_EXPIRED" if exc.code in (401, 403) else "REMOTE_TEMPORARY_ERROR"
            raise AppError(code, f"模型调用失败 HTTP {exc.code}: {detail}") from exc
        except TimeoutError as exc:
            raise AppError("NETWORK_TIMEOUT", "模型调用超时") from exc
        except urllib.error.URLError as exc:
            raise AppError("NETWORK_UNAVAILABLE", "模型接口不可达") from exc
        if not isinstance(body, dict):
            raise AppError("SCHEMA_INVALID", "模型返回格式异常")
        choices = body.get("choices") or []
        if not choices:
            raise AppError("SCHEMA_INVALID", "模型未返回 choices")
        message = choices[0].get("message") or {}
        if self.trace:
            text = message.get("content") or ""
            calls = message.get("tool_calls") or []
            if calls:
                names = ", ".join(
                    (call.get("function") or {}).get("name", "") for call in calls
                )
                detail = "；".join(
                    (call.get("function") or {}).get("arguments", "") for call in calls
                )
                text = f"[调用工具] {names} {detail}".strip()
            self.trace("reply", f"← {model.model}", text or "（空回复）")
        return message

    # -- tool execution --------------------------------------------------
    def _http_get(self, url: str, headers: dict[str, str]) -> tuple[bytes, str, dict[str, str]]:
        context = self._ensure_context()
        if context is not None:
            page = context.new_page()
            try:
                response = page.goto(url, wait_until="domcontentloaded", timeout=45000)
                body = page.content().encode("utf-8")
                return body, page.url, dict(response.headers) if response else {}
            finally:
                page.close()
        response = self.fetcher.fetch(url, timeout=30.0, headers=headers)
        return response.body, response.url or url, dict(response.headers)

    def _fetch_page(self, url: str, headers: dict[str, str]) -> dict[str, Any]:
        body, final_url, _ = self._http_get(url, headers)
        parser = _parse_page(body, final_url)
        title = " ".join(parser.title_parts).strip() or final_url
        text = "\n\n".join(parser.paragraphs) or parser.description
        if len(text) < 300 and ("登录" in title or "登录" in text[:80]):
            self._auth_failed = True
            raise AppError(
                "AUTH_REQUIRED",
                "页面要求登录，Cookie 可能已失效。请在来源里重新点「浏览器登录」。",
            )
        links: list[dict[str, str]] = []
        seen: set[str] = set()
        for href, label in parser.links:
            absolute = urljoin(final_url, href)
            if absolute in seen or not absolute.startswith(("http://", "https://")):
                continue
            seen.add(absolute)
            links.append({"url": absolute, "text": label})
            if len(links) >= LINK_LIMIT:
                break
        return {
            "url": final_url,
            "title": title,
            "text": text[:PAGE_CHAR_LIMIT],
            "images": [urljoin(final_url, src) for src in parser.images[:20]],
            "links": links,
        }

    def _load_prompt_files(
        self, entries: list[Any]
    ) -> tuple[str, list[str]]:
        """Split uploaded references into prompt text and vision data URLs."""
        texts: list[str] = []
        images: list[str] = []
        uploads = Path(self.config.data_dir) / "uploads"
        for entry in entries:
            if not isinstance(entry, dict):
                continue
            path = str(entry.get("path") or "")
            name = str(entry.get("name") or path)
            if not path.startswith("/uploads/"):
                continue
            local = uploads / path[len("/uploads/"):]
            if not local.is_file():
                continue
            mime = str(entry.get("type") or mimetypes.guess_type(name)[0] or "")
            if mime.startswith("image/"):
                data = base64.b64encode(local.read_bytes()).decode()
                images.append(f"data:{mime};base64,{data}")
            else:
                try:
                    body = local.read_text(encoding="utf-8", errors="replace")
                except OSError:
                    continue
                texts.append(f"### {name}\n{body[:6000]}")
        return "\n\n".join(texts), images

    # -- collection ------------------------------------------------------
    def collect(self, source: SourceContext) -> FetchResult:
        self._source_id = source.id
        cfg = source.config or {}
        url = str(cfg.get("url") or "").strip()
        self._source_url = url
        if not url.startswith(("http://", "https://")):
            raise AppError("CONFIG_INVALID", "来源地址必须以 http:// 或 https:// 开头")
        cookie = str(cfg.get("cookie") or "").strip()
        headers = {"Cookie": cookie} if cookie and cookie != "***" else {}
        prompt = str(cfg.get("prompt") or "").strip()
        extra_docs, image_urls = self._load_prompt_files(cfg.get("prompt_files") or [])

        messages: list[dict[str, Any]] = [
            {"role": "system", "content": self._system_prompt(prompt, extra_docs)},
            {"role": "user", "content": f"来源名称：{source.name}\n起始地址：{url}"},
        ]
        if image_urls:
            messages[-1] = {
                "role": "user",
                "content": [{"type": "text", "text": f"来源名称：{source.name}\n起始地址：{url}"}]
                + [{"type": "image_url", "image_url": {"url": item}} for item in image_urls[:4]],
            }
        warnings: list[str] = []
        for _ in range(MAX_STEPS):
            try:
                reply = self._chat(messages, TOOL_SCHEMA)
            except AppError as exc:
                if image_urls and exc.code in ("SCHEMA_INVALID", "REMOTE_TEMPORARY_ERROR"):
                    warnings.append(f"模型不接受图片输入，已改为纯文本：{exc.message}")
                    image_urls = []
                    messages[-1] = {
                        "role": "user",
                        "content": f"来源名称：{source.name}\n起始地址：{url}",
                    }
                    continue
                raise
            calls = reply.get("tool_calls") or []
            if not calls:
                return self._finish(reply.get("content") or "", source, url, headers, warnings)
            messages.append({"role": "assistant", "content": reply.get("content") or "",
                             "tool_calls": calls})
            for call in calls:
                name = (call.get("function") or {}).get("name")
                raw_args = (call.get("function") or {}).get("arguments") or "{}"
                try:
                    args = json.loads(raw_args)
                except json.JSONDecodeError:
                    args = {}
                if name != "fetch_page":
                    result: Any = {"error": f"未知工具 {name}"}
                else:
                    target = str(args.get("url") or "").strip()
                    if self.trace:
                        self.trace("tool", "抓取网页", target)
                    try:
                        result = self._fetch_page(target, headers)
                    except AppError as exc:
                        result = {"error": exc.message}
                    except Exception as exc:  # noqa: BLE001 - report to the model
                        result = {"error": f"{type(exc).__name__}: {exc}"}
                messages.append({
                    "role": "tool",
                    "tool_call_id": call.get("id") or "",
                    "content": json.dumps(result, ensure_ascii=False)[:12000],
                })
        warnings.append("达到单次采集的最大步数，已停止")
        return FetchResult(messages=[], next_cursor=Cursor(), partial=True, warnings=warnings,
                           stats={"fetched": 0, "skipped": 0, "failed": 0})

    def _system_prompt(self, extra: str, docs: str = "") -> str:
        base = (
            "你是一个信息采集智能体。你有 fetch_page 工具，可以抓取任意网页。\n"
            f"今天是 {self.today.isoformat()}（时区 {self.config.system.timezone}）。\n"
            "工作方式：先抓取起始地址，阅读内容；如果它是列表页，挑选真正需要归档的条目并逐条抓取详情。\n"
            "列表页里今天发布的每一条都要单独调用 fetch_page 抓详情，不要遗漏，也不要只看第一条。\n"
            "只保留发布日期是今天的条目；无法确认日期的条目一律丢弃。\n"
            "抓够之后，输出一个 JSON 对象，不要输出任何其他文字：\n"
            '{"items":[{"title":"标题","url":"原文地址","published_at":"YYYY-MM-DD",'
            '"summary":"两三句话的摘要","content_md":"用 Markdown 记录的正文，图片写成 ![](图片地址)",'
            '"images":["图片地址"],"attachments":[{"name":"附件名","url":"附件地址"}]}]}\n'
            f"最多 {MAX_ITEMS} 条。"
        )
        if extra:
            base += "\n\n用户的采集要求（优先遵守）：\n" + extra
        profile = profile_prompt(self.config.profile)
        if profile:
            base += "\n\n" + profile
        if docs:
            base += "\n\n参考文档：\n" + docs
        return base

    def _finish(
        self,
        raw: str,
        source: SourceContext,
        base_url: str,
        headers: dict[str, str],
        warnings: list[str],
    ) -> FetchResult:
        data = _extract_json(raw)
        items = data.get("items") if isinstance(data, dict) else None
        if not isinstance(items, list):
            raise AppError("SCHEMA_INVALID", "模型没有返回 items 列表")
        messages: list[RawMessage] = []
        skipped = 0
        for item in items[:MAX_ITEMS]:
            if not isinstance(item, dict):
                continue
            published = parse_datetime(str(item.get("published_at") or ""))
            if published is None or published.astimezone(self.zone).date() != self.today:
                skipped += 1
                continue
            link = str(item.get("url") or base_url).strip()
            title = str(item.get("title") or source.name).strip()
            content = str(item.get("content_md") or item.get("summary") or "").strip()
            image_urls = [str(u) for u in (item.get("images") or []) if str(u).startswith("http")]
            inline_images = re.findall(r"!\[[^\]]*\]\((https?://[^)\s]+)\)", content)
            for url in inline_images:
                if url not in image_urls:
                    image_urls.append(url)
            local_images = self._download_images(image_urls, headers, warnings)
            if local_images:
                for remote, local in local_images.items():
                    content = content.replace(remote, local)
                if "![" not in content:
                    content += "\n\n" + "\n".join(
                        f"![图片]({path})" for path in local_images.values()
                    )
            attachments = self._download_attachments(item.get("attachments") or [], headers, warnings)
            if attachments:
                content += "\n\n## 附件\n\n" + "\n".join(
                    f"- [{name}]({path})" for name, path in attachments
                )
            if not content:
                skipped += 1
                continue
            messages.append(
                RawMessage(
                    platform_message_id=link,
                    conversation_id=base_url,
                    conversation_name=title,
                    conversation_kind="web",
                    published_at=iso_utc(published),
                    content_text=content,
                    content_type="markdown",
                    source_url=link,
                    raw_payload={"title": title, "url": link, "summary": item.get("summary")},
                )
            )
        return FetchResult(
            messages=messages,
            next_cursor=Cursor(cursor_type="mixed", cursor_value=iso_utc()),
            partial=bool(warnings),
            warnings=warnings[:20],
            stats={"fetched": len(messages), "skipped": skipped, "failed": 0},
        )

    def _download_images(
        self, urls: list[str], headers: dict[str, str], warnings: list[str]
    ) -> dict[str, str]:
        saved: dict[str, str] = {}
        for index, url in enumerate(urls[:6]):
            try:
                body, _, resp_headers = self._http_get(url, headers)
            except Exception as exc:  # noqa: BLE001 - images are best effort
                warnings.append(f"图片下载失败 {url}: {type(exc).__name__}")
                continue
            suffix = _image_suffix(url, resp_headers.get("content-type", ""))
            self.media_root.mkdir(parents=True, exist_ok=True)
            name = f"{abs(hash(url)) % (10 ** 10)}-{index}{suffix}"
            path = self.media_root / name
            path.write_bytes(body)
            saved[url] = f"/media/{self.today.isoformat()}/{name}"
        return saved

    def _download_attachments(
        self, entries: list[Any], headers: dict[str, str], warnings: list[str]
    ) -> list[tuple[str, str]]:
        saved: list[tuple[str, str]] = []
        root = Path(self.config.data_dir) / "attachments" / self.today.isoformat()
        for entry in entries[:8]:
            if not isinstance(entry, dict):
                continue
            url = str(entry.get("url") or "").strip()
            name = str(entry.get("name") or "").strip()
            if not url.startswith(("http://", "https://")):
                continue
            if not name:
                name = Path(urlsplit(url).path).name or "attachment"
            try:
                body, _, resp_headers = self._http_get(url, headers)
            except Exception as exc:  # noqa: BLE001 - attachments are best effort
                warnings.append(f"附件下载失败 {name}: {type(exc).__name__}")
                continue
            suffix = Path(name).suffix or Path(urlsplit(url).path).suffix or ".bin"
            if not Path(name).suffix:
                name += suffix
            safe = re.sub(r"[^\w.\-]+", "_", name) or "attachment.bin"
            root.mkdir(parents=True, exist_ok=True)
            path = root / f"{abs(hash(url)) % (10 ** 8)}-{safe}"
            path.write_bytes(body)
            saved.append((name, f"/attachments/{self.today.isoformat()}/{path.name}"))
        return saved


def _extract_json(text: str) -> Any:
    cleaned = text.strip()
    if cleaned.startswith("```"):
        cleaned = re.sub(r"^```[a-zA-Z]*\n?", "", cleaned)
        cleaned = re.sub(r"```$", "", cleaned).strip()
    try:
        return json.loads(cleaned)
    except json.JSONDecodeError:
        match = re.search(r"\{.*\}", cleaned, re.S)
        if not match:
            raise AppError("SCHEMA_INVALID", "模型输出不是 JSON") from None
        return json.loads(match.group(0))


def _image_suffix(url: str, content_type: str) -> str:
    if "png" in content_type:
        return ".png"
    if "gif" in content_type:
        return ".gif"
    if "webp" in content_type:
        return ".webp"
    suffix = Path(urlsplit(url).path).suffix.lower()
    if suffix in (".png", ".jpg", ".jpeg", ".gif", ".webp"):
        return suffix
    return ".jpg"


def _render_prompt(messages: list[dict[str, Any]]) -> str:
    """Show the conversation part only; tool payloads stay out of the log."""
    parts: list[str] = []
    for item in messages:
        role = item.get("role")
        content = item.get("content")
        if role == "system":
            parts.append(f"[system]\n{str(content)[:800]}")
        elif role == "user":
            parts.append(f"[user]\n{str(content)[:900]}")
    return "\n\n".join(parts)[-1800:]
