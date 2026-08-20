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
from app.services.resilience import CircuitOpen, get_breaker

logger = logging.getLogger(__name__)

GENERATION_BREAKER = "generation"

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
class Citation:
    """A source the model's own hosted web search consulted."""

    url: str
    title: str = ""
    snippet: str = ""


@dataclass(slots=True)
class StreamEvent:
    """One normalised event from the generation stream.

    Both the Chat Completions and Responses paths emit this same shape, so the
    agent graph does not care which API produced the tokens.
    """

    type: Literal["token", "tool_calls", "usage", "finish", "hosted_tool", "citations"]
    text: str = ""
    tool_calls: list[ToolCall] = field(default_factory=list)
    usage: dict[str, int] = field(default_factory=dict)
    finish_reason: str | None = None
    # For `hosted_tool`: the name of a tool the model ran server-side.
    tool_name: str = ""
    # For `citations`: sources attached to the answer by hosted web search.
    citations: list[Citation] = field(default_factory=list)


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

    # Gate on entry and report the outcome below: a streaming generator
    # outlives the call that created it, so breaker.call() cannot wrap it.
    breaker = get_breaker(GENERATION_BREAKER)
    try:
        await breaker.acquire()
    except CircuitOpen as exc:
        raise RuntimeError(str(exc)) from exc

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
        # Rate limiting is the provider working, not failing — tripping the
        # breaker on it would take generation down during a traffic spike.
        await breaker.record_success()
        raise RuntimeError("Generation model rate limit reached. Please retry shortly.") from exc
    except APIStatusError as exc:
        # 4xx is our bad request; only 5xx counts against the provider.
        if exc.status_code >= 500:
            await breaker.record_failure()
        else:
            await breaker.record_success()
        raise RuntimeError(f"Generation model error ({exc.status_code}): {exc.message}") from exc
    except APIError as exc:
        await breaker.record_failure()
        raise RuntimeError(f"Generation model error: {exc}") from exc

    await breaker.record_success()

    if accumulator:
        yield StreamEvent(type="tool_calls", tool_calls=accumulator.finalize())

    yield StreamEvent(type="finish", finish_reason=finish_reason)


# ===================================================== Responses API path ===
# OpenAI's *hosted* tools — web search among them — live on the Responses API,
# not on Chat Completions. When the model does its own searching we switch to
# this path; everything else keeps using Chat Completions.


def to_responses_input(messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Translate Chat-Completions-shaped messages into Responses `input` items.

    The agent graph builds one message list and both APIs consume it, so the
    tool-call loop does not have to know which path it is on.
    """
    items: list[dict[str, Any]] = []

    for message in messages:
        role = message.get("role")

        if role == "tool":
            items.append(
                {
                    "type": "function_call_output",
                    "call_id": message.get("tool_call_id", ""),
                    "output": str(message.get("content") or ""),
                }
            )
            continue

        if role == "assistant" and message.get("tool_calls"):
            if message.get("content"):
                items.append(
                    {
                        "role": "assistant",
                        "content": [{"type": "output_text", "text": message["content"]}],
                    }
                )
            for call in message["tool_calls"]:
                fn = call.get("function", {})
                items.append(
                    {
                        "type": "function_call",
                        "call_id": call.get("id", ""),
                        "name": fn.get("name", ""),
                        "arguments": fn.get("arguments", "{}"),
                    }
                )
            continue

        content = message.get("content")
        if content is None:
            continue

        # Assistant text is `output_text`; everything inbound is `input_text`.
        text_type = "output_text" if role == "assistant" else "input_text"

        if isinstance(content, str):
            parts: list[dict[str, Any]] = [{"type": text_type, "text": content}]
        else:
            parts = []
            for part in content:
                if part.get("type") == "image_url":
                    url = (part.get("image_url") or {}).get("url")
                    if url:
                        parts.append({"type": "input_image", "image_url": url})
                elif part.get("type") == "text":
                    parts.append({"type": text_type, "text": part.get("text", "")})

        if parts:
            items.append({"role": role, "content": parts})

    return items


def to_responses_tools(tools: list[dict[str, Any]] | None) -> list[dict[str, Any]]:
    """Chat Completions nests the schema under `function`; Responses flattens it."""
    out: list[dict[str, Any]] = []
    for tool in tools or []:
        fn = tool.get("function")
        if not fn:
            out.append(tool)  # already flat, or a hosted tool
            continue
        out.append(
            {
                "type": "function",
                "name": fn["name"],
                "description": fn.get("description", ""),
                "parameters": fn.get("parameters", {}),
            }
        )
    return out


def hosted_web_search_tool() -> dict[str, Any]:
    """The model's built-in web search. The tool type is configurable because
    providers rename these (`web_search`, `web_search_preview`, …) and a wrong
    name is a 400 rather than a silent no-op."""
    tool: dict[str, Any] = {"type": settings.openai_web_search_tool}
    if settings.openai_web_search_context_size:
        tool["search_context_size"] = settings.openai_web_search_context_size
    return tool


class _ResponsesToolAccumulator:
    """Responses streams function-call arguments per output item."""

    def __init__(self) -> None:
        self._items: dict[str, dict[str, str]] = {}

    def start(self, item_id: str, call_id: str, name: str) -> None:
        self._items[item_id] = {"call_id": call_id, "name": name, "args": ""}

    def add_arguments(self, item_id: str, delta: str) -> None:
        slot = self._items.setdefault(item_id, {"call_id": "", "name": "", "args": ""})
        slot["args"] += delta

    def complete(self, item_id: str, call_id: str, name: str, arguments: str) -> None:
        slot = self._items.setdefault(item_id, {"call_id": "", "name": "", "args": ""})
        slot["call_id"] = call_id or slot["call_id"]
        slot["name"] = name or slot["name"]
        if arguments:
            slot["args"] = arguments

    def finalize(self) -> list[ToolCall]:
        calls: list[ToolCall] = []
        for item_id, slot in self._items.items():
            if not slot["name"]:
                continue
            try:
                args = json.loads(slot["args"]) if slot["args"].strip() else {}
            except json.JSONDecodeError:
                logger.warning("unparseable tool arguments for %s: %r", slot["name"], slot["args"])
                args = {}
            calls.append(
                ToolCall(id=slot["call_id"] or item_id, name=slot["name"], arguments=args)
            )
        return calls

    def __bool__(self) -> bool:
        return bool(self._items)


def _extract_citation(annotation: Any) -> Citation | None:
    kind = getattr(annotation, "type", None) or (
        annotation.get("type") if isinstance(annotation, dict) else None
    )
    if kind != "url_citation":
        return None

    def get(key: str) -> str:
        if isinstance(annotation, dict):
            return str(annotation.get(key) or "")
        return str(getattr(annotation, key, "") or "")

    url = get("url")
    return Citation(url=url, title=get("title")) if url else None


async def stream_responses(
    messages: list[dict[str, Any]],
    *,
    tools: list[dict[str, Any]] | None = None,
    web_search: bool = False,
    model: str | None = None,
    temperature: float | None = None,
    max_tokens: int | None = None,
) -> AsyncIterator[StreamEvent]:
    """Stream from the Responses API, optionally with hosted web search.

    Hosted search runs entirely server-side: the model decides to search, runs
    it, reads the results, and keeps generating. There is no round-trip back to
    us, so those searches surface as `hosted_tool` and `citations` events rather
    than as tool calls we have to execute.
    """
    client = get_llm()

    request_tools = to_responses_tools(tools)
    if web_search:
        request_tools.insert(0, hosted_web_search_tool())

    kwargs: dict[str, Any] = {
        "model": model or settings.generation_model,
        "input": to_responses_input(messages),
        "stream": True,
        "temperature": temperature if temperature is not None else settings.generation_temperature,
        "max_output_tokens": max_tokens or settings.generation_max_output_tokens,
        # Without this the API retains the response server-side for 30 days.
        "store": False,
    }
    if request_tools:
        kwargs["tools"] = request_tools
        kwargs["tool_choice"] = "auto"

    accumulator = _ResponsesToolAccumulator()
    citations: dict[str, Citation] = {}
    usage: dict[str, int] = {}
    finish_reason: str | None = None

    # Gate on entry and report the outcome below: a streaming generator
    # outlives the call that created it, so breaker.call() cannot wrap it.
    breaker = get_breaker(GENERATION_BREAKER)
    try:
        await breaker.acquire()
    except CircuitOpen as exc:
        raise RuntimeError(str(exc)) from exc

    try:
        stream = await client.responses.create(**kwargs)

        async for event in stream:
            kind = getattr(event, "type", "")

            if kind == "response.output_text.delta":
                delta = getattr(event, "delta", "")
                if delta:
                    yield StreamEvent(type="token", text=delta)

            elif kind == "response.output_text.annotation.added":
                citation = _extract_citation(getattr(event, "annotation", None))
                if citation:
                    citations[citation.url] = citation

            elif kind == "response.output_item.added":
                item = getattr(event, "item", None)
                item_type = getattr(item, "type", "")
                if item_type == "function_call":
                    accumulator.start(
                        getattr(item, "id", "") or "",
                        getattr(item, "call_id", "") or "",
                        getattr(item, "name", "") or "",
                    )
                elif item_type.endswith("_call"):
                    # A hosted tool the model is running server-side.
                    yield StreamEvent(
                        type="hosted_tool", tool_name=item_type.removesuffix("_call")
                    )

            elif kind == "response.function_call_arguments.delta":
                accumulator.add_arguments(
                    getattr(event, "item_id", "") or "", getattr(event, "delta", "") or ""
                )

            elif kind == "response.output_item.done":
                item = getattr(event, "item", None)
                if getattr(item, "type", "") == "function_call":
                    accumulator.complete(
                        getattr(item, "id", "") or "",
                        getattr(item, "call_id", "") or "",
                        getattr(item, "name", "") or "",
                        getattr(item, "arguments", "") or "",
                    )
                # Annotations may only appear on the completed message item.
                for content in getattr(item, "content", None) or []:
                    for annotation in getattr(content, "annotations", None) or []:
                        citation = _extract_citation(annotation)
                        if citation:
                            citations.setdefault(citation.url, citation)

            elif kind in {"response.completed", "response.incomplete"}:
                response = getattr(event, "response", None)
                token_usage = getattr(response, "usage", None)
                if token_usage is not None:
                    usage = {
                        "prompt_tokens": getattr(token_usage, "input_tokens", 0) or 0,
                        "completion_tokens": getattr(token_usage, "output_tokens", 0) or 0,
                    }
                finish_reason = "stop" if kind == "response.completed" else "length"

            elif kind == "response.failed":
                response = getattr(event, "response", None)
                error = getattr(response, "error", None)
                message = getattr(error, "message", None) or "the model reported a failure"
                raise RuntimeError(f"Generation failed: {message}")

    except RateLimitError as exc:
        # Rate limiting is the provider working, not failing — tripping the
        # breaker on it would take generation down during a traffic spike.
        await breaker.record_success()
        raise RuntimeError("Generation model rate limit reached. Please retry shortly.") from exc
    except APIStatusError as exc:
        detail = getattr(exc, "message", str(exc))
        if web_search and exc.status_code == 400:
            # Almost always an unsupported hosted-tool type for this model.
            raise RuntimeError(
                f"The model rejected built-in web search ({detail}). "
                f"Check OPENAI_WEB_SEARCH_TOOL — it is currently "
                f"'{settings.openai_web_search_tool}' — or set "
                "WEB_SEARCH_PROVIDER=tavily to use an external provider."
            ) from exc
        if exc.status_code >= 500:
            await breaker.record_failure()
        else:
            await breaker.record_success()
        raise RuntimeError(f"Generation model error ({exc.status_code}): {detail}") from exc
    except APIError as exc:
        await breaker.record_failure()
        raise RuntimeError(f"Generation model error: {exc}") from exc

    await breaker.record_success()

    if citations:
        yield StreamEvent(type="citations", citations=list(citations.values()))
    if usage:
        yield StreamEvent(type="usage", usage=usage)
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
