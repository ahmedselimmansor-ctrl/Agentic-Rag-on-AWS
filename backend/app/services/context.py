"""Context-window assembly.

The budget is spent in priority order, because when the window is tight the
things that must survive are the system prompt and the user's actual question —
not the tenth retrieved passage:

    system prompt + memory  >  current question  >  recent turns
      >  retrieved passages  >  rolling summary

Each section is capped independently so no single one can starve the others, and
the total is verified against the model's window before the request goes out.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any

from app.agent.prompts import (
    MEMORY_BLOCK_HEADER,
    SUMMARY_BLOCK_HEADER,
    SYSTEM_PROMPT,
    format_context_block,
)
from app.config import settings
from app.db.models import Message, Role
from app.services.retrieval import RetrievedChunk
from app.services.tokens import count_message_tokens, count_tokens, truncate_to_tokens

logger = logging.getLogger(__name__)


@dataclass(slots=True)
class AssembledContext:
    messages: list[dict[str, Any]]
    sources: list[dict] = field(default_factory=list)
    used_chunk_ids: list[str] = field(default_factory=list)
    prompt_tokens: int = 0
    dropped_passages: int = 0
    dropped_history: int = 0


def build_messages(
    *,
    question: str,
    chunks: list[RetrievedChunk],
    history: list[Message],
    summary: str | None = None,
    memory_block: str = "",
    attachments: list[dict] | None = None,
    web_results: list[dict] | None = None,
) -> AssembledContext:
    budget = settings.input_token_budget

    # ---- 1. system prompt + long-term memory (never dropped) ---------------
    system_parts = [SYSTEM_PROMPT]
    if memory_block:
        system_parts.append(f"\n{MEMORY_BLOCK_HEADER}\n{memory_block}")
    system_content = "\n".join(system_parts)
    spent = count_tokens(system_content) + 8

    # ---- 2. the question itself (never dropped) ---------------------------
    user_content = _build_user_content(question, attachments)
    spent += count_tokens(question) + (800 * len(attachments or [])) + 8

    # ---- 3. retrieved passages --------------------------------------------
    passage_budget = min(settings.max_retrieved_context_tokens, max(0, budget - spent - 2000))
    passages, sources, used_ids, dropped = _fit_passages(chunks, passage_budget, web_results or [])
    context_block = format_context_block(passages)
    spent += count_tokens(context_block)

    # ---- 4. recent turns ---------------------------------------------------
    history_budget = min(settings.max_history_tokens, max(0, budget - spent - 500))
    history_messages, dropped_history = _fit_history(history, history_budget)
    spent += count_message_tokens(history_messages)

    # ---- 5. rolling summary (first to go) ----------------------------------
    if summary:
        remaining = max(0, budget - spent - 200)
        if remaining > 100:
            trimmed = truncate_to_tokens(summary, min(remaining, 1200))
            system_content += f"\n\n{SUMMARY_BLOCK_HEADER}\n{trimmed}"
            spent += count_tokens(trimmed)

    messages: list[dict[str, Any]] = [{"role": "system", "content": system_content}]
    messages.extend(history_messages)
    # Context sits immediately before the question so the model reads it last —
    # attention to instructions degrades with distance from the end of the prompt.
    messages.append({"role": "user", "content": _prepend_context(user_content, context_block)})

    total = count_message_tokens(messages)
    if total > budget:
        logger.warning("assembled prompt is %d tokens over budget %d", total, budget)

    return AssembledContext(
        messages=messages,
        sources=sources,
        used_chunk_ids=used_ids,
        prompt_tokens=total,
        dropped_passages=dropped,
        dropped_history=dropped_history,
    )


def _build_user_content(question: str, attachments: list[dict] | None) -> Any:
    """Multimodal content parts when images are attached, plain string otherwise."""
    if not attachments:
        return question
    parts: list[dict[str, Any]] = []
    for att in attachments:
        url = att.get("url")
        if url and str(att.get("mime_type", "")).startswith("image/"):
            parts.append({"type": "image_url", "image_url": {"url": url}})
    if not parts:
        return question
    parts.append({"type": "text", "text": question})
    return parts


def _prepend_context(user_content: Any, context_block: str) -> Any:
    if isinstance(user_content, str):
        return f"{context_block}\n\n## QUESTION\n{user_content}"
    parts = list(user_content)
    for i, part in enumerate(parts):
        if part.get("type") == "text":
            parts[i] = {"type": "text", "text": f"{context_block}\n\n## QUESTION\n{part['text']}"}
            break
    return parts


def _fit_passages(
    chunks: list[RetrievedChunk], budget: int, web_results: list[dict]
) -> tuple[list[tuple[int, str, str]], list[dict], list[str], int]:
    passages: list[tuple[int, str, str]] = []
    sources: list[dict] = []
    used_ids: list[str] = []
    spent = 0
    index = 1

    for chunk in chunks:
        body = chunk.prompt_text
        cost = count_tokens(body) + 20
        if spent + cost > budget:
            # Try to fit a truncated version rather than dropping outright.
            remaining = budget - spent - 20
            if remaining < 120:
                break
            body = truncate_to_tokens(body, remaining) + "\n[…truncated]"
            cost = remaining + 20
        passages.append((index, chunk.citation_label, body))
        sources.append(chunk.to_source(index))
        used_ids.append(str(chunk.chunk_id))
        spent += cost
        index += 1

    dropped = len(chunks) - len(passages)

    for result in web_results:
        body = f"{result.get('snippet', '')}\n{result.get('content', '')}".strip()
        cost = count_tokens(body) + 20
        if spent + cost > budget:
            break
        label = f"{result.get('title', 'Web result')} — {result.get('url', '')}"
        passages.append((index, label, body))
        sources.append(
            {
                "index": index,
                "label": result.get("title", "Web result"),
                "url": result.get("url"),
                "snippet": (result.get("snippet") or "")[:400],
                "kind": "web",
                "score": result.get("score"),
            }
        )
        spent += cost
        index += 1

    return passages, sources, used_ids, dropped


def _fit_history(history: list[Message], budget: int) -> tuple[list[dict[str, Any]], int]:
    """Walk backwards from the most recent turn, keeping whole messages."""
    out: list[dict[str, Any]] = []
    spent = 0
    kept = 0

    for message in reversed(history):
        if not message.content:
            continue
        content = message.content
        cost = count_tokens(content) + 8
        if spent + cost > budget:
            break
        role = "assistant" if message.role == Role.assistant else "user"
        out.append({"role": role, "content": content})
        spent += cost
        kept += 1

    out.reverse()
    # Never open the history with an assistant message — it reads as a dangling reply.
    while out and out[0]["role"] == "assistant":
        out.pop(0)
        kept -= 1

    return out, max(0, len(history) - kept)
