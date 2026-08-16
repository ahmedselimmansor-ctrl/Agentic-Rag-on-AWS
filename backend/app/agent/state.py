"""Graph state and the event contract between the agent and the SSE layer."""

from __future__ import annotations

import operator
import uuid
from typing import Annotated, Any, TypedDict

from app.services.retrieval import RetrievedChunk


def _last(a: Any, b: Any) -> Any:
    """Reducer: later writes win."""
    return b if b is not None else a


class AgentState(TypedDict, total=False):
    # ---- request ----------------------------------------------------------
    user_id: uuid.UUID
    conversation_id: uuid.UUID
    question: str
    attachments: list[dict]
    web_enabled: bool

    # ---- assembled context ------------------------------------------------
    search_query: Annotated[str, _last]
    history: list[Any]
    summary: str | None
    memory_block: str

    # ---- evidence (accumulates across tool rounds) ------------------------
    chunks: Annotated[list[RetrievedChunk], operator.add]
    web_results: Annotated[list[dict], operator.add]

    # ---- generation -------------------------------------------------------
    llm_messages: Annotated[list[dict], _last]
    answer: Annotated[str, _last]
    sources: Annotated[list[dict], _last]
    pending_tool_calls: Annotated[list[Any], _last]
    tool_trace: Annotated[list[dict], operator.add]

    # ---- control ----------------------------------------------------------
    step: Annotated[int, operator.add]
    prompt_tokens: Annotated[int, _last]
    completion_tokens: Annotated[int, _last]
    error: Annotated[str | None, _last]


def initial_state(
    *,
    user_id: uuid.UUID,
    conversation_id: uuid.UUID,
    question: str,
    attachments: list[dict] | None = None,
    web_enabled: bool = False,
) -> AgentState:
    return AgentState(
        user_id=user_id,
        conversation_id=conversation_id,
        question=question,
        attachments=attachments or [],
        web_enabled=web_enabled,
        search_query=question,
        history=[],
        summary=None,
        memory_block="",
        chunks=[],
        web_results=[],
        llm_messages=[],
        answer="",
        sources=[],
        pending_tool_calls=[],
        tool_trace=[],
        step=0,
        prompt_tokens=0,
        completion_tokens=0,
        error=None,
    )


def emit(writer, event: str, **data: Any) -> None:
    """Push one custom-stream event. `writer` is LangGraph's injected StreamWriter."""
    if writer is not None:
        writer({"event": event, **data})
