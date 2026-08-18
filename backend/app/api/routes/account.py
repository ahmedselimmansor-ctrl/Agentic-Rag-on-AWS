"""Email verification and password reset.

Every response here is deliberately uninformative about whether an address is
registered. "Forgot password" is the classic account-enumeration oracle: if it
answers differently for known and unknown addresses, it becomes a free
membership check for anyone with a list of emails.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime

from fastapi import APIRouter, BackgroundTasks, HTTPException, status
from sqlalchemy import func, select, update

from app.api.deps import CurrentUser, DbSession
from app.db.models import OneTimeToken, OneTimeTokenPurpose, RefreshToken, User
from app.schemas.auth import (
    ForgotPasswordRequest,
    MessageOut,
    ResetPasswordRequest,
    VerifyEmailRequest,
)
from app.services import auth as auth_service
from app.services import email as email_service
from app.services.auth import AuthError

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/account", tags=["account"])

# Identical for every outcome of "forgot password".
RESET_ACK = "If that address has an account, a reset link is on its way."


async def issue_verification(session, user: User, background: BackgroundTasks) -> None:
    """Invalidate any outstanding verification tokens and send a fresh one."""
    await session.execute(
        update(OneTimeToken)
        .where(
            OneTimeToken.user_id == user.id,
            OneTimeToken.purpose == OneTimeTokenPurpose.email_verification,
            OneTimeToken.used_at.is_(None),
        )
        .values(used_at=func.now())
    )

    raw, token_hash = auth_service.generate_one_time_token()
    session.add(
        OneTimeToken(
            user_id=user.id,
            purpose=OneTimeTokenPurpose.email_verification,
            token_hash=token_hash,
            expires_at=auth_service.verification_expiry(),
        )
    )
    # Sending is slow and must not extend the request; it also must not fail it.
    background.add_task(email_service.send, email_service.verification_email(user.email, raw))


@router.post("/verify-email", response_model=MessageOut)
async def verify_email(
    payload: VerifyEmailRequest, session: DbSession
) -> MessageOut:
    token_hash = auth_service.hash_refresh_token(payload.token)
    row = (
        await session.execute(
            select(OneTimeToken).where(
                OneTimeToken.token_hash == token_hash,
                OneTimeToken.purpose == OneTimeTokenPurpose.email_verification,
            )
        )
    ).scalar_one_or_none()

    if row is None or row.used_at is not None or row.expires_at <= datetime.now(UTC):
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            "That confirmation link is invalid or has expired. Request a new one.",
        )

    row.used_at = datetime.now(UTC)
    user = await session.get(User, row.user_id)
    if user is None:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "Account no longer exists.")

    # Idempotent: clicking the link twice is a normal thing for people to do.
    if user.email_verified_at is None:
        user.email_verified_at = datetime.now(UTC)

    return MessageOut(message="Email confirmed. You're all set.")


@router.post("/resend-verification", response_model=MessageOut)
async def resend_verification(
    user: CurrentUser, session: DbSession, background: BackgroundTasks
) -> MessageOut:
    if user.email_verified_at is not None:
        return MessageOut(message="Your email is already confirmed.")
    await issue_verification(session, user, background)
    return MessageOut(message="Confirmation email sent.")


@router.post("/forgot-password", response_model=MessageOut)
async def forgot_password(
    payload: ForgotPasswordRequest, session: DbSession, background: BackgroundTasks
) -> MessageOut:
    try:
        email = auth_service.normalize_email(payload.email)
    except AuthError:
        return MessageOut(message=RESET_ACK)

    user = (
        await session.execute(select(User).where(User.email == email))
    ).scalar_one_or_none()

    # Only users with a local password can reset one; IdP-provisioned accounts
    # have nothing to reset. Either way the caller gets the same answer.
    if user is not None and user.is_active and user.password_hash:
        await session.execute(
            update(OneTimeToken)
            .where(
                OneTimeToken.user_id == user.id,
                OneTimeToken.purpose == OneTimeTokenPurpose.password_reset,
                OneTimeToken.used_at.is_(None),
            )
            .values(used_at=func.now())
        )

        raw, token_hash = auth_service.generate_one_time_token()
        session.add(
            OneTimeToken(
                user_id=user.id,
                purpose=OneTimeTokenPurpose.password_reset,
                token_hash=token_hash,
                expires_at=auth_service.reset_expiry(),
            )
        )
        background.add_task(
            email_service.send, email_service.password_reset_email(user.email, raw)
        )
    else:
        logger.info("password reset requested for unknown or ineligible address")

    return MessageOut(message=RESET_ACK)


@router.post("/reset-password", response_model=MessageOut)
async def reset_password(
    payload: ResetPasswordRequest, session: DbSession, background: BackgroundTasks
) -> MessageOut:
    token_hash = auth_service.hash_refresh_token(payload.token)
    row = (
        await session.execute(
            select(OneTimeToken).where(
                OneTimeToken.token_hash == token_hash,
                OneTimeToken.purpose == OneTimeTokenPurpose.password_reset,
            )
        )
    ).scalar_one_or_none()

    if row is None or row.used_at is not None or row.expires_at <= datetime.now(UTC):
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            "That reset link is invalid or has expired. Request a new one.",
        )

    try:
        password_hash = auth_service.hash_password(payload.password)
    except AuthError as exc:
        # Leave the token usable — the link is fine, the new password was not.
        raise HTTPException(status.HTTP_400_BAD_REQUEST, str(exc)) from exc

    user = await session.get(User, row.user_id)
    if user is None or not user.is_active:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "Account unavailable.")

    row.used_at = datetime.now(UTC)
    user.password_hash = password_hash

    # A reset is the remedy for a compromised account, so it must also evict
    # whoever else is holding a session.
    await session.execute(
        update(RefreshToken)
        .where(RefreshToken.user_id == user.id, RefreshToken.revoked_at.is_(None))
        .values(revoked_at=func.now())
    )

    # Reaching the reset link proves control of the mailbox.
    if user.email_verified_at is None:
        user.email_verified_at = datetime.now(UTC)

    background.add_task(email_service.send, email_service.password_changed_email(user.email))
    return MessageOut(message="Password updated. Sign in with your new password.")
