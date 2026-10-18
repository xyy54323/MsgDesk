"""Keyword matching that ignores simple negated phrases."""

from __future__ import annotations

NEGATION_PREFIXES = ("无需", "不需要", "不用", "免于", "勿")


def has_keyword(text: str, keyword: str) -> bool:
    if not keyword:
        return False
    haystack = text.lower()
    needle = keyword.lower()
    start = 0
    while True:
        index = haystack.find(needle, start)
        if index < 0:
            return False
        prefix = haystack[max(0, index - 8):index]
        negated = prefix.endswith("不") or any(
            marker in prefix[-6:] for marker in NEGATION_PREFIXES
        )
        if not negated:
            return True
        start = index + len(needle)

