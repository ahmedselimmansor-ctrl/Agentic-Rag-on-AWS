"""Password handling and token semantics."""

from __future__ import annotations

import uuid

import pytest

from app.config import settings
from app.services import auth


# ------------------------------------------------------------- passwords ---
def test_hash_is_salted_and_verifies():
    password = "correct horse battery staple"
    a, b = auth.hash_password(password), auth.hash_password(password)

    assert a != b, "identical passwords must not produce identical hashes"
    assert auth.verify_password(password, a)
    assert auth.verify_password(password, b)


def test_wrong_password_is_rejected():
    stored = auth.hash_password("the right passphrase")
    assert not auth.verify_password("the wrong passphrase", stored)


def test_empty_or_malformed_hash_never_verifies():
    assert not auth.verify_password("anything", "")
    assert not auth.verify_password("anything", "not-a-bcrypt-hash")


def test_short_password_is_refused():
    with pytest.raises(auth.AuthError, match="at least"):
        auth.hash_password("short")


def test_password_beyond_bcrypt_limit_is_refused():
    # bcrypt silently ignores bytes past 72; accepting such a password would
    # mean the tail never contributes to security.
    with pytest.raises(auth.AuthError, match="72 bytes"):
        auth.hash_password("x" * 73)


def test_common_password_is_refused():
    with pytest.raises(auth.AuthError, match="too common"):
        auth.hash_password("password123")


# ---------------------------------------------------------- access token ---
def test_access_token_roundtrips():
    user_id = uuid.uuid4()
    token, expires_in = auth.create_access_token(user_id, "a@b.com")
    payload = auth.decode_access_token(token)

    assert payload["sub"] == str(user_id)
    assert payload["email"] == "a@b.com"
    assert payload["type"] == auth.ACCESS_TOKEN_TYPE
    assert expires_in == settings.access_token_ttl_minutes * 60


def test_tampered_token_is_rejected():
    token, _ = auth.create_access_token(uuid.uuid4(), "a@b.com")
    head, payload, sig = token.split(".")
    forged = f"{head}.{payload}.{'A' * len(sig)}"

    with pytest.raises(auth.AuthError, match="Invalid token"):
        auth.decode_access_token(forged)


def test_expired_token_is_rejected(monkeypatch):
    monkeypatch.setattr(settings, "access_token_ttl_minutes", -1)
    token, _ = auth.create_access_token(uuid.uuid4(), "a@b.com")

    with pytest.raises(auth.AuthError, match="expired"):
        auth.decode_access_token(token)


def test_garbage_is_rejected():
    with pytest.raises(auth.AuthError):
        auth.decode_access_token("not.a.token")


def test_token_signed_with_another_key_is_rejected():
    import jwt

    forged = jwt.encode(
        {"sub": str(uuid.uuid4()), "type": "access", "exp": 9_999_999_999},
        "an-attacker-chosen-key-long-enough-to-avoid-a-length-warning",
        algorithm="HS256",
    )
    with pytest.raises(auth.AuthError):
        auth.decode_access_token(forged)


def test_refresh_token_cannot_be_used_as_a_bearer_token():
    """A refresh token presented as an access token would bypass the short
    access-token lifetime entirely."""
    import jwt

    smuggled = jwt.encode(
        {"sub": str(uuid.uuid4()), "type": "refresh", "exp": 9_999_999_999},
        settings.effective_jwt_secret,
        algorithm=settings.jwt_algorithm,
    )
    with pytest.raises(auth.AuthError, match="Wrong token type"):
        auth.decode_access_token(smuggled)


# --------------------------------------------------------- refresh token ---
def test_refresh_tokens_are_unique_and_hashed():
    raw_a, hash_a = auth.generate_refresh_token()
    raw_b, hash_b = auth.generate_refresh_token()

    assert raw_a != raw_b
    assert hash_a != hash_b
    assert hash_a == auth.hash_refresh_token(raw_a)
    assert raw_a not in hash_a, "the plaintext must not be recoverable from the stored hash"
    assert len(hash_a) == 64


def test_refresh_expiry_is_in_the_future():
    from datetime import UTC, datetime

    assert auth.refresh_expiry() > datetime.now(UTC)


# ---------------------------------------------------------------- email ---
@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("  User@Example.COM ", "user@example.com"),
        ("a@b.co", "a@b.co"),
    ],
)
def test_email_is_normalized(raw, expected):
    assert auth.normalize_email(raw) == expected


@pytest.mark.parametrize("raw", ["", "no-at-sign", "@leading", "trailing@", "   "])
def test_invalid_email_is_refused(raw):
    with pytest.raises(auth.AuthError):
        auth.normalize_email(raw)
