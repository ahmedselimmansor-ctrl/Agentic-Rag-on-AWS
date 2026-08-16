"""Conversation CRUD — backs the sidebar (new chat + history)."""

from __future__ import annotations

from fastapi import APIRouter, Query, Response, status
from sqlalchemy import func, select

from app.api.deps import CurrentUser, DbSession, OwnedConversation
from app.db.models import Conversation, Message, Role
from app.schemas.chat import (
    ConversationCreate,
    ConversationDetail,
    ConversationOut,
    ConversationUpdate,
    MessageOut,
)

router = APIRouter(prefix="/conversations", tags=["conversations"])


@router.get("", response_model=list[ConversationOut])
async def list_conversations(
    session: DbSession,
    user: CurrentUser,
    archived: bool = False,
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
) -> list[Conversation]:
    rows = (
        await session.execute(
            select(Conversation)
            .where(Conversation.user_id == user.id, Conversation.archived == archived)
            .order_by(
                func.coalesce(Conversation.last_message_at, Conversation.created_at).desc()
            )
            .limit(limit)
            .offset(offset)
        )
    ).scalars().all()
    return list(rows)


@router.post("", response_model=ConversationOut, status_code=status.HTTP_201_CREATED)
async def create_conversation(
    payload: ConversationCreate, session: DbSession, user: CurrentUser
) -> Conversation:
    conversation = Conversation(user_id=user.id, title=payload.title or "New chat")
    session.add(conversation)
    await session.flush()
    return conversation


@router.get("/{conversation_id}", response_model=ConversationDetail)
async def get_conversation(
    conversation: OwnedConversation, session: DbSession
) -> ConversationDetail:
    messages = (
        await session.execute(
            select(Message)
            .where(
                Message.conversation_id == conversation.id,
                Message.role.in_([Role.user, Role.assistant]),
            )
            .order_by(Message.ordinal)
        )
    ).scalars().all()

    return ConversationDetail(
        id=conversation.id,
        title=conversation.title,
        archived=conversation.archived,
        created_at=conversation.created_at,
        updated_at=conversation.updated_at,
        last_message_at=conversation.last_message_at,
        summary=conversation.summary,
        messages=[MessageOut.model_validate(m) for m in messages],
    )


@router.patch("/{conversation_id}", response_model=ConversationOut)
async def update_conversation(
    payload: ConversationUpdate, conversation: OwnedConversation
) -> Conversation:
    if payload.title is not None:
        conversation.title = payload.title
    if payload.archived is not None:
        conversation.archived = payload.archived
    return conversation


@router.delete("/{conversation_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_conversation(conversation: OwnedConversation, session: DbSession) -> Response:
    await session.delete(conversation)
    return Response(status_code=status.HTTP_204_NO_CONTENT)
