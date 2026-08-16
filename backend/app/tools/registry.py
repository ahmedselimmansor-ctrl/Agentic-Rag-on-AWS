"""Tool schemas + dispatch.

Tools are exposed to the model as OpenAI function definitions. `available_tools`
decides which ones this turn actually gets — the frontend's web-search toggle
maps straight onto it, so a user who turned search off simply never sees the
model reach for it.
"""

from __future__ import annotations

import logging
import time
import uuid
from dataclasses import dataclass, field
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.services.retrieval import RetrievedChunk, retrieve
from app.tools import web_search

logger = logging.getLogger(__name__)

SEARCH_DOCUMENTS = "search_documents"
WEB_SEARCH = "web_search"
FETCH_PAGE = "fetch_page"


SEARCH_DOCUMENTS_SCHEMA = {
    "type": "function",
    "function": {
        "name": SEARCH_DOCUMENTS,
        "description": (
            "Search the user's uploaded documents. Use when the question needs "
            "information from their own files, or when the passages already in "
            "context do not answer it."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "description": (
                        "A standalone search query. Resolve pronouns and keep the "
                        "user's domain terms verbatim."
                    ),
                },
                "top_n": {
                    "type": "integer",
                    "description": "How many passages to return (1-15). Default 8.",
                    "minimum": 1,
                    "maximum": 15,
                },
            },
            "required": ["query"],
        },
    },
}

WEB_SEARCH_SCHEMA = {
    "type": "function",
    "function": {
        "name": WEB_SEARCH,
        "description": (
            "Search the public web. Use for current events, live data, or facts "
            "outside the user's uploaded documents and your training data."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "The web search query."},
                "max_results": {
                    "type": "integer",
                    "description": "Results to return (1-10). Default 6.",
                    "minimum": 1,
                    "maximum": 10,
                },
            },
            "required": ["query"],
        },
    },
}

FETCH_PAGE_SCHEMA = {
    "type": "function",
    "function": {
        "name": FETCH_PAGE,
        "description": (
            "Fetch the readable text of a specific URL returned by web_search, "
            "when the snippet is not enough to answer."
        ),
        "parameters": {
            "type": "object",
            "properties": {"url": {"type": "string", "description": "Absolute URL to fetch."}},
            "required": ["url"],
        },
    },
}


def available_tools(*, web_enabled: bool, has_documents: bool) -> list[dict[str, Any]]:
    tools: list[dict[str, Any]] = []
    if has_documents:
        tools.append(SEARCH_DOCUMENTS_SCHEMA)
    if web_enabled and settings.web_search_provider != "none":
        tools.append(WEB_SEARCH_SCHEMA)
        tools.append(FETCH_PAGE_SCHEMA)
    return tools


@dataclass(slots=True)
class ToolOutcome:
    """`content` goes back to the model; `chunks`/`web_results` feed the citation UI."""

    name: str
    content: str
    ok: bool = True
    error: str | None = None
    duration_ms: int = 0
    chunks: list[RetrievedChunk] = field(default_factory=list)
    web_results: list[dict] = field(default_factory=list)


async def dispatch(
    name: str,
    arguments: dict[str, Any],
    *,
    session: AsyncSession,
    user_id: uuid.UUID,
    conversation_id: uuid.UUID | None,
) -> ToolOutcome:
    started = time.perf_counter()
    try:
        if name == SEARCH_DOCUMENTS:
            outcome = await _run_search_documents(arguments, session, user_id, conversation_id)
        elif name == WEB_SEARCH:
            outcome = await _run_web_search(arguments)
        elif name == FETCH_PAGE:
            outcome = await _run_fetch_page(arguments)
        else:
            outcome = ToolOutcome(name=name, content=f"Unknown tool: {name}", ok=False,
                                  error="unknown_tool")
    except Exception as exc:  # noqa: BLE001 - a tool failure is a message, not a crash
        logger.exception("tool %s failed", name)
        outcome = ToolOutcome(
            name=name,
            content=f"The {name} tool failed: {exc}. Answer without it, and say so.",
            ok=False,
            error=str(exc)[:500],
        )

    outcome.duration_ms = int((time.perf_counter() - started) * 1000)
    return outcome


async def _run_search_documents(
    args: dict, session: AsyncSession, user_id: uuid.UUID, conversation_id: uuid.UUID | None
) -> ToolOutcome:
    query = str(args.get("query", "")).strip()
    if not query:
        return ToolOutcome(SEARCH_DOCUMENTS, "No query provided.", ok=False, error="empty_query")

    top_n = min(max(int(args.get("top_n") or settings.rerank_top_n), 1), 15)
    chunks = await retrieve(
        session, query, user_id=user_id, conversation_id=conversation_id, top_n=top_n
    )
    if not chunks:
        return ToolOutcome(
            SEARCH_DOCUMENTS,
            f"No passages in the user's documents match '{query}'.",
            chunks=[],
        )

    rendered = "\n\n".join(
        f"[{i}] {c.citation_label}\n{c.prompt_text[:1500]}" for i, c in enumerate(chunks, 1)
    )
    return ToolOutcome(SEARCH_DOCUMENTS, rendered, chunks=chunks)


async def _run_web_search(args: dict) -> ToolOutcome:
    query = str(args.get("query", "")).strip()
    if not query:
        return ToolOutcome(WEB_SEARCH, "No query provided.", ok=False, error="empty_query")

    n = min(max(int(args.get("max_results") or settings.web_search_max_results), 1), 10)
    try:
        results = await web_search.search(query, n)
    except web_search.WebSearchUnavailable as exc:
        return ToolOutcome(WEB_SEARCH, str(exc), ok=False, error=str(exc))

    if not results:
        return ToolOutcome(WEB_SEARCH, f"No web results for '{query}'.")

    rendered = "\n\n".join(
        f"[{i}] {r.title} — {r.url}\n{r.snippet}" for i, r in enumerate(results, 1)
    )
    return ToolOutcome(WEB_SEARCH, rendered, web_results=[r.to_dict() for r in results])


async def _run_fetch_page(args: dict) -> ToolOutcome:
    url = str(args.get("url", "")).strip()
    if not url.startswith(("http://", "https://")):
        return ToolOutcome(FETCH_PAGE, "An absolute http(s) URL is required.", ok=False,
                           error="bad_url")
    text = await web_search.fetch_page_text(url)
    if not text:
        return ToolOutcome(FETCH_PAGE, f"No readable text at {url}.")
    return ToolOutcome(
        FETCH_PAGE,
        f"Content of {url}:\n\n{text}",
        web_results=[{"title": url, "url": url, "snippet": text[:400]}],
    )
