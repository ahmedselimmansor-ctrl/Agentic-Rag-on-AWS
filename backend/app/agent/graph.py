"""The agentic RAG graph.

    prepare -> retrieve -> generate -+-> tools -+
                              ^                 |
                              +-----------------+
                                        |
                                       END

`prepare` resolves the question into a standalone query and loads memory.
`retrieve` does one grounded pass up front, so the common single-hop question
never pays for a tool round-trip. `generate` streams; if the model decides the
context is insufficient it emits tool calls, `tools` executes them, and control
returns to `generate` with the results appended. The loop is bounded by
`agent_max_steps`.

Streaming note: nodes publish to an asyncio.Queue supplied through the run
config rather than LangGraph's custom-stream writer. The queue gives us true
token-level delivery with backpressure, and keeps the SSE contract independent
of the LangGraph version in use.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

from langchain_core.runnables import RunnableConfig
from langgraph.graph import END, START, StateGraph
from sqlalchemy.ext.asyncio import AsyncSession

from app.agent.prompts import QUERY_REWRITE_PROMPT
from app.agent.state import AgentState
from app.config import settings
from app.services import memory as memory_service
from app.services.context import build_messages
from app.services.llm import Citation, ToolCall, complete, stream_chat, stream_responses
from app.services.resilience import TurnBudget
from app.services.retrieval import retrieve as retrieve_chunks
from app.tools import registry

logger = logging.getLogger(__name__)


class Emitter:
    """Publishes agent events to the SSE layer."""

    def __init__(self, queue: asyncio.Queue | None) -> None:
        self._queue = queue

    async def emit(self, event: str, **data: Any) -> None:
        if self._queue is not None:
            await self._queue.put({"event": event, **data})


def _ctx(config: RunnableConfig) -> tuple[AsyncSession, Emitter, TurnBudget]:
    configurable = (config or {}).get("configurable", {})
    session = configurable.get("session")
    if session is None:
        raise RuntimeError("graph config must provide a database session")
    # An unbudgeted turn is still bounded by agent_max_steps; the budget adds
    # token and wall-clock ceilings a step count cannot express.
    budget = configurable.get("budget") or TurnBudget(max_steps=settings.agent_max_steps)
    return session, Emitter(configurable.get("queue")), budget


# ================================================================ prepare ===
async def prepare(state: AgentState, config: RunnableConfig) -> dict:
    session, emitter, budget = _ctx(config)
    await emitter.emit("status", label="Reading conversation")

    short_term = await memory_service.load_short_term(session, state["conversation_id"])

    # Recall and query-rewriting are independent; run them together.
    memories, search_query = await asyncio.gather(
        memory_service.recall_long_term(
            session, user_id=state["user_id"], query=state["question"]
        ),
        _rewrite_query(state["question"], short_term.recent),
    )

    memory_block = memory_service.render_memories(memories)
    if memory_block:
        await emitter.emit("status", label=f"Recalled {len(memories)} memories")

    return {
        "history": short_term.recent,
        "summary": short_term.summary,
        "memory_block": memory_block,
        "search_query": search_query,
    }


async def _rewrite_query(question: str, history: list) -> str:
    """A follow-up like "what about the second one?" is unsearchable on its own.
    Rewriting it against the recent turns is what makes multi-turn RAG work."""
    if not history:
        return question
    # Short, self-contained questions rarely need rewriting — skip the round-trip.
    if len(question.split()) > 12 and "?" in question:
        return question

    transcript = "\n".join(
        f"{m.role.value.upper()}: {m.content[:600]}" for m in history[-6:]
    )
    rewritten = await complete(
        [
            {
                "role": "user",
                "content": QUERY_REWRITE_PROMPT % {"history": transcript, "question": question},
            }
        ],
        temperature=0.0,
        max_tokens=120,
    )
    cleaned = (rewritten or "").strip().strip('"')
    # Guard against a runaway rewrite replacing the question with an essay.
    return cleaned if 0 < len(cleaned) <= 400 else question


# =============================================================== retrieve ===
async def retrieve_node(state: AgentState, config: RunnableConfig) -> dict:
    session, emitter, budget = _ctx(config)
    query = state.get("search_query") or state["question"]
    await emitter.emit("status", label="Searching documents")

    try:
        chunks = await retrieve_chunks(
            session,
            query,
            user_id=state["user_id"],
            conversation_id=state["conversation_id"],
        )
    except Exception as exc:  # noqa: BLE001 - answer ungrounded rather than fail
        logger.exception("initial retrieval failed")
        await emitter.emit("status", label="Document search unavailable")
        return {"chunks": [], "error": None, "tool_trace": [
            {"tool": "retrieve", "ok": False, "error": str(exc)[:300]}
        ]}

    await emitter.emit(
        "status",
        label=f"Found {len(chunks)} relevant passage{'s' if len(chunks) != 1 else ''}"
        if chunks
        else "No matching passages",
    )
    return {"chunks": chunks}


# =============================================================== generate ===
async def generate(state: AgentState, config: RunnableConfig) -> dict:
    session, emitter, budget = _ctx(config)

    messages = state.get("llm_messages") or []
    sources = state.get("sources") or []

    if not messages:
        assembled = build_messages(
            question=state["question"],
            chunks=state.get("chunks", []),
            history=state.get("history", []),
            summary=state.get("summary"),
            memory_block=state.get("memory_block", ""),
            attachments=state.get("attachments"),
            web_results=state.get("web_results", []),
        )
        messages = assembled.messages
        sources = assembled.sources
        if sources:
            await emitter.emit("sources", sources=sources)
        if assembled.dropped_passages:
            logger.info("context budget dropped %d passages", assembled.dropped_passages)

    has_documents = bool(state.get("chunks")) or state.get("step", 0) == 0
    tools = registry.available_tools(
        web_enabled=bool(state.get("web_enabled")), has_documents=has_documents
    )
    # Out of budget: force a final answer by withholding the tools. Stopping
    # this way yields a real answer from what we already have, rather than an
    # error the user has to read.
    budget.steps_used = state.get("step", 0)
    exhausted = budget.exhausted()
    if exhausted:
        logger.info("turn hit its %s budget; forcing a final answer", exhausted)
        await emitter.emit("status", label="Wrapping up")
        tools = []

    # Native search runs inside the model's own turn, so it must be requested on
    # this call rather than executed by us between calls.
    native_search = registry.uses_native_web_search() and bool(state.get("web_enabled"))

    answer_parts: list[str] = []
    tool_calls: list[ToolCall] = []
    usage: dict[str, int] = {}
    citations: list[Citation] = []

    if native_search:
        stream = stream_responses(messages, tools=tools or None, web_search=True)
    else:
        stream = stream_chat(messages, tools=tools or None)

    try:
        async for event in stream:
            if event.type == "token":
                answer_parts.append(event.text)
                await emitter.emit("token", text=event.text)
            elif event.type == "tool_calls":
                tool_calls = event.tool_calls
            elif event.type == "usage":
                usage = event.usage
                budget.add_tokens(
                    usage.get("prompt_tokens", 0), usage.get("completion_tokens", 0)
                )
            elif event.type == "hosted_tool":
                # The model searched server-side; surface it in the same trace
                # the UI already renders for tools we run ourselves.
                await emitter.emit("tool_call", name=event.tool_name, arguments={})
            elif event.type == "citations":
                citations = event.citations
    except RuntimeError as exc:
        # Recorded in state, not emitted: the runner owns the single terminal
        # `error` frame so the client never sees the failure twice.
        logger.warning("generation failed: %s", exc)
        return {"error": str(exc), "answer": "".join(answer_parts), "sources": sources}

    answer = "".join(answer_parts)

    trace: list[dict] = []
    if citations:
        sources = _append_citations(sources, citations)
        await emitter.emit("sources", sources=sources)
        trace.append(
            {
                "tool": "web_search",
                "arguments": {"native": True},
                "ok": True,
                "result_count": len(citations),
            }
        )

    return {
        "llm_messages": messages,
        "answer": (state.get("answer") or "") + answer if tool_calls else answer or state.get("answer", ""),
        "sources": sources,
        "pending_tool_calls": tool_calls,
        "tool_trace": trace,
        "prompt_tokens": usage.get("prompt_tokens", state.get("prompt_tokens", 0)),
        "completion_tokens": state.get("completion_tokens", 0) + usage.get("completion_tokens", 0),
        "step": 1,
    }


def _append_citations(sources: list[dict], citations: list[Citation]) -> list[dict]:
    """Fold hosted-search citations into the same numbered source list the
    document passages use, so the UI renders both identically."""
    merged = list(sources)
    seen = {s.get("url") for s in merged if s.get("url")}
    next_index = max((s.get("index", 0) for s in merged), default=0) + 1

    for citation in citations:
        if citation.url in seen:
            continue
        seen.add(citation.url)
        merged.append(
            {
                "index": next_index,
                "label": citation.title or citation.url,
                "url": citation.url,
                "snippet": citation.snippet[:400],
                "kind": "web",
            }
        )
        next_index += 1

    return merged


# ================================================================== tools ===
async def tools_node(state: AgentState, config: RunnableConfig) -> dict:
    session, emitter, budget = _ctx(config)
    calls: list[ToolCall] = state.get("pending_tool_calls") or []
    if not calls:
        return {"pending_tool_calls": []}

    budget.add_tool_calls(len(calls))

    for call in calls:
        await emitter.emit("tool_call", name=call.name, arguments=call.arguments)

    outcomes = await asyncio.gather(
        *(
            registry.dispatch(
                call.name,
                call.arguments,
                session=session,
                user_id=state["user_id"],
                conversation_id=state["conversation_id"],
            )
            for call in calls
        )
    )

    messages = list(state.get("llm_messages") or [])
    # The assistant turn that requested the tools must be replayed verbatim, or
    # the tool result messages have nothing to attach to.
    messages.append(
        {
            "role": "assistant",
            "content": state.get("answer") or None,
            "tool_calls": [
                {
                    "id": call.id,
                    "type": "function",
                    "function": {"name": call.name, "arguments": _json(call.arguments)},
                }
                for call in calls
            ],
        }
    )

    new_chunks = []
    new_web = []
    trace = []

    for call, outcome in zip(calls, outcomes, strict=True):
        messages.append(
            {"role": "tool", "tool_call_id": call.id, "content": outcome.content[:12000]}
        )
        new_chunks.extend(outcome.chunks)
        new_web.extend(outcome.web_results)
        trace.append(
            {
                "tool": outcome.name,
                "arguments": call.arguments,
                "ok": outcome.ok,
                "error": outcome.error,
                "duration_ms": outcome.duration_ms,
                "result_count": len(outcome.chunks) + len(outcome.web_results),
            }
        )
        await emitter.emit(
            "tool_result",
            name=outcome.name,
            ok=outcome.ok,
            error=outcome.error,
            duration_ms=outcome.duration_ms,
            result_count=len(outcome.chunks) + len(outcome.web_results),
        )

    # Extend citations with anything the tools surfaced.
    sources = list(state.get("sources") or [])
    next_index = max((s.get("index", 0) for s in sources), default=0) + 1
    for chunk in new_chunks:
        sources.append(chunk.to_source(next_index))
        next_index += 1
    for result in new_web:
        sources.append(
            {
                "index": next_index,
                "label": result.get("title", "Web result"),
                "url": result.get("url"),
                "snippet": (result.get("snippet") or "")[:400],
                "kind": "web",
            }
        )
        next_index += 1
    if sources:
        await emitter.emit("sources", sources=sources)

    return {
        "llm_messages": messages,
        "chunks": new_chunks,
        "web_results": new_web,
        "sources": sources,
        "tool_trace": trace,
        "pending_tool_calls": [],
        # The pre-tool partial text was a preamble to the tool call, not the answer.
        "answer": "",
    }


def _json(value: Any) -> str:
    import json

    return json.dumps(value, ensure_ascii=False)


# ================================================================ routing ===
def route_after_generate(state: AgentState) -> str:
    if state.get("error"):
        return END
    if state.get("pending_tool_calls") and state.get("step", 0) < settings.agent_max_steps:
        return "tools"
    return END


def build_graph():  # noqa: ANN201 - CompiledStateGraph type is version-dependent
    builder = StateGraph(AgentState)
    builder.add_node("prepare", prepare)
    builder.add_node("retrieve", retrieve_node)
    builder.add_node("generate", generate)
    builder.add_node("tools", tools_node)

    builder.add_edge(START, "prepare")
    builder.add_edge("prepare", "retrieve")
    builder.add_edge("retrieve", "generate")
    builder.add_conditional_edges("generate", route_after_generate, {"tools": "tools", END: END})
    builder.add_edge("tools", "generate")

    return builder.compile()


_graph = None


def get_graph():  # noqa: ANN201
    global _graph
    if _graph is None:
        _graph = build_graph()
    return _graph
