from __future__ import annotations

import sys
import unittest
from tempfile import TemporaryDirectory
from unittest.mock import patch

from msgdesk.adapters.base import (
    FetchOptions,
    FetchResponse,
    HttpFetcher,
    SourceContext,
    create_fetcher,
)
from msgdesk.adapters.scrapling_http import (
    MAX_BODY_BYTES,
    ScraplingHttpFetcher,
)
from msgdesk.adapters.web import WebAdapter
from msgdesk.config import AppConfig
from msgdesk.errors import AppError
from msgdesk.models import Cursor
from msgdesk.services.site_crawler import StaticListCollector, StaticListUnavailable


class FakeResponse:
    def __init__(
        self,
        status: int = 200,
        body: bytes = b"",
        headers: dict | None = None,
        url: str = "",
    ) -> None:
        self.status = status
        self.body = body
        self.headers = dict(headers or {})
        self.url = url


class FakeSession:
    def __init__(self, responses: list[object]) -> None:
        self.responses = list(responses)
        self.calls: list[tuple[str, dict]] = []
        self.close_calls = 0

    def get(self, url: str, **kwargs) -> object:
        recorded = dict(kwargs)
        if "headers" in recorded:
            recorded["headers"] = dict(recorded["headers"] or {})
        self.calls.append((url, recorded))
        if not self.responses:
            raise AssertionError(f"unexpected get call: {url}")
        action = self.responses.pop(0)
        if isinstance(action, BaseException):
            raise action
        return action

    def close(self) -> None:
        self.close_calls += 1


class FakeNoCloseSession(FakeSession):
    close = None


class FakeSessionManager:
    def __init__(self, session: FakeSession) -> None:
        self.session = session
        self.enter_calls = 0
        self.exit_calls: list[tuple[object, object, object]] = []

    def __enter__(self) -> FakeSession:
        self.enter_calls += 1
        return self.session

    def __exit__(self, exc_type, exc, traceback) -> None:
        self.exit_calls.append((exc_type, exc, traceback))


class FakeSessionFactory:
    def __init__(self, manager: FakeSessionManager) -> None:
        self.manager = manager
        self.calls: list[dict] = []

    def __call__(self, **kwargs) -> FakeSessionManager:
        self.calls.append(dict(kwargs))
        return self.manager


class RecordingFetcher:
    def __init__(self, body: bytes, final_url: str | None = None) -> None:
        self.body = body
        self.final_url = final_url
        self.calls: list[dict] = []
        self.close_calls = 0

    def fetch(
        self,
        url: str,
        *,
        etag: str | None = None,
        last_modified: str | None = None,
        timeout: float | None = 20.0,
        headers: dict[str, str] | None = None,
    ) -> FetchResponse:
        self.calls.append(
            {
                "url": url,
                "etag": etag,
                "last_modified": last_modified,
                "timeout": timeout,
                "headers": dict(headers or {}),
            }
        )
        return FetchResponse(
            status=200,
            body=self.body,
            headers={},
            url=self.final_url or url,
        )

    def close(self) -> None:
        self.close_calls += 1


class MappingFetcher:
    def __init__(
        self,
        responses: dict[str, FakeResponse],
        *,
        close_error: Exception | None = None,
    ) -> None:
        self.responses = dict(responses)
        self.close_error = close_error
        self.calls: list[dict] = []
        self.close_calls = 0

    def fetch(
        self,
        url: str,
        *,
        etag: str | None = None,
        last_modified: str | None = None,
        timeout: float | None = 20.0,
        headers: dict[str, str] | None = None,
    ) -> FakeResponse:
        self.calls.append(
            {
                "url": url,
                "etag": etag,
                "last_modified": last_modified,
                "timeout": timeout,
                "headers": dict(headers or {}),
            }
        )
        try:
            return self.responses[url]
        except KeyError as exc:
            raise AssertionError(f"unexpected fetch: {url}") from exc

    def close(self) -> None:
        self.close_calls += 1
        if self.close_error is not None:
            raise self.close_error


class FetcherFactoryTests(unittest.TestCase):
    def test_default_blank_and_urllib_return_http_fetcher(self) -> None:
        for value in ("urllib", "", "   ", None, " URLLIB "):
            with self.subTest(value=repr(value)):
                fetcher = create_fetcher(value, timeout=17.0)
                self.assertIsInstance(fetcher, HttpFetcher)
                self.assertEqual(fetcher.timeout, 17.0)
                fetcher.close()
                fetcher.close()

        default_fetcher = create_fetcher(timeout=18.0)
        self.assertIsInstance(default_fetcher, HttpFetcher)
        self.assertEqual(default_fetcher.timeout, 18.0)
        default_fetcher.close()

    def test_unknown_backend_is_config_invalid(self) -> None:
        with self.assertRaises(AppError) as caught:
            create_fetcher("requests")

        self.assertEqual(caught.exception.code, "CONFIG_INVALID")
        self.assertEqual(caught.exception.details, {"fetch_backend": "requests"})
        self.assertFalse(caught.exception.retryable)
        self.assertTrue(caught.exception.needs_user_action)

    def test_missing_scrapling_is_dependency_missing(self) -> None:
        with patch.dict(
            sys.modules,
            {"scrapling": None, "scrapling.fetchers": None},
        ):
            with self.assertRaises(AppError) as caught:
                create_fetcher("scrapling_http", timeout=30.0)

        self.assertEqual(caught.exception.code, "DEPENDENCY_MISSING")
        self.assertIn("requirements-scrapling.txt", caught.exception.message)
        self.assertEqual(
            caught.exception.details,
            {"dependency": "scrapling[fetchers]==0.5"},
        )
        self.assertFalse(caught.exception.retryable)
        self.assertTrue(caught.exception.needs_user_action)


class ScraplingHttpFetcherTests(unittest.TestCase):
    def make_fetcher(
        self,
        responses: list[object],
        *,
        timeout: float = 20.0,
        session: FakeSession | None = None,
    ) -> tuple[ScraplingHttpFetcher, FakeSession, FakeSessionManager, FakeSessionFactory]:
        actual_session = session or FakeSession(responses)
        manager = FakeSessionManager(actual_session)
        factory = FakeSessionFactory(manager)
        patcher = patch(
            "msgdesk.adapters.scrapling_http._load_fetcher_session",
            return_value=factory,
        )
        patcher.start()
        self.addCleanup(patcher.stop)
        fetcher = ScraplingHttpFetcher(timeout=timeout)
        return fetcher, actual_session, manager, factory

    def test_response_mapping_conditional_headers_and_session_reuse(self) -> None:
        url = "https://example.test/source"
        final_url = "https://example.test/source/final"
        long_body = b"x" * (MAX_BODY_BYTES + 17)
        fetcher, session, _manager, factory = self.make_fetcher(
            [
                FakeResponse(
                    status="200",
                    body=b"hello",
                    headers={"Content-Type": "text/plain", "X-TRACE": "abc"},
                    url=final_url,
                ),
                FakeResponse(
                    status=200,
                    body=long_body,
                    headers={"Content-Type": "application/octet-stream"},
                    url="",
                ),
            ],
            timeout=8.0,
        )

        response = fetcher.fetch(
            url,
            etag="automatic-etag",
            last_modified="automatic-modified",
            timeout=2.5,
            headers={
                "Cookie": "session=explicit",
                "If-None-Match": "user-etag",
                "If-Modified-Since": "user-modified",
                "X-Custom": "value",
            },
        )

        self.assertEqual(factory.calls, [{"timeout": 8.0}])
        self.assertEqual(session.calls[0][1]["timeout"], 2.5)
        self.assertEqual(
            session.calls[0][1]["headers"],
            {
                "Cookie": "session=explicit",
                "If-None-Match": "user-etag",
                "If-Modified-Since": "user-modified",
                "X-Custom": "value",
            },
        )
        self.assertEqual(response.status, 200)
        self.assertIsInstance(response.status, int)
        self.assertEqual(response.body, b"hello")
        self.assertEqual(
            response.headers,
            {"content-type": "text/plain", "x-trace": "abc"},
        )
        self.assertTrue(all(key == key.lower() for key in response.headers))
        self.assertEqual(response.url, final_url)

        second = fetcher.fetch(
            url,
            etag="automatic-etag-2",
            last_modified="automatic-modified-2",
            headers={"Cookie": "session=explicit"},
        )

        self.assertEqual(len(factory.calls), 1)
        self.assertEqual(len(session.calls), 2)
        self.assertEqual(session.calls[1][1]["timeout"], 8.0)
        self.assertEqual(
            session.calls[1][1]["headers"],
            {
                "If-None-Match": "automatic-etag-2",
                "If-Modified-Since": "automatic-modified-2",
                "Cookie": "session=explicit",
            },
        )
        self.assertIsInstance(second.body, bytes)
        self.assertEqual(len(second.body), MAX_BODY_BYTES)
        self.assertEqual(second.url, url)

    def test_status_304_returns_empty_body(self) -> None:
        url = "https://example.test/cached"
        final_url = "https://example.test/cached/final"
        fetcher, _session, _manager, _factory = self.make_fetcher(
            [
                FakeResponse(
                    status=304,
                    body=b"must be ignored",
                    headers={"ETag": "abc", "Content-Length": "15"},
                    url=final_url,
                )
            ]
        )

        response = fetcher.fetch(url)

        self.assertEqual(response.status, 304)
        self.assertEqual(response.body, b"")
        self.assertEqual(
            response.headers,
            {"etag": "abc", "content-length": "15"},
        )
        self.assertEqual(response.url, final_url)

    def test_http_statuses_map_to_stable_error_codes(self) -> None:
        cases = (
            (400, "REMOTE_TEMPORARY_ERROR"),
            (401, "PERMISSION_DENIED"),
            (403, "PERMISSION_DENIED"),
            (404, "REMOTE_TEMPORARY_ERROR"),
            (429, "RATE_LIMITED"),
            (500, "REMOTE_TEMPORARY_ERROR"),
            (503, "REMOTE_TEMPORARY_ERROR"),
        )
        for status, expected_code in cases:
            with self.subTest(status=status):
                fetcher, _session, _manager, _factory = self.make_fetcher(
                    [FakeResponse(status=status, body=b"error")]
                )
                with self.assertRaises(AppError) as caught:
                    fetcher.fetch("https://example.test/http-error")
                self.assertEqual(caught.exception.code, expected_code)
                self.assertIn(f"HTTP {status}", caught.exception.message)

    def test_exceptions_map_without_leaking_third_party_types(self) -> None:
        class RequestTimedOut(Exception):
            pass

        class DnsResolutionError(Exception):
            pass

        cases = (
            (TimeoutError("too slow"), "NETWORK_TIMEOUT"),
            (RequestTimedOut("request timed out"), "NETWORK_TIMEOUT"),
            (ConnectionError("connection refused"), "NETWORK_UNAVAILABLE"),
            (DnsResolutionError("dns failed"), "NETWORK_UNAVAILABLE"),
            (RuntimeError("unexpected failure"), "REMOTE_TEMPORARY_ERROR"),
        )
        for error, expected_code in cases:
            with self.subTest(error=type(error).__name__):
                fetcher, _session, _manager, _factory = self.make_fetcher([error])
                with self.assertRaises(AppError) as caught:
                    fetcher.fetch("https://example.test/exception")
                self.assertEqual(caught.exception.code, expected_code)
                self.assertIs(caught.exception.__cause__, error)

    def test_close_prefers_session_close_and_is_idempotent(self) -> None:
        fetcher, session, manager, _factory = self.make_fetcher([])

        fetcher.close()
        fetcher.close()

        self.assertEqual(session.close_calls, 1)
        self.assertEqual(manager.exit_calls, [])

    def test_close_falls_back_to_context_exit_and_is_idempotent(self) -> None:
        session = FakeNoCloseSession([])
        fetcher, _session, manager, _factory = self.make_fetcher(
            [],
            session=session,
        )

        fetcher.close()
        fetcher.close()

        self.assertEqual(
            manager.exit_calls,
            [(None, None, None)],
        )


WEB_URL = "https://web.example.test/article"
WEB_HTML = b"<html><head><title>Web article</title></head><body><p>Body text.</p></body></html>"


class WebAdapterFetcherTests(unittest.TestCase):
    def test_default_web_backend_creates_one_urllib_fetcher(self) -> None:
        fetcher = RecordingFetcher(WEB_HTML)
        adapter = WebAdapter()
        source = SourceContext(
            id="web-source",
            type="web",
            name="Web source",
            config={"url": WEB_URL, "mode": "page", "fetch_backend": "   "},
        )

        with patch(
            "msgdesk.adapters.web.create_fetcher",
            return_value=fetcher,
        ) as factory:
            first = adapter.fetch_incremental(
                source,
                Cursor(),
                FetchOptions(timeout_seconds=12.5),
            )
            second = adapter.fetch_incremental(
                source,
                Cursor(),
                FetchOptions(timeout_seconds=12.5),
            )

        factory.assert_called_once_with("urllib", timeout=12.5)
        self.assertEqual(first.stats["fetched"], 1)
        self.assertEqual(second.stats["fetched"], 1)
        self.assertEqual(len(fetcher.calls), 2)
        self.assertIs(adapter.fetcher, fetcher)
        adapter.close()
        self.assertEqual(fetcher.close_calls, 1)

    def test_injected_fetcher_takes_priority_over_factory(self) -> None:
        fetcher = RecordingFetcher(WEB_HTML)
        adapter = WebAdapter(fetcher=fetcher)
        source = SourceContext(
            id="injected-source",
            type="web",
            name="Injected source",
            config={
                "url": WEB_URL,
                "mode": "page",
                "fetch_backend": "scrapling_http",
                "cookie": "session=web",
            },
        )

        with patch(
            "msgdesk.adapters.web.create_fetcher",
            side_effect=AssertionError("factory must not replace an injected fetcher"),
        ) as factory:
            result = adapter.fetch_incremental(
                source,
                Cursor(),
                FetchOptions(timeout_seconds=9.0),
            )

        factory.assert_not_called()
        self.assertEqual(result.stats["fetched"], 1)
        self.assertEqual(fetcher.calls[0]["headers"], {"Cookie": "session=web"})
        self.assertEqual(fetcher.calls[0]["timeout"], 9.0)
        self.assertIs(adapter.fetcher, fetcher)

    def test_validate_config_checks_backend_without_creating_fetcher(self) -> None:
        adapter = WebAdapter(fetcher=RecordingFetcher(WEB_HTML))

        with patch(
            "msgdesk.adapters.web.create_fetcher",
            side_effect=AssertionError("validation must not create a fetcher"),
        ) as factory:
            default = adapter.validate_config({"url": WEB_URL})
            scrapling = adapter.validate_config(
                {"url": WEB_URL, "fetch_backend": " SCRAPLING_HTTP "}
            )
            file_default = adapter.validate_config({"url": "file:///tmp/page.html"})
            unknown = adapter.validate_config(
                {"url": WEB_URL, "fetch_backend": "requests"}
            )
            file_scrapling = adapter.validate_config(
                {"url": "file:///tmp/page.html", "fetch_backend": "scrapling_http"}
            )

        factory.assert_not_called()
        self.assertTrue(default.ok)
        self.assertTrue(scrapling.ok)
        self.assertTrue(file_default.ok)
        self.assertFalse(unknown.ok)
        self.assertEqual(unknown.details, {"fetch_backend": "requests"})
        self.assertFalse(file_scrapling.ok)
        self.assertEqual(
            file_scrapling.details,
            {
                "fetch_backend": "scrapling_http",
                "url": "file:///tmp/page.html",
            },
        )


STATIC_LIST_URL = "https://static.example.test/list.html"
STATIC_PAGE_2_URL = "https://static.example.test/page2.html"
STATIC_ARTICLE_URL = "https://static.example.test/article.html"
STATIC_IMAGE_URL = "https://static.example.test/image.png"
STATIC_ATTACHMENT_URL = "https://static.example.test/files/report.pdf"

STATIC_LIST_PAGE_1 = b"""
<html><head><title>News</title></head><body>
<p><a href="/article.html">First story</a> - 2026-10-21</p>
<a href="/page2.html">2</a>
</body></html>
"""

STATIC_LIST_PAGE_2 = b"""
<html><head><title>News page 2</title></head><body>
<p><a href="/old.html">Old story</a> - 2026-10-19</p>
</body></html>
"""

STATIC_ARTICLE_PAGE = b"""
<html><head><title>First story detail</title></head><body>
<div class="wzxxys">Author: Research Office &nbsp;&nbsp; Time: 2026-10-21 09:10:48</div>
<p>Detailed article body for the shared-fetcher test.</p>
<img src="/image.png">
<a href="/files/report.pdf">Report.pdf</a>
</body></html>
"""

STATIC_IMAGE_BODY = b"\x89PNG\r\n\x1a\n" + b"\x00" * 2048
STATIC_ATTACHMENT_BODY = b"%PDF-1.4\n" + b"attachment data " * 40


class StaticListCollectorFetcherTests(unittest.TestCase):
    def source_config(self) -> dict:
        return {
            "url": STATIC_LIST_URL,
            "fetch_backend": "scrapling_http",
            "cookie": "session=static",
        }

    def make_config(self, root: str) -> AppConfig:
        config = AppConfig()
        config.system.data_dir = root
        config.date_range.start = "2026-10-21"
        config.date_range.end = "2026-10-21"
        return config

    def test_list_pagination_article_media_and_attachment_share_fetcher(self) -> None:
        fetcher = MappingFetcher(
            {
                STATIC_LIST_URL: FakeResponse(
                    200,
                    STATIC_LIST_PAGE_1,
                    {"Content-Type": "text/html; charset=utf-8"},
                    STATIC_LIST_URL,
                ),
                STATIC_PAGE_2_URL: FakeResponse(
                    200,
                    STATIC_LIST_PAGE_2,
                    {"Content-Type": "text/html; charset=utf-8"},
                    STATIC_PAGE_2_URL,
                ),
                STATIC_ARTICLE_URL: FakeResponse(
                    200,
                    STATIC_ARTICLE_PAGE,
                    {"Content-Type": "text/html; charset=utf-8"},
                    STATIC_ARTICLE_URL,
                ),
                STATIC_IMAGE_URL: FakeResponse(
                    200,
                    STATIC_IMAGE_BODY,
                    {"Content-Type": "image/png"},
                    STATIC_IMAGE_URL,
                ),
                STATIC_ATTACHMENT_URL: FakeResponse(
                    200,
                    STATIC_ATTACHMENT_BODY,
                    {"Content-Type": "application/pdf"},
                    STATIC_ATTACHMENT_URL,
                ),
            }
        )
        source = SourceContext(
            id="static-source",
            type="ai_web",
            name="Static source",
            config=self.source_config(),
        )

        with TemporaryDirectory() as root:
            collector = StaticListCollector(self.make_config(root))
            with patch(
                "msgdesk.services.site_crawler.create_fetcher",
                return_value=fetcher,
            ) as factory:
                result = collector.collect(source)

        factory.assert_called_once_with("scrapling_http", timeout=30.0)
        self.assertEqual(
            [call["url"] for call in fetcher.calls],
            [
                STATIC_LIST_URL,
                STATIC_PAGE_2_URL,
                STATIC_ARTICLE_URL,
                STATIC_IMAGE_URL,
                STATIC_ATTACHMENT_URL,
            ],
        )
        for call in fetcher.calls:
            self.assertEqual(call["headers"], {"Cookie": "session=static"})
        self.assertEqual(
            {call["url"]: call["timeout"] for call in fetcher.calls},
            {
                STATIC_LIST_URL: 30.0,
                STATIC_PAGE_2_URL: 30.0,
                STATIC_ARTICLE_URL: 30.0,
                STATIC_IMAGE_URL: 30.0,
                STATIC_ATTACHMENT_URL: 60.0,
            },
        )
        self.assertEqual(fetcher.close_calls, 1)
        self.assertEqual(len(result.messages), 1)
        self.assertEqual(result.messages[0].source_url, STATIC_ARTICLE_URL)
        self.assertEqual(result.messages[0].published_at, "2026-10-21T01:10:48Z")
        self.assertTrue(result.messages[0].published_time_known)
        self.assertIn("/media/2026-10-21/", result.messages[0].content_text)
        self.assertIn("/attachments/2026-10-21/", result.messages[0].content_text)

    def test_static_collector_closes_on_unavailable_without_masking_error(self) -> None:
        fetcher = MappingFetcher(
            {
                STATIC_LIST_URL: FakeResponse(
                    200,
                    b"<html><head><title>No list</title></head><body><p>No dates.</p></body></html>",
                    {"Content-Type": "text/html"},
                    STATIC_LIST_URL,
                )
            },
            close_error=RuntimeError("close failed"),
        )
        source_config = self.source_config()
        source_config.pop("fetch_backend")
        source = SourceContext(
            id="unavailable-source",
            type="ai_web",
            name="Unavailable source",
            config=source_config,
        )

        with TemporaryDirectory() as root:
            collector = StaticListCollector(self.make_config(root))
            with patch(
                "msgdesk.services.site_crawler.create_fetcher",
                return_value=fetcher,
            ) as factory:
                with self.assertRaises(StaticListUnavailable):
                    collector.collect(source)

        factory.assert_called_once_with(None, timeout=30.0)
        self.assertEqual(fetcher.close_calls, 1)

    def test_dependency_error_propagates_without_static_fallback(self) -> None:
        source = SourceContext(
            id="dependency-source",
            type="ai_web",
            name="Dependency source",
            config=self.source_config(),
        )
        dependency_error = AppError(
            "DEPENDENCY_MISSING",
            "missing Scrapling",
            {"dependency": "scrapling[fetchers]==0.5"},
        )

        with TemporaryDirectory() as root:
            collector = StaticListCollector(self.make_config(root))
            with patch(
                "msgdesk.services.site_crawler.create_fetcher",
                side_effect=dependency_error,
            ):
                with self.assertRaises(AppError) as caught:
                    collector.collect(source)

        self.assertIs(caught.exception, dependency_error)
        self.assertEqual(caught.exception.code, "DEPENDENCY_MISSING")
        self.assertNotIsInstance(caught.exception, StaticListUnavailable)


if __name__ == "__main__":
    unittest.main()
