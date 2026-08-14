"""Parse bounded round requests from ordinary collaboration messages."""

from __future__ import annotations

import re
import unicodedata


_MAX_DISCUSSION_ROUNDS = 10
_CHINESE_COUNTS = {
    "一": 1,
    "二": 2,
    "两": 2,
    "三": 3,
    "四": 4,
    "五": 5,
    "六": 6,
    "七": 7,
    "八": 8,
    "九": 9,
    "十": 10,
}
_NUMBER = r"(?:[0-9]+|[零〇一二两三四五六七八九十百]+)"
_ROUND_COUNT = re.compile(
    rf"(?:(?P<before>{_NUMBER})\s*(?:轮|rounds?\b)|"
    rf"(?:轮|\brounds?)\s*(?P<after>{_NUMBER}))",
    re.IGNORECASE,
)


def parse_discussion_round_count(text: str) -> int:
    """Return the one explicit adjacent round count, defaulting to one.

    Width-normalization accepts full-width Arabic digits and Latin text. Repeated
    equal counts are harmless, while conflicting or unsupported counts fail
    before any message persistence occurs.
    """

    normalized = unicodedata.normalize("NFKC", str(text or ""))
    counts: list[int] = []
    for match in _ROUND_COUNT.finditer(normalized):
        token = str(match.group("before") or match.group("after"))
        count = (
            int(token)
            if token.isascii() and token.isdigit()
            else _CHINESE_COUNTS.get(token, _MAX_DISCUSSION_ROUNDS + 1)
        )
        if not 1 <= count <= _MAX_DISCUSSION_ROUNDS:
            raise ValueError("collaboration round count must be between 1 and 10")
        counts.append(count)
    if not counts:
        return 1
    if len(set(counts)) != 1:
        raise ValueError("conflicting collaboration round counts")
    return counts[0]
