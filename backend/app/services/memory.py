"""Two-tier memory.

Short-term = this conversation. The last N turns stay verbatim; everything older
is folded into a rolling summary on the conversation row, so a 200-turn thread
still fits the context window without losing its thread.

Long-term = across conversations. After each turn a cheap model extracts durable
facts and preferences; they are embedded and recalled by vector similarity on
later turns. Contradictions supersede rather than delete, so history is auditable.
"""

from __future__ import annotations

import json
import logging
import uuid
from dataclasses import dataclass

from sqlalchemy import select, text, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.db.models import Conversation, LongTermMemory, MemoryKind, Message, Role
from app.services.embeddings import embed_texts
from app.services.llm import complete
from app.services.tokens import count_tokens

logger = logging.getLogger(__name__)

VERBATIM_TURNS = 8  # user+assistant pairs kept word-for-word


# ============================================================ short-term ====
@dataclass(slots=True)
class ConversationContext:
    summary: str | None
    recent: list[Message]


async def load_short_term(
    session: AsyncSession, conversation_id: uuid.UUID, *, verbatim_turns: int = VERBATIM_TURNS
) -> ConversationContext:
    conv = await session.get(Conversation, conversation_id)
    summary = conv.summary if conv else None

    rows = (
        await session.execute(
            select(Message)
            .where(
                Message.conversation_id == conversation_id,
                Message.role.in_([Role.user, Role.assistant]),
                Message.error.is_(None),
            )
            .order_by(Message.ordinal.desc())
            .limit(verbatim_turns * 2)
        )
    ).scalars().all()

    return ConversationContext(summary=summary, recent=list(reversed(rows)))


async def maybe_summarize(session: AsyncSession, conversation_id: uuid.UUID) -> str | None:
    """Fold messages that have aged out of the verbatim window into the summary.

    Runs after a turn completes, so its latency never sits in the user's path.
    """
    conv = await session.get(Conversation, conversation_id)
    if conv is None:
        return None

    cutoff = (
        await session.execute(
            select(Message.ordinal)
            .where(Message.conversation_id == conversation_id)
            .order_by(Message.ordinal.desc())
            .limit(1)
        )
    ).scalar_one_or_none()
    if cutoff is None:
        return None

    # Everything at or below this ordinal has fallen out of the verbatim window.
    fold_through = cutoff - (VERBATIM_TURNS * 2)
    if fold_through <= conv.summarized_through:
        return conv.summary

    pending = (
        await session.execute(
            select(Message)
            .where(
                Message.conversation_id == conversation_id,
                Message.ordinal > conv.summarized_through,
                Message.ordinal <= fold_through,
                Message.role.in_([Role.user, Role.assistant]),
            )
            .order_by(Message.ordinal)
        )
    ).scalars().all()
    if not pending:
        return conv.summary

    transcript = "\n\n".join(f"{m.role.value.upper()}: {m.content[:2000]}" for m in pending)
    prompt = (
        "Update the running summary of this conversation. Preserve concrete "
        "details the user may refer back to: names, numbers, decisions, file "
        "names, constraints, and open questions. Drop pleasantries. "
        "Write at most 300 words of plain prose.\n\n"
        f"EXISTING SUMMARY:\n{conv.summary or '(none)'}\n\n"
        f"NEW MESSAGES:\n{transcript}\n\nUPDATED SUMMARY:"
    )
    summary = await complete(
        [{"role": "user", "content": prompt}], temperature=0.0, max_tokens=700
    )
    if not summary:
        return conv.summary

    await session.execute(
        update(Conversation)
        .where(Conversation.id == conversation_id)
        .values(summary=summary, summarized_through=fold_through)
    )
    return summary


async def generate_title(session: AsyncSession, conversation_id: uuid.UUID, first_message: str) -> str:
    title = await complete(
        [
            {
                "role": "user",
                "content": (
                    "Write a 3-6 word title for a chat that starts with the message "
                    f"below. Reply with the title only, no quotes.\n\n{first_message[:1000]}"
                ),
            }
        ],
        temperature=0.2,
        max_tokens=30,
    )
    title = (title or first_message[:60]).strip().strip('"').strip()[:120] or "New chat"
    await session.execute(
        update(Conversation).where(Conversation.id == conversation_id).values(title=title)
    )
    return title


# ============================================================= long-term ====
_EXTRACTION_PROMPT = """You maintain a durable memory of a user across conversations.

From the exchange below, extract only facts worth remembering weeks from now:
stable preferences, their role or domain, ongoing projects, named entities they
work with, and constraints they have stated.

Do NOT extract: anything specific to this one question, transient state, content
of retrieved documents, or your own answer.

Return JSON: {"memories": [{"kind": "fact"|"preference"|"entity", "content": "...", "salience": 0.0-1.0}]}
Return {"memories": []} when nothing qualifies — that is the common case.

USER: %(user)s

ASSISTANT: %(assistant)s"""


async def extract_long_term(
    session: AsyncSession,
    *,
    user_id: uuid.UUID,
    conversation_id: uuid.UUID,
    user_text: str,
    assistant_text: str,
) -> list[LongTermMemory]:
    """Post-turn distillation. Failures are logged and swallowed."""
    raw = await complete(
        [
            {
                "role": "user",
                "content": _EXTRACTION_PROMPT
                % {"user": user_text[:4000], "assistant": assistant_text[:4000]},
            }
        ],
        temperature=0.0,
        max_tokens=600,
        json_mode=True,
    )
    if not raw:
        return []

    try:
        items = json.loads(raw).get("memories", [])
    except (json.JSONDecodeError, AttributeError):
        logger.warning("memory extraction returned non-JSON: %r", raw[:200])
        return []

    candidates = [
        (str(i.get("content", "")).strip(), i.get("kind", "fact"), float(i.get("salience", 0.5)))
        for i in items
        if isinstance(i, dict) and str(i.get("content", "")).strip()
    ][:5]
    if not candidates:
        return []

    vectors = await embed_texts([c[0] for c in candidates])
    stored: list[LongTermMemory] = []

    for (content, kind, salience), vector in zip(candidates, vectors, strict=True):
        duplicate = await _find_near_duplicate(session, user_id, vector)
        if duplicate is not None:
            # Same ground being restated — reinforce instead of duplicating.
            duplicate.salience = min(1.0, duplicate.salience + 0.1)
            duplicate.use_count += 1
            continue
        try:
            memory_kind = MemoryKind(kind)
        except ValueError:
            memory_kind = MemoryKind.fact
        memory = LongTermMemory(
            user_id=user_id,
            kind=memory_kind,
            content=content,
            embedding=vector,
            salience=max(0.0, min(1.0, salience)),
            source_conversation_id=conversation_id,
        )
        session.add(memory)
        stored.append(memory)

    return stored


async def _find_near_duplicate(
    session: AsyncSession, user_id: uuid.UUID, vector: list[float], threshold: float = 0.12
) -> LongTermMemory | None:
    """Cosine distance below `threshold` means we already know this."""
    literal = "[" + ",".join(f"{v:.7g}" for v in vector) + "]"
    row = (
        await session.execute(
            text(
                """
                SELECT id, (embedding <=> :qvec::vector) AS distance
                FROM long_term_memories
                WHERE user_id = :user_id
                  AND superseded_by IS NULL
                  AND embedding IS NOT NULL
                ORDER BY embedding <=> :qvec::vector
                LIMIT 1
                """
            ),
            {"qvec": literal, "user_id": str(user_id)},
        )
    ).mappings().first()

    if row and row["distance"] < threshold:
        return await session.get(LongTermMemory, row["id"])
    return None


async def recall_long_term(
    session: AsyncSession, *, user_id: uuid.UUID, query: str, limit: int | None = None
) -> list[LongTermMemory]:
    """Vector recall, biased toward salient memories."""
    limit = limit or settings.max_long_term_memories
    try:
        from app.services.embeddings import embed_query

        vector = await embed_query(query)
    except Exception as exc:  # noqa: BLE001 - memory is an enhancement, not a requirement
        logger.warning("long-term recall skipped: %s", exc)
        return []

    literal = "[" + ",".join(f"{v:.7g}" for v in vector) + "]"
    rows = (
        await session.execute(
            text(
                """
                SELECT id
                FROM long_term_memories
                WHERE user_id = :user_id
                  AND superseded_by IS NULL
                  AND embedding IS NOT NULL
                ORDER BY (embedding <=> :qvec::vector) - (salience * 0.1)
                LIMIT :limit
                """
            ),
            {"qvec": literal, "user_id": str(user_id), "limit": limit},
        )
    ).scalars().all()
    if not rows:
        return []

    memories = (
        await session.execute(select(LongTermMemory).where(LongTermMemory.id.in_(rows)))
    ).scalars().all()

    await session.execute(
        update(LongTermMemory)
        .where(LongTermMemory.id.in_(rows))
        .values(last_used_at=text("now()"), use_count=LongTermMemory.use_count + 1)
    )
    return list(memories)


def render_memories(memories: list[LongTermMemory], max_tokens: int = 800) -> str:
    """Format for the system prompt, hard-capped so memory cannot crowd out retrieval."""
    if not memories:
        return ""
    lines: list[str] = []
    used = 0
    for m in sorted(memories, key=lambda x: x.salience, reverse=True):
        line = f"- ({m.kind.value}) {m.content}"
        t = count_tokens(line)
        if used + t > max_tokens:
            break
        lines.append(line)
        used += t
    return "\n".join(lines)
