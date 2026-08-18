"""Transactional email content and the token expiry windows."""

from __future__ import annotations

from datetime import UTC, datetime

from app.config import settings
from app.services import auth, email


def test_verification_link_points_at_the_app(monkeypatch):
    monkeypatch.setattr(settings, "app_base_url", "https://rag.example.com/")
    message = email.verification_email("a@b.com", "tok-123")

    assert "https://rag.example.com/verify-email?token=tok-123" in message.text
    assert "https://rag.example.com/verify-email?token=tok-123" in message.html
    assert message.to == "a@b.com"


def test_reset_link_points_at_the_reset_route(monkeypatch):
    monkeypatch.setattr(settings, "app_base_url", "https://rag.example.com")
    message = email.password_reset_email("a@b.com", "tok-456")

    assert "https://rag.example.com/reset-password?token=tok-456" in message.text


def test_token_with_url_unsafe_characters_is_escaped(monkeypatch):
    monkeypatch.setattr(settings, "app_base_url", "https://x.test")
    message = email.verification_email("a@b.com", "a b&c=d")

    # An unescaped '&' would truncate the token at the query-string boundary.
    assert "a%20b%26c%3Dd" in message.text
    assert "a b&c=d" not in message.text


def test_both_bodies_are_populated():
    message = email.verification_email("a@b.com", "t")
    assert message.text.strip()
    assert message.html.strip().startswith("<!doctype html>")
    assert message.subject


def test_log_backend_reports_success(monkeypatch):
    monkeypatch.setattr(settings, "email_backend", "log")
    assert email.send(email.verification_email("a@b.com", "t")) is True


def test_disabled_backend_reports_not_sent(monkeypatch):
    monkeypatch.setattr(settings, "email_backend", "none")
    assert email.send(email.verification_email("a@b.com", "t")) is False


def test_ses_failure_never_raises(monkeypatch):
    """A failed email must not roll back an account that was created."""
    monkeypatch.setattr(settings, "email_backend", "ses")

    class Boom:
        def send_email(self, **_: object) -> None:
            raise RuntimeError("SES is unavailable")

    monkeypatch.setattr(email, "_ses", lambda: Boom())
    assert email.send(email.verification_email("a@b.com", "t")) is False


def test_password_changed_notice_has_no_link():
    """It is a warning, not an action — a link here would train people to click
    through security notices."""
    message = email.password_changed_email("a@b.com")
    assert "http" not in message.text


# ------------------------------------------------------------- expiries ----
def test_one_time_tokens_are_unique_and_hashed():
    raw_a, hash_a = auth.generate_one_time_token()
    raw_b, hash_b = auth.generate_one_time_token()

    assert raw_a != raw_b
    assert hash_a != hash_b
    assert hash_a == auth.hash_refresh_token(raw_a)
    assert len(hash_a) == 64


def test_reset_window_is_shorter_than_verification(monkeypatch):
    """A live password-reset link is the more dangerous of the two, so it
    should not sit in an inbox for a day."""
    now = datetime.now(UTC)
    assert (auth.reset_expiry() - now) < (auth.verification_expiry() - now)


def test_expiries_honour_configuration(monkeypatch):
    monkeypatch.setattr(settings, "reset_token_ttl_minutes", 5)
    monkeypatch.setattr(settings, "verification_token_ttl_hours", 2)

    now = datetime.now(UTC)
    assert 4 <= (auth.reset_expiry() - now).total_seconds() / 60 <= 6
    assert 1.9 <= (auth.verification_expiry() - now).total_seconds() / 3600 <= 2.1
