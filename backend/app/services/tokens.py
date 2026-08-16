"""Token counting.

tiktoken has no encoding registered for models it predates, so we resolve once
and fall back to a conservative characters-per-token heuristic. Counts here are
used for budgeting, not billing — slight overestimation is the safe direction.
"""

from __future__ import annotations

import logging
from functools import lru_cache

from app.config import settings

logger = logging.getLogger(__name__)

_FALLBACK_CHARS_PER_TOKEN = 3.6  # deliberately low => overestimates tokens


@lru_cache(maxsize=8)
def _encoder(model: str):  # noqa: ANN202 - tiktoken types are not exported
    try:
        import tiktoken
    except ImportError:
        return None
    try:
        return tiktoken.encoding_for_model(model)
    except KeyError:
        try:
            return tiktoken.get_encoding("o200k_base")
        except Exception:  # noqa: BLE001
            return None
    except Exception:  # noqa: BLE001
        return None


def count_tokens(text: str, model: str | None = None) -> int:
    if not text:
        return 0
    enc = _encoder(model or settings.generation_model)
    if enc is None:
        return int(len(text) / _FALLBACK_CHARS_PER_TOKEN) + 1
    return len(enc.encode(text, disallowed_special=()))


def count_message_tokens(messages: list[dict], model: str | None = None) -> int:
    """Approximate chat-format overhead at ~4 tokens per message envelope."""
    total = 0
    for m in messages:
        total += 4
        content = m.get("content")
        if isinstance(content, str):
            total += count_tokens(content, model)
        elif isinstance(content, list):
            for part in content:
                if isinstance(part, dict) and part.get("type") == "text":
                    total += count_tokens(part.get("text", ""), model)
                else:
                    total += 800  # image parts cost roughly this much
        for tc in m.get("tool_calls") or []:
            total += count_tokens(str(tc), model)
    return total


def truncate_to_tokens(text: str, max_tokens: int, model: str | None = None) -> str:
    """Trim to a token budget, cutting on a whitespace boundary where possible."""
    if max_tokens <= 0:
        return ""
    enc = _encoder(model or settings.generation_model)
    if enc is None:
        limit = int(max_tokens * _FALLBACK_CHARS_PER_TOKEN)
        if len(text) <= limit:
            return text
        cut = text[:limit]
        return cut.rsplit(" ", 1)[0] if " " in cut[-40:] else cut
    ids = enc.encode(text, disallowed_special=())
    if len(ids) <= max_tokens:
        return text
    return enc.decode(ids[:max_tokens])
