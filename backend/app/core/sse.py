"""Server-Sent Events helpers for token-by-token streaming."""

from __future__ import annotations

import json
from typing import Any, Literal

EventType = Literal[
    "start",       # stream opened, carries message ids
    "status",      # agent step/progress label for the UI
    "tool_call",   # agent invoked a tool
    "tool_result", # tool returned (truncated payload)
    "sources",     # retrieved + reranked citations
    "token",       # a single generation delta
    "usage",       # token accounting
    "error",       # terminal error
    "done",        # terminal success
]


def sse(event: EventType, data: Any) -> str:
    """Encode one SSE frame. Newlines inside JSON are escaped by json.dumps."""
    return f"event: {event}\ndata: {json.dumps(data, default=str, ensure_ascii=False)}\n\n"


def sse_comment(text: str = "") -> str:
    """Heartbeat frame — keeps ALB/CloudFront idle timeouts from killing the stream."""
    return f": {text}\n\n"
