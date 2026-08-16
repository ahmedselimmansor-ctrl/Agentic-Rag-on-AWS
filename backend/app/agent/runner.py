"""Bridges the LangGraph run to the SSE response.

The graph runs as a background task publishing to a queue; this generator drains
the queue and yields SSE frames. That decoupling is what lets tokens reach the
browser the moment the model produces them, while persistence and memory
extraction happen after the stream closes rather than blocking it.
"""

from __future__ import annotations

import asyncio
import logging
import time
import uuid
from collections.abc import AsyncIterator
from typing import Any

from sqlalchemy import func, select, update

from app.agent.graph import get_graph
from app.agent.state import initial_state
from app.config import settings
from app.core.sse import sse, sse_comment
from app.db.models import Conversation, Message, Role, ToolInvocation
from app.db.session import session_scope
from app.services import memory as memory_service

logger = logging.getLogger(__name__)

_SENTINEL = object()
HEARTBEAT_SECONDS = 15.0


async def run_turn(
    *,
    user_id: uuid.UUID,
    conversation_id: uuid.UUID,
    question: str,
    attachments: list[dict] | None = None,
    web_enabled: bool = False,
) -> AsyncIterator[str]:
    started = time.perf_counter()
    queue: asyncio.Queue = asyncio.Queue(maxsize=512)

    async with session_scope() as session:
        user_message = await _persist_user_message(
            session, conversation_id, question, attachments or []
        )
        assistant_ordinal = user_message.ordinal + 1
        is_first_turn = user_message.ordinal == 1

    assistant_id = uuid.uuid4()
    yield sse(
        "start",
        {
            "conversation_id": str(conversation_id),
            "user_message_id": str(user_message.id),
            "assistant_message_id": str(assistant_id),
        },
    )

    state = initial_state(
        user_id=user_id,
        conversation_id=conversation_id,
        question=question,
        attachments=attachments,
        web_enabled=web_enabled,
    )

    final_state: dict[str, Any] = {}
    graph_error: str | None = None

    async def drive() -> None:
        nonlocal final_state, graph_error
        # A dedicated session: the graph outlives any request-scoped session.
        try:
            async with session_scope() as graph_session:
                final_state = await get_graph().ainvoke(
                    state,
                    config={
                        "configurable": {"session": graph_session, "queue": queue},
                        "recursion_limit": settings.agent_recursion_limit,
                    },
                )
        except Exception as exc:  # noqa: BLE001
            logger.exception("agent run failed")
            graph_error = str(exc)
        finally:
            await queue.put(_SENTINEL)

    task = asyncio.create_task(drive())

    try:
        while True:
            try:
                item = await asyncio.wait_for(queue.get(), timeout=HEARTBEAT_SECONDS)
            except TimeoutError:
                # Keeps ALB / CloudFront idle timers from tearing down the stream.
                yield sse_comment("keepalive")
                continue

            if item is _SENTINEL:
                break

            event = item.pop("event")
            yield sse(event, item)

    except asyncio.CancelledError:
        # Client disconnected mid-stream — stop the graph rather than let it run on.
        task.cancel()
        raise
    finally:
        if not task.done():
            await task

    if graph_error:
        yield sse("error", {"message": _friendly_error(graph_error)})
        await _persist_error(conversation_id, assistant_id, assistant_ordinal, graph_error)
        return

    answer = (final_state.get("answer") or "").strip()
    sources = final_state.get("sources") or []
    tool_trace = final_state.get("tool_trace") or []
    state_error = final_state.get("error")

    if state_error and not answer:
        yield sse("error", {"message": _friendly_error(state_error)})
        await _persist_error(conversation_id, assistant_id, assistant_ordinal, state_error)
        return

    latency_ms = int((time.perf_counter() - started) * 1000)
    usage = {
        "prompt_tokens": final_state.get("prompt_tokens", 0),
        "completion_tokens": final_state.get("completion_tokens", 0),
        "latency_ms": latency_ms,
        "steps": final_state.get("step", 0),
    }
    yield sse("usage", usage)

    await _persist_assistant_message(
        conversation_id=conversation_id,
        message_id=assistant_id,
        ordinal=assistant_ordinal,
        content=answer,
        sources=sources,
        tool_trace=tool_trace,
        usage=usage,
    )

    title = None
    if is_first_turn:
        async with session_scope() as session:
            title = await memory_service.generate_title(session, conversation_id, question)

    yield sse(
        "done",
        {
            "message_id": str(assistant_id),
            "sources": sources,
            "tool_trace": tool_trace,
            "title": title,
            "usage": usage,
        },
    )

    # Post-turn upkeep. Failures here never reach the user.
    await _post_turn(
        user_id=user_id,
        conversation_id=conversation_id,
        question=question,
        answer=answer,
    )


# ============================================================ persistence ===
async def _persist_user_message(
    session, conversation_id: uuid.UUID, content: str, attachments: list[dict]
) -> Message:
    next_ordinal = (
        await session.execute(
            select(func.coalesce(func.max(Message.ordinal), 0) + 1).where(
                Message.conversation_id == conversation_id
            )
        )
    ).scalar_one()

    message = Message(
        conversation_id=conversation_id,
        ordinal=next_ordinal,
        role=Role.user,
        content=content,
        attachments_json=attachments,
    )
    session.add(message)
    await session.execute(
        update(Conversation)
        .where(Conversation.id == conversation_id)
        .values(last_message_at=func.now())
    )
    await session.flush()
    return message


async def _persist_assistant_message(
    *,
    conversation_id: uuid.UUID,
    message_id: uuid.UUID,
    ordinal: int,
    content: str,
    sources: list[dict],
    tool_trace: list[dict],
    usage: dict,
) -> None:
    try:
        async with session_scope() as session:
            session.add(
                Message(
                    id=message_id,
                    conversation_id=conversation_id,
                    ordinal=ordinal,
                    role=Role.assistant,
                    content=content,
                    sources_json=sources,
                    tool_calls_json=tool_trace,
                    prompt_tokens=usage.get("prompt_tokens", 0),
                    completion_tokens=usage.get("completion_tokens", 0),
                    latency_ms=usage.get("latency_ms", 0),
                )
            )
            for entry in tool_trace:
                session.add(
                    ToolInvocation(
                        message_id=message_id,
                        conversation_id=conversation_id,
                        tool_name=entry.get("tool", "unknown"),
                        arguments_json=entry.get("arguments") or {},
                        result_json={"result_count": entry.get("result_count", 0)},
                        ok=bool(entry.get("ok", True)),
                        error=entry.get("error"),
                        duration_ms=entry.get("duration_ms", 0),
                    )
                )
            await session.execute(
                update(Conversation)
                .where(Conversation.id == conversation_id)
                .values(last_message_at=func.now())
            )
    except Exception:  # noqa: BLE001 - the user already has the answer
        logger.exception("failed to persist assistant message %s", message_id)


async def _persist_error(
    conversation_id: uuid.UUID, message_id: uuid.UUID, ordinal: int, error: str
) -> None:
    try:
        async with session_scope() as session:
            session.add(
                Message(
                    id=message_id,
                    conversation_id=conversation_id,
                    ordinal=ordinal,
                    role=Role.assistant,
                    content="",
                    error=error[:2000],
                )
            )
    except Exception:  # noqa: BLE001
        logger.exception("failed to persist error message")


async def _post_turn(
    *, user_id: uuid.UUID, conversation_id: uuid.UUID, question: str, answer: str
) -> None:
    """Summarisation + long-term memory extraction, after the response closes."""
    if not answer:
        return
    try:
        async with session_scope() as session:
            await memory_service.maybe_summarize(session, conversation_id)
            await memory_service.extract_long_term(
                session,
                user_id=user_id,
                conversation_id=conversation_id,
                user_text=question,
                assistant_text=answer,
            )
    except Exception:  # noqa: BLE001
        logger.exception("post-turn memory upkeep failed")


def _friendly_error(raw: str) -> str:
    lowered = raw.lower()
    if "rate limit" in lowered:
        return "The model is rate limited right now. Please try again in a moment."
    if "api_key" in lowered or "api key" in lowered:
        return "A model provider API key is missing or invalid. Check the server configuration."
    if "timeout" in lowered or "timed out" in lowered:
        return "The request timed out. Try again, or narrow the question."
    return f"Something went wrong while answering: {raw[:300]}"
