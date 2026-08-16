"""OpenAI generation client — token-by-token streaming plus tool calling.

Streaming and tool calls arrive on the same channel: the model may emit text
deltas, then start accumulating a tool call across many chunks. `stream_chat`
normalises that into a typed event sequence the agent graph can drive off.
"""

from __future__ import annotations

import json
import logging
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from typing import Any, Literal

from openai import APIError, APIStatusError, AsyncOpenAI, RateLimitError

from app.config import settings

logger = logging.getLogger(__name__)

_client: AsyncOpenAI | None = None


def get_llm() -> AsyncOpenAI:
    global _client
    if _client is None:
        if not settings.openai_api_key:
            raise RuntimeError("OPENAI_API_KEY is not set")
        _client = AsyncOpenAI(
            api_key=settings.openai_api_key,
            base_url=settings.openai_base_url,
            max_retries=settings.http_max_retries,
            timeout=settings.http_timeout_seconds,
        )
    return _client


@dataclass(slots=True)
class ToolCall:
    id: str
    name: str
    arguments: dict[str, Any]


@dataclass(slots=True)
class StreamEvent:
    """One normalised event from the generation stream."""

    type: Literal["token", "tool_calls", "usage", "finish"]
    text: str = ""
    tool_calls: list[ToolCall] = field(default_factory=list)
    usage: dict[str, int] = field(default_factory=dict)
    finish_reason: str | None = None


class _ToolCallAccumulator:
    """Tool-call arguments stream in as JSON fragments across many chunks;
    they must be concatenated per index before parsing."""

    def __init__(self) -> None:
        self._by_index: dict[int, dict[str, str]] = {}

    def add(self, delta_tool_calls: list[Any]) -> None:
        for tc in delta_tool_calls:
            idx = tc.index or 0
            slot = self._by_index.setdefault(idx, {"id": "", "name": "", "args": ""})
            if getattr(tc, "id", None):
                slot["id"] = tc.id
            fn = getattr(tc, "function", None)
            if fn is not None:
                if getattr(fn, "name", None):
                    slot["name"] += fn.name
                if getattr(fn, "arguments", None):
                    slot["args"] += fn.arguments

    def finalize(self) -> list[ToolCall]:
        calls: list[ToolCall] = []
        for idx in sorted(self._by_index):
            slot = self._by_index[idx]
            if not slot["name"]:
                continue
            try:
                args = json.loads(slot["args"]) if slot["args"].strip() else {}
            except json.JSONDecodeError:
                logger.warning("unparseable tool arguments for %s: %r", slot["name"], slot["args"])
                args = {}
            calls.append(ToolCall(id=slot["id"] or f"call_{idx}", name=slot["name"], arguments=args))
        return calls

    def __bool__(self) -> bool:
        return bool(self._by_index)


async def stream_chat(
    messages: list[dict[str, Any]],
    *,
    tools: list[dict[str, Any]] | None = None,
    model: str | None = None,
    temperature: float | None = None,
    max_tokens: int | None = None,
) -> AsyncIterator[StreamEvent]:
    """Yield token deltas as they arrive, then any accumulated tool calls."""
    client = get_llm()
    kwargs: dict[str, Any] = {
        "model": model or settings.generation_model,
        "messages": messages,
        "stream": True,
        "stream_options": {"include_usage": True},
        "temperature": temperature if temperature is not None else settings.generation_temperature,
        "max_completion_tokens": max_tokens or settings.generation_max_output_tokens,
    }
    if tools:
        kwargs["tools"] = tools
        kwargs["tool_choice"] = "auto"

    accumulator = _ToolCallAccumulator()
    finish_reason: str | None = None

    try:
        stream = await client.chat.completions.create(**kwargs)
        async for chunk in stream:
            if chunk.usage is not None:
                yield StreamEvent(
                    type="usage",
                    usage={
                        "prompt_tokens": chunk.usage.prompt_tokens or 0,
                        "completion_tokens": chunk.usage.completion_tokens or 0,
                    },
                )
            if not chunk.choices:
                continue
            choice = chunk.choices[0]
            delta = choice.delta
            if delta is None:
                continue
            if delta.content:
                yield StreamEvent(type="token", text=delta.content)
            if delta.tool_calls:
                accumulator.add(delta.tool_calls)
            if choice.finish_reason:
                finish_reason = choice.finish_reason
    except RateLimitError as exc:
        raise RuntimeError("Generation model rate limit reached. Please retry shortly.") from exc
    except APIStatusError as exc:
        raise RuntimeError(f"Generation model error ({exc.status_code}): {exc.message}") from exc
    except APIError as exc:
        raise RuntimeError(f"Generation model error: {exc}") from exc

    if accumulator:
        yield StreamEvent(type="tool_calls", tool_calls=accumulator.finalize())

    yield StreamEvent(type="finish", finish_reason=finish_reason)


async def complete(
    messages: list[dict[str, Any]],
    *,
    model: str | None = None,
    temperature: float = 0.0,
    max_tokens: int = 1024,
    json_mode: bool = False,
) -> str:
    """Non-streaming single-shot call for utility work (titles, query rewriting,
    memory extraction). Returns "" on failure — never fatal to the main turn."""
    client = get_llm()
    kwargs: dict[str, Any] = {
        "model": model or settings.utility_model,
        "messages": messages,
        "temperature": temperature,
        "max_completion_tokens": max_tokens,
    }
    if json_mode:
        kwargs["response_format"] = {"type": "json_object"}
    try:
        resp = await client.chat.completions.create(**kwargs)
        return (resp.choices[0].message.content or "").strip()
    except (APIError, APIStatusError) as exc:
        logger.warning("utility completion failed: %s", exc)
        return ""
