"""Shared dependencies.

Identity is intentionally pluggable. Locally, `X-User-Email` is trusted and the
user is created on first sight. In front of a real deployment, put Cognito (or
any OIDC provider) on the ALB and replace `resolve_user` with a verified-claims
lookup — nothing else in the codebase reads identity directly.
"""

from __future__ import annotations

import logging
import uuid
from typing import Annotated

from fastapi import Depends, Header, HTTPException, Path, status
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import Conversation, User
from app.db.session import get_session

logger = logging.getLogger(__name__)

DEFAULT_USER_EMAIL = "local@example.com"


async def resolve_user(
    session: Annotated[AsyncSession, Depends(get_session)],
    x_user_email: Annotated[str | None, Header(alias="X-User-Email")] = None,
) -> User:
    email = (x_user_email or DEFAULT_USER_EMAIL).strip().lower()

    user = (
        await session.execute(select(User).where(User.email == email))
    ).scalar_one_or_none()
    if user is not None:
        return user

    user = User(email=email, display_name=email.split("@")[0])
    session.add(user)
    try:
        await session.flush()
    except IntegrityError:
        # Concurrent first request for the same email.
        await session.rollback()
        user = (
            await session.execute(select(User).where(User.email == email))
        ).scalar_one()
    return user


CurrentUser = Annotated[User, Depends(resolve_user)]
DbSession = Annotated[AsyncSession, Depends(get_session)]


async def owned_conversation(
    conversation_id: Annotated[uuid.UUID, Path()],
    session: DbSession,
    user: CurrentUser,
) -> Conversation:
    conversation = (
        await session.execute(
            select(Conversation).where(
                Conversation.id == conversation_id, Conversation.user_id == user.id
            )
        )
    ).scalar_one_or_none()
    if conversation is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Conversation not found")
    return conversation


OwnedConversation = Annotated[Conversation, Depends(owned_conversation)]
