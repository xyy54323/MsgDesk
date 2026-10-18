"""Deterministic local rules applied before and after model processing."""

from __future__ import annotations

import json
from typing import Any

from ..repository import loads
from .textmatch import has_keyword


class RuleEngine:
    def __init__(self, rules: list[dict[str, Any]]):
        self.rules = rules

    def apply(self, *, source_id: str, content_type: str, text: str) -> dict[str, Any]:
        result: dict[str, Any] = {
            "matched": [],
            "category": None,
            "priority": None,
            "urgency": None,
            "tags": [],
            "notify": None,
            "ignore": False,
        }
        lowered = text.lower()
        for rule in self.rules:
            if not rule.get("enabled", 1):
                continue
            condition = loads(rule.get("condition_json"), {}) or {}
            if not self._scope_matches(rule, source_id):
                continue
            if not self._condition_matches(condition, source_id, content_type, text, lowered):
                continue
            action = loads(rule.get("action_json"), {}) or {}
            result["matched"].append(rule["name"])
            for key in ("category", "priority", "urgency"):
                if action.get(key):
                    result[key] = action[key]
            if action.get("tags"):
                result["tags"].extend(action["tags"])
            if "notify" in action:
                result["notify"] = bool(action["notify"])
            if action.get("ignore"):
                result["ignore"] = True
        result["tags"] = list(dict.fromkeys(result["tags"]))
        return result

    @staticmethod
    def _scope_matches(rule: dict[str, Any], source_id: str) -> bool:
        scope_type = rule.get("scope_type", "global")
        if scope_type == "global":
            return True
        return scope_type == "source" and rule.get("scope_id") == source_id

    @staticmethod
    def _condition_matches(
        condition: dict[str, Any], source_id: str, content_type: str,
        text: str, lowered: str,
    ) -> bool:
        if condition.get("source_ids") and source_id not in condition["source_ids"]:
            return False
        if condition.get("content_types") and content_type not in condition["content_types"]:
            return False
        if condition.get("min_chars") and len(text) < int(condition["min_chars"]):
            return False
        exclude = condition.get("exclude_keywords") or []
        if any(has_keyword(lowered, keyword) for keyword in exclude):
            return False
        keywords = condition.get("keywords") or []
        if keywords and not any(has_keyword(lowered, keyword) for keyword in keywords):
            return False
        return True

