"""Authentication: password hashing and JWT issuance.

Design notes:

- Access tokens are short-lived and stateless; refresh tokens are long-lived and
  stateful. That combination means a logout or a stolen-token revocation takes
  effect within one access-token lifetime rather than never.
- Refresh tokens rotate on every use. If an old one is presented again it means
  the token was replayed, so the whole family is revoked — this is what turns a
  silent theft into a detectable event.
- Only a hash of the refresh token is stored. A database leak must not hand the
  attacker working credentials.
"""

from __future__ import annotations

import hashlib
import logging
import secrets
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

import bcrypt
import jwt

from app.config import settings

logger = logging.getLogger(__name__)

ACCESS_TOKEN_TYPE = "access"  # noqa: S105 - a type label, not a credential
# bcrypt silently truncates beyond 72 bytes; reject rather than accept a
# password whose tail is ignored.
BCRYPT_MAX_BYTES = 72


class AuthError(Exception):
    """Raised for any credential or token failure. The message is safe to show."""


@dataclass(slots=True)
class TokenPair:
    access_token: str
    refresh_token: str
    expires_in: int


# ------------------------------------------------------------- passwords ----
def hash_password(password: str) -> str:
    validate_password_strength(password)
    return bcrypt.hashpw(password.encode(), bcrypt.gensalt(rounds=12)).decode()


def verify_password(password: str, password_hash: str) -> bool:
    if not password_hash:
        return False
    try:
        return bcrypt.checkpw(password.encode()[:BCRYPT_MAX_BYTES], password_hash.encode())
    except ValueError:
        return False


def validate_password_strength(password: str) -> None:
    if len(password) < settings.password_min_length:
        raise AuthError(
            f"Password must be at least {settings.password_min_length} characters."
        )
    if len(password.encode()) > BCRYPT_MAX_BYTES:
        raise AuthError("Password must be at most 72 bytes.")
    # Deliberately not demanding symbol/case classes: length beats composition
    # rules, and complexity requirements push people toward reused passwords.
    if password.lower() in _COMMON_PASSWORDS:
        raise AuthError("That password is too common. Choose something less predictable.")


_COMMON_PASSWORDS = {
    "password123", "12345678910", "qwertyuiop123", "administrator",
    "letmein12345", "welcome12345", "iloveyou1234", "passw0rd123",
}


def dummy_verify() -> None:
    """Burn a comparable amount of CPU when the account does not exist, so
    response timing cannot be used to enumerate registered emails."""
    bcrypt.checkpw(
        b"timing-equalizer",
        b"$2b$12$C6UzMDM.H6dfI/f/IKcEe.7Q7bK5Y0z0iZ0jZ0jZ0jZ0jZ0jZ0jZ0",
    )


# ---------------------------------------------------------- access token ----
def create_access_token(user_id: uuid.UUID, email: str) -> tuple[str, int]:
    expires_in = settings.access_token_ttl_minutes * 60
    now = datetime.now(UTC)
    payload = {
        "sub": str(user_id),
        "email": email,
        "type": ACCESS_TOKEN_TYPE,
        "iat": int(now.timestamp()),
        "exp": int((now + timedelta(seconds=expires_in)).timestamp()),
        "jti": secrets.token_hex(8),
    }
    token = jwt.encode(payload, settings.effective_jwt_secret, algorithm=settings.jwt_algorithm)
    return token, expires_in


def decode_access_token(token: str) -> dict:
    try:
        payload = jwt.decode(
            token,
            settings.effective_jwt_secret,
            algorithms=[settings.jwt_algorithm],
            options={"require": ["exp", "sub", "type"]},
        )
    except jwt.ExpiredSignatureError as exc:
        raise AuthError("Session expired.") from exc
    except jwt.InvalidTokenError as exc:
        raise AuthError("Invalid token.") from exc

    if payload.get("type") != ACCESS_TOKEN_TYPE:
        # Refusing a refresh token here stops it being used as a bearer token,
        # which would sidestep the short access-token lifetime entirely.
        raise AuthError("Wrong token type.")
    return payload


# --------------------------------------------------------- refresh token ----
def generate_refresh_token() -> tuple[str, str]:
    """Returns (plaintext, sha256). Only the hash is persisted."""
    raw = secrets.token_urlsafe(48)
    return raw, hash_refresh_token(raw)


def hash_refresh_token(raw: str) -> str:
    return hashlib.sha256(raw.encode()).hexdigest()


def refresh_expiry() -> datetime:
    return datetime.now(UTC) + timedelta(days=settings.refresh_token_ttl_days)


# -------------------------------------------------------- one-time token ----
def generate_one_time_token() -> tuple[str, str]:
    """Returns (plaintext, sha256). The plaintext goes in the emailed link and
    is never persisted."""
    raw = secrets.token_urlsafe(32)
    return raw, hash_refresh_token(raw)


def verification_expiry() -> datetime:
    return datetime.now(UTC) + timedelta(hours=settings.verification_token_ttl_hours)


def reset_expiry() -> datetime:
    return datetime.now(UTC) + timedelta(minutes=settings.reset_token_ttl_minutes)


def normalize_email(email: str) -> str:
    email = (email or "").strip().lower()
    if "@" not in email or len(email) > 320 or email.startswith("@") or email.endswith("@"):
        raise AuthError("Enter a valid email address.")
    return email
