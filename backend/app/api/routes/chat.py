"""Chat endpoint — SSE, token-by-token."""

from __future__ import annotations

import logging

from fastapi import APIRouter, HTTPException, status
from fastapi.responses import StreamingResponse
from sqlalchemy import func, select

from app.agent.runner import run_turn
from app.api.deps import CurrentUser, DbSession, RateLimitedUser
from app.db.models import Conversation
from app.schemas.chat import ChatRequest, SearchHit, SearchRequest
from app.services.retrieval import retrieve

logger = logging.getLogger(__name__)
router = APIRouter(tags=["chat"])

SSE_HEADERS = {
    "Cache-Control": "no-cache, no-transform",
    "Connection": "keep-alive",
    # Tells nginx (and any well-behaved proxy) not to buffer the stream.
    "X-Accel-Buffering": "no",
}


@router.post("/chat")
async def chat(payload: ChatRequest, session: DbSession, user: RateLimitedUser) -> StreamingResponse:
    """Stream one assistant turn. Frames: start, status, tool_call, tool_result,
    sources, token, usage, done | error."""
    conversation_id = payload.conversation_id

    if conversation_id is None:
        conversation = Conversation(user_id=user.id, title="New chat", last_message_at=func.now())
        session.add(conversation)
        await session.flush()
        conversation_id = conversation.id
    else:
        owned = (
            await session.execute(
                select(Conversation.id).where(
                    Conversation.id == conversation_id, Conversation.user_id == user.id
                )
            )
        ).scalar_one_or_none()
        if owned is None:
            raise HTTPException(status.HTTP_404_NOT_FOUND, "Conversation not found")

    # Commit before streaming: the generator runs on its own sessions and must
    # be able to see the conversation row.
    await session.commit()

    attachments = [a.model_dump(mode="json") for a in payload.attachments]

    return StreamingResponse(
        run_turn(
            user_id=user.id,
            conversation_id=conversation_id,
            question=payload.message,
            attachments=attachments,
            web_enabled=payload.web_search,
        ),
        media_type="text/event-stream",
        headers=SSE_HEADERS,
    )


@router.post("/search", response_model=list[SearchHit])
async def search_documents(
    payload: SearchRequest, session: DbSession, user: CurrentUser
) -> list[SearchHit]:
    """Retrieval without generation — useful for debugging relevance."""
    chunks = await retrieve(
        session,
        payload.query,
        user_id=user.id,
        conversation_id=payload.conversation_id,
        top_n=payload.top_n,
    )
    return [
        SearchHit(
            chunk_id=c.chunk_id,
            document_id=c.document_id,
            filename=c.filename,
            label=c.citation_label,
            snippet=c.content[:600],
            page_from=c.page_from,
            page_to=c.page_to,
            score=c.rerank_score,
            fusion_score=c.fusion_score,
        )
        for c in chunks
    ]
