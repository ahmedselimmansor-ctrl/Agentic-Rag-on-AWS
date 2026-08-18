"""Shared dependencies: identity resolution and per-user rate limits.

Identity has exactly one entry point — `resolve_user`. Everything else in the
codebase takes an already-authenticated `User`, so swapping the mechanism (say,
for Cognito or another OIDC provider) touches this file only.
"""

from __future__ import annotations

import logging
import uuid
from datetime import UTC, datetime, timedelta
from typing import Annotated

from fastapi import Depends, Header, HTTPException, Path, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.db.models import Conversation, Document, Message, Role, User
from app.db.session import get_session
from app.services.auth import AuthError, decode_access_token

logger = logging.getLogger(__name__)

DEV_USER_EMAIL = "local@example.com"

# auto_error=False so a missing header yields our own 401 with a WWW-Authenticate
# challenge rather than FastAPI's bare 403.
bearer_scheme = HTTPBearer(auto_error=False)

DbSession = Annotated[AsyncSession, Depends(get_session)]


def _unauthorized(detail: str) -> HTTPException:
    return HTTPException(
        status.HTTP_401_UNAUTHORIZED,
        detail,
        headers={"WWW-Authenticate": "Bearer"},
    )


async def resolve_user(
    session: DbSession,
    credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(bearer_scheme)] = None,
    x_user_email: Annotated[str | None, Header(alias="X-User-Email")] = None,
) -> User:
    if settings.auth_mode == "header":
        return await _resolve_dev_user(session, x_user_email)

    if credentials is None or not credentials.credentials:
        raise _unauthorized("Not authenticated.")

    try:
        payload = decode_access_token(credentials.credentials)
    except AuthError as exc:
        raise _unauthorized(str(exc)) from exc

    try:
        user_id = uuid.UUID(payload["sub"])
    except (KeyError, ValueError) as exc:
        raise _unauthorized("Invalid token.") from exc

    user = await session.get(User, user_id)
    if user is None:
        raise _unauthorized("Account no longer exists.")
    if not user.is_active:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "This account is disabled.")
    if settings.require_email_verification and user.email_verified_at is None:
        # 403 with a distinct code so the UI can show "confirm your email"
        # rather than bouncing the user back to a sign-in form they just used.
        raise HTTPException(
            status.HTTP_403_FORBIDDEN,
            "Confirm your email address to continue.",
            headers={"X-Auth-Reason": "email_unverified"},
        )
    return user


async def _resolve_dev_user(session: AsyncSession, x_user_email: str | None) -> User:
    """Header-trusting shortcut for local development. `Settings` refuses this
    mode in staging/prod, so it cannot reach a real deployment."""
    email = (x_user_email or DEV_USER_EMAIL).strip().lower()

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
        await session.rollback()
        user = (
            await session.execute(select(User).where(User.email == email))
        ).scalar_one()
    return user


CurrentUser = Annotated[User, Depends(resolve_user)]


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
        # 404 rather than 403: confirming a conversation exists but belongs to
        # someone else leaks that it exists.
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Conversation not found")
    return conversation


OwnedConversation = Annotated[Conversation, Depends(owned_conversation)]


# ------------------------------------------------------------ rate limits ---
# Counted from rows that already exist, so there is no extra write path and the
# limit holds across every ECS task rather than per-process.
async def enforce_message_quota(session: DbSession, user: CurrentUser) -> User:
    since = datetime.now(UTC) - timedelta(hours=1)
    sent = (
        await session.execute(
            select(func.count(Message.id))
            .join(Conversation, Conversation.id == Message.conversation_id)
            .where(
                Conversation.user_id == user.id,
                Message.role == Role.user,
                Message.created_at >= since,
            )
        )
    ).scalar_one()

    if sent >= settings.max_messages_per_hour:
        raise HTTPException(
            status.HTTP_429_TOO_MANY_REQUESTS,
            f"Message limit reached ({settings.max_messages_per_hour}/hour). Try again later.",
            headers={"Retry-After": "3600"},
        )
    return user


async def enforce_upload_quota(session: DbSession, user: CurrentUser) -> User:
    since = datetime.now(UTC) - timedelta(hours=1)
    uploaded = (
        await session.execute(
            select(func.count(Document.id)).where(
                Document.user_id == user.id, Document.created_at >= since
            )
        )
    ).scalar_one()

    if uploaded >= settings.max_uploads_per_hour:
        raise HTTPException(
            status.HTTP_429_TOO_MANY_REQUESTS,
            f"Upload limit reached ({settings.max_uploads_per_hour}/hour). Try again later.",
            headers={"Retry-After": "3600"},
        )
    return user


RateLimitedUser = Annotated[User, Depends(enforce_message_quota)]
UploadUser = Annotated[User, Depends(enforce_upload_quota)]
