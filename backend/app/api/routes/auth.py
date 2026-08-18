"""Registration, login, refresh, logout."""

from __future__ import annotations

import logging
import uuid
from datetime import UTC, datetime

from fastapi import APIRouter, HTTPException, Request, Response, status
from sqlalchemy import func, select, update

from app.api.deps import CurrentUser, DbSession
from app.config import settings
from app.db.models import RefreshToken, User
from app.schemas.auth import (
    AuthTokens,
    LoginRequest,
    RefreshRequest,
    RegisterRequest,
    UserOut,
)
from app.services import auth as auth_service
from app.services.auth import AuthError

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/auth", tags=["auth"])

# Deliberately identical for "no such account" and "wrong password" — a
# distinguishable error turns the login form into an account-existence oracle.
INVALID_CREDENTIALS = "Incorrect email or password."


@router.post("/register", response_model=AuthTokens, status_code=status.HTTP_201_CREATED)
async def register(
    payload: RegisterRequest,
    request: Request,
    session: DbSession,
) -> AuthTokens:
    if not settings.allow_registration:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Registration is disabled.")

    try:
        email = auth_service.normalize_email(payload.email)
        password_hash = auth_service.hash_password(payload.password)
    except AuthError as exc:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, str(exc)) from exc

    existing = (
        await session.execute(select(User.id).where(User.email == email))
    ).scalar_one_or_none()
    if existing is not None:
        # Same status and shape as a weak-password rejection, so this does not
        # confirm whether the address is registered.
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            "Could not create that account. Try signing in instead.",
        )

    user = User(
        email=email,
        display_name=(payload.display_name or email.split("@")[0])[:200],
        password_hash=password_hash,
        # A Python value, not func.now(): a SQL expression would stay unresolved
        # on the instance and force a lazy read during serialization.
        last_login_at=datetime.now(UTC),
    )
    session.add(user)
    await session.flush()
    # created_at/updated_at come from server defaults. Without this refresh the
    # response model triggers a lazy load, which cannot run in async context.
    await session.refresh(user)

    return await _issue_tokens(session, user, request)


@router.post("/login", response_model=AuthTokens)
async def login(payload: LoginRequest, request: Request, session: DbSession) -> AuthTokens:
    try:
        email = auth_service.normalize_email(payload.email)
    except AuthError:
        auth_service.dummy_verify()
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, INVALID_CREDENTIALS) from None

    user = (
        await session.execute(select(User).where(User.email == email))
    ).scalar_one_or_none()

    if user is None or not user.password_hash:
        # Equalise timing against the hash-verification branch below.
        auth_service.dummy_verify()
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, INVALID_CREDENTIALS)

    if not auth_service.verify_password(payload.password, user.password_hash):
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, INVALID_CREDENTIALS)

    if not user.is_active:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "This account is disabled.")

    user.last_login_at = datetime.now(UTC)
    return await _issue_tokens(session, user, request)


@router.post("/refresh", response_model=AuthTokens)
async def refresh(payload: RefreshRequest, request: Request, session: DbSession) -> AuthTokens:
    token_hash = auth_service.hash_refresh_token(payload.refresh_token)

    row = (
        await session.execute(
            select(RefreshToken).where(RefreshToken.token_hash == token_hash)
        )
    ).scalar_one_or_none()

    if row is None:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Invalid refresh token.")

    if row.revoked_at is not None:
        # A revoked token being presented means it was captured and replayed.
        # Kill every token in the family: the legitimate holder re-authenticates,
        # and the attacker's copy dies with it.
        logger.warning("refresh token replay detected for family %s", row.family_id)
        await session.execute(
            update(RefreshToken)
            .where(RefreshToken.family_id == row.family_id, RefreshToken.revoked_at.is_(None))
            .values(revoked_at=func.now())
        )
        # Commit before raising. The session dependency rolls back on exception,
        # which would silently undo the revocation and leave the stolen token
        # working — the exact failure this branch exists to prevent.
        await session.commit()
        raise HTTPException(
            status.HTTP_401_UNAUTHORIZED, "Session revoked. Please sign in again."
        )

    if row.expires_at <= datetime.now(UTC):
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Session expired. Please sign in again.")

    user = await session.get(User, row.user_id)
    if user is None or not user.is_active:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Account unavailable.")

    row.revoked_at = datetime.now(UTC)
    return await _issue_tokens(session, user, request, family_id=row.family_id)


@router.post("/logout", status_code=status.HTTP_204_NO_CONTENT)
async def logout(payload: RefreshRequest, session: DbSession) -> Response:
    """Revokes the presented token's whole family — i.e. this device's session."""
    token_hash = auth_service.hash_refresh_token(payload.refresh_token)
    row = (
        await session.execute(
            select(RefreshToken).where(RefreshToken.token_hash == token_hash)
        )
    ).scalar_one_or_none()

    if row is not None:
        await session.execute(
            update(RefreshToken)
            .where(RefreshToken.family_id == row.family_id, RefreshToken.revoked_at.is_(None))
            .values(revoked_at=func.now())
        )
    # 204 regardless: whether the token existed is not the caller's business.
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.post("/logout-all", status_code=status.HTTP_204_NO_CONTENT)
async def logout_all(user: CurrentUser, session: DbSession) -> Response:
    """Sign out every device. The escape hatch after a suspected compromise."""
    await session.execute(
        update(RefreshToken)
        .where(RefreshToken.user_id == user.id, RefreshToken.revoked_at.is_(None))
        .values(revoked_at=func.now())
    )
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.get("/me", response_model=UserOut)
async def me(user: CurrentUser) -> User:
    return user


async def _issue_tokens(
    session,
    user: User,
    request: Request,
    *,
    family_id: uuid.UUID | None = None,
) -> AuthTokens:
    access_token, expires_in = auth_service.create_access_token(user.id, user.email)
    raw_refresh, refresh_hash = auth_service.generate_refresh_token()

    session.add(
        RefreshToken(
            user_id=user.id,
            token_hash=refresh_hash,
            family_id=family_id or uuid.uuid4(),
            expires_at=auth_service.refresh_expiry(),
            user_agent=(request.headers.get("user-agent") or "")[:400] or None,
        )
    )

    return AuthTokens(
        access_token=access_token,
        refresh_token=raw_refresh,
        expires_in=expires_in,
        user=UserOut.model_validate(user),
    )
