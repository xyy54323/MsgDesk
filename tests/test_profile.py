import json
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace

from msgdesk.config import load_config, merge_config_file
from msgdesk.services.profile_prompt import profile_prompt


class ProfileTests(unittest.TestCase):
    def test_profile_is_loaded_and_saved(self):
        with TemporaryDirectory() as tmp:
            path = Path(tmp) / "config.json"
            path.write_text(json.dumps({"profile": {"identity": "研究生"}}), encoding="utf-8")
            config = load_config(path)
            self.assertEqual(config.profile.identity, "研究生")
            self.assertEqual(config.profile.todo_rule, "")

            merge_config_file(path, {"profile": {"todo_rule": "只算我的事"}})
            raw = json.loads(path.read_text(encoding="utf-8"))
            self.assertEqual(raw["profile"]["identity"], "研究生")
            self.assertEqual(raw["profile"]["todo_rule"], "只算我的事")

    def test_prompt_renders_fields_and_todo_rule(self):
        profile = SimpleNamespace(
            identity="2026 级研究生",
            interests="奖学金、比赛",
            ignore="行政报送",
            todo_rule="",
            notes="",
        )
        text = profile_prompt(profile)
        self.assertIn("2026 级研究生", text)
        self.assertIn("奖学金、比赛", text)
        self.assertIn("行政报送", text)
        self.assertIn("只有需要我本人行动", text)

    def test_empty_profile_renders_nothing(self):
        profile = SimpleNamespace(
            identity="", interests="", ignore="", todo_rule="", notes=""
        )
        self.assertEqual(profile_prompt(profile), "")


if __name__ == "__main__":
    unittest.main()
