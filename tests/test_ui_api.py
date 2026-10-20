from __future__ import annotations

import json
import threading
import unittest
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from tempfile import TemporaryDirectory

from msgdesk.api.server import ApiServer
from msgdesk.config import load_config
from msgdesk.runtime import Runtime
from msgdesk.timeutil import iso_utc


def _request(
    base: str, method: str, path: str, payload: dict | None = None
) -> tuple[int, dict]:
    data = json.dumps(payload).encode("utf-8") if payload is not None else None
    request = urllib.request.Request(
        base + path,
        data=data,
        method=method,
        headers={"Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(request, timeout=5) as response:
            return response.status, json.loads(response.read() or b"{}")
    except urllib.error.HTTPError as exc:
        return exc.code, json.loads(exc.read() or b"{}")


def _seed_message(repo, source_id: str, platform_id: str, text: str) -> str:
    with repo.db.connection() as conn:
        conversation_id = repo.upsert_conversation(
            conn, source_id, "conversation-1", "测试群", "group"
        )
        message_id = repo.insert_message(
            conn,
            {
                "source_id": source_id,
                "conversation_id": conversation_id,
                "platform_message_id": platform_id,
                "published_at": iso_utc(),
                "content_text": text,
                "content_hash": f"hash-{platform_id}",
            },
        )
    return message_id


class UiApiTests(unittest.TestCase):
    def _start(self, root: Path) -> tuple[Runtime, ApiServer, str, str]:
        config_path = root / "config.json"
        config_path.write_text(
            json.dumps(
                {
                    "system": {
                        "data_dir": "./data",
                        "digest_dir": "./digests",
                    },
                    "model": {"provider": ""},
                }
            ),
            encoding="utf-8",
        )
        config = load_config(config_path)
        runtime = Runtime(config)
        server = ApiServer(("127.0.0.1", 0), runtime)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        base = f"http://127.0.0.1:{server.server_address[1]}"
        source = runtime.repo.create_source(
            {
                "type": "ai_web",
                "name": "测试来源",
                "adapter_type": "ai_web",
                "config": {"url": "https://example.com/page"},
            }
        )
        return runtime, server, base, source["id"]

    def test_message_read_hide_and_purge(self):
        with TemporaryDirectory() as tmp:
            runtime, server, base, source_id = self._start(Path(tmp))
            try:
                first = _seed_message(
                    runtime.repo, source_id, "m-1", "请于明天前完成报名"
                )
                second = _seed_message(
                    runtime.repo, source_id, "m-2", "普通技术资讯"
                )
                runtime.repo.override_processing(first, {"priority": "urgent"})
                runtime.repo.override_processing(second, {"priority": "normal"})

                status, message = _request(
                    base, "PATCH", f"/api/messages/{first}", {"is_read": True}
                )
                self.assertEqual(status, 200)
                self.assertEqual(message["is_read"], 1)

                status, result = _request(base, "GET", "/api/messages?unread=1")
                self.assertEqual(status, 200)
                self.assertEqual([m["id"] for m in result["messages"]], [second])

                status, result = _request(base, "GET", "/api/messages?priority=urgent")
                self.assertEqual(status, 200)
                self.assertEqual([m["id"] for m in result["messages"]], [first])

                status, result = _request(
                    base,
                    "GET",
                    "/api/messages?q=" + urllib.parse.quote("报名"),
                )
                self.assertEqual(status, 200)
                self.assertEqual([m["id"] for m in result["messages"]], [first])

                status, result = _request(
                    base, "DELETE", f"/api/messages/{first}?purge=false"
                )
                self.assertEqual(status, 200)
                self.assertFalse(result["purged"])
                self.assertIsNotNone(runtime.repo.get_message(first))
                _, listing = _request(base, "GET", "/api/messages")
                self.assertNotIn(first, [m["id"] for m in listing["messages"]])

                status, result = _request(
                    base, "DELETE", f"/api/messages/{first}?purge=true"
                )
                self.assertEqual(status, 200)
                self.assertTrue(result["purged"])
                self.assertIsNone(runtime.repo.get_message(first))

                status, result = _request(
                    base,
                    "POST",
                    "/api/messages/batch-delete",
                    {"ids": [second], "purge": False},
                )
                self.assertEqual(status, 200)
                self.assertEqual(result["deleted"], 1)
                _, listing = _request(base, "GET", "/api/messages")
                self.assertEqual(listing["messages"], [])
            finally:
                server.shutdown()
                server.server_close()
                runtime.stop()

    def test_profile_round_trip(self):
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            runtime, server, base, _ = self._start(root)
            try:
                status, _ = _request(base, "POST", "/api/profile", {
                    "profile": {
                        "identity": "2026 级研究生",
                        "interests": "奖学金、比赛",
                        "ignore": "行政报送",
                        "todo_rule": "只算需要我本人提交的事",
                        "notes": "导师方向是嵌入式",
                    }
                })
                self.assertEqual(status, 200)
                status, payload = _request(base, "GET", "/api/profile")
                self.assertEqual(status, 200)
                profile = payload["profile"]
                self.assertEqual(profile["identity"], "2026 级研究生")
                self.assertEqual(profile["ignore"], "行政报送")
                saved = json.loads((root / "config.json").read_text(encoding="utf-8"))
                self.assertEqual(saved["profile"]["todo_rule"], "只算需要我本人提交的事")
            finally:
                server.shutdown()
                server.server_close()
                runtime.stop()

    def test_range_round_trip(self):
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            runtime, server, base, _ = self._start(root)
            try:
                status, payload = _request(
                    base,
                    "POST",
                    "/api/range",
                    {"start": "2026-09-01", "end": ""},
                )
                self.assertEqual(status, 200)
                self.assertEqual(payload["range"]["start"], "2026-09-01")
                self.assertEqual(payload["range"]["end"], "")
                self.assertTrue(payload["range"]["effective_end"])
                saved = json.loads((root / "config.json").read_text(encoding="utf-8"))
                self.assertEqual(saved["range"]["start"], "2026-09-01")
                self.assertEqual(saved["range"]["end"], "")

                status, error = _request(
                    base,
                    "POST",
                    "/api/range",
                    {"start": "2026-10-02", "end": "2026-10-01"},
                )
                self.assertEqual(status, 400)
                self.assertEqual(error["code"], "VALIDATION_ERROR")
            finally:
                server.shutdown()
                server.server_close()
                runtime.stop()

    def test_model_settings_are_persisted_and_applied(self):
        with TemporaryDirectory() as tmp:
            runtime, server, base, _ = self._start(Path(tmp))
            try:
                status, saved = _request(
                    base,
                    "POST",
                    "/api/models",
                    {
                        "provider": "openai_compatible",
                        "base_url": "http://127.0.0.1:9/v1",
                        "model": "test-model",
                        "api_key": "secret-model-key",
                        "api_key_env": "CUSTOM_MODEL_KEY",
                        "timeout_seconds": 5,
                    },
                )
                self.assertEqual(status, 200)
                self.assertEqual(saved["provider"], "openai_compatible")
                self.assertNotIn("secret-model-key", json.dumps(saved))
                self.assertEqual(runtime.model.name, "openai_compatible")

                status, current = _request(base, "GET", "/api/models")
                self.assertEqual(status, 200)
                self.assertTrue(current["config"]["api_key_set"])
                self.assertNotIn("secret-model-key", json.dumps(current))

                raw = json.loads(
                    (Path(tmp) / "config.json").read_text(encoding="utf-8")
                )
                self.assertEqual(raw["model"]["api_key"], "secret-model-key")
                self.assertEqual(raw["model"]["model"], "test-model")
                self.assertEqual(raw["model"]["api_key_env"], "CUSTOM_MODEL_KEY")

                status, error = _request(
                    base, "POST", "/api/models", {"provider": "unknown"}
                )
                self.assertEqual(status, 400)
                self.assertEqual(error["code"], "VALIDATION_ERROR")
            finally:
                server.shutdown()
                server.server_close()
                runtime.stop()

    def test_source_masked_secret_is_preserved(self):
        with TemporaryDirectory() as tmp:
            runtime, server, base, _ = self._start(Path(tmp))
            try:
                status, created = _request(
                    base,
                    "POST",
                    "/api/sources",
                    {
                        "type": "qq_onebot",
                        "name": "QQ 测试群",
                        "config": {
                            "base_url": "http://127.0.0.1:3000",
                            "access_token": "source-secret",
                            "selected_conversations": ["1"],
                        },
                    },
                )
                self.assertEqual(status, 201)
                source_id = created["id"]
                self.assertEqual(created["config"]["access_token"], "***")

                status, updated = _request(
                    base,
                    "PATCH",
                    f"/api/sources/{source_id}",
                    {
                        "config": {
                            "base_url": "http://127.0.0.1:3000",
                            "access_token": "***",
                            "selected_conversations": ["1", "2"],
                        }
                    },
                )
                self.assertEqual(status, 200)
                self.assertEqual(updated["config"]["access_token"], "***")
                stored = runtime.repo.get_source(source_id)
                stored_config = json.loads(stored["config_json"])
                self.assertEqual(stored_config["access_token"], "source-secret")
                self.assertEqual(stored_config["selected_conversations"], ["1", "2"])
            finally:
                server.shutdown()
                server.server_close()
                runtime.stop()

    def test_removed_non_mvp_routes_return_404(self):
        with TemporaryDirectory() as tmp:
            runtime, server, base, _ = self._start(Path(tmp))
            try:
                for path in (
                    "/api/notifications",
                    "/api/notification-channels",
                    "/api/rules",
                    "/api/jobs",
                    "/api/traces",
                ):
                    status, payload = _request(base, "GET", path)
                    self.assertEqual(status, 404)
                    self.assertEqual(payload["code"], "NOT_FOUND")
            finally:
                server.shutdown()
                server.server_close()
                runtime.stop()


if __name__ == "__main__":
    unittest.main()
