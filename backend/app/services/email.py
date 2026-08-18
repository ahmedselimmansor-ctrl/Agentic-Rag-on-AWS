"""Transactional email — verification and password reset.

Backends: `ses` for real delivery, `log` for local development (the message,
link included, goes to the logs so the flow is testable without a verified
domain), and `none` to disable outbound mail entirely.

Sending never raises into a request handler. A failed email must not roll back
an account that was otherwise created successfully — the user can request
another one.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from urllib.parse import quote

from app.config import settings

logger = logging.getLogger(__name__)


@dataclass(slots=True)
class Email:
    to: str
    subject: str
    text: str
    html: str


def _ses():  # noqa: ANN202 - boto3 client is untyped
    import boto3

    return boto3.client("ses", region_name=settings.aws_region)


def send(email: Email) -> bool:
    """Returns True when the message was handed off. Never raises."""
    if settings.email_backend == "none":
        logger.info("email disabled, not sending %r to %s", email.subject, email.to)
        return False

    if settings.email_backend == "log":
        # The link is the whole point in dev — log the body, not just a summary.
        logger.info(
            "[email:log] to=%s subject=%r\n%s", email.to, email.subject, email.text
        )
        return True

    try:
        _ses().send_email(
            Source=f"{settings.email_from_name} <{settings.email_from}>",
            Destination={"ToAddresses": [email.to]},
            Message={
                "Subject": {"Data": email.subject, "Charset": "UTF-8"},
                "Body": {
                    "Text": {"Data": email.text, "Charset": "UTF-8"},
                    "Html": {"Data": email.html, "Charset": "UTF-8"},
                },
            },
        )
        return True
    except Exception as exc:  # noqa: BLE001
        logger.error("failed to send %r to %s: %s", email.subject, email.to, exc)
        return False


def _link(path: str, token: str) -> str:
    return f"{settings.app_base_url.rstrip('/')}{path}?token={quote(token)}"


def _wrap(title: str, body_html: str) -> str:
    return f"""<!doctype html>
<html><body style="margin:0;padding:24px;background:#faf9f7;
font-family:-apple-system,'Segoe UI',Roboto,sans-serif;color:#1c1a17">
<div style="max-width:520px;margin:0 auto;background:#fff;border:1px solid #e2ded7;
border-radius:14px;padding:28px">
<h1 style="margin:0 0 14px;font-size:20px">{title}</h1>
{body_html}
<p style="margin:26px 0 0;font-size:12px;color:#8b857c">
If you did not request this, you can ignore this message.</p>
</div></body></html>"""


def _button(url: str, label: str) -> str:
    return (
        f'<p style="margin:22px 0"><a href="{url}" '
        'style="display:inline-block;padding:10px 18px;background:#b4553a;color:#fff;'
        'border-radius:8px;text-decoration:none;font-weight:600">'
        f"{label}</a></p>"
        f'<p style="margin:0;font-size:12px;color:#8b857c;word-break:break-all">'
        f"Or paste this into your browser: {url}</p>"
    )


def verification_email(to: str, token: str) -> Email:
    url = _link("/verify-email", token)
    hours = settings.verification_token_ttl_hours
    return Email(
        to=to,
        subject="Confirm your email address",
        text=(
            f"Confirm your email address to finish setting up your account.\n\n{url}\n\n"
            f"This link expires in {hours} hours."
        ),
        html=_wrap(
            "Confirm your email address",
            '<p style="margin:0;font-size:14px;line-height:1.6">'
            "Confirm your address to finish setting up your account.</p>"
            + _button(url, "Confirm email")
            + f'<p style="margin:14px 0 0;font-size:12px;color:#8b857c">'
            f"This link expires in {hours} hours.</p>",
        ),
    )


def password_reset_email(to: str, token: str) -> Email:
    url = _link("/reset-password", token)
    minutes = settings.reset_token_ttl_minutes
    return Email(
        to=to,
        subject="Reset your password",
        text=(
            f"Use this link to choose a new password.\n\n{url}\n\n"
            f"It expires in {minutes} minutes. Signing in with your existing "
            "password will also cancel it."
        ),
        html=_wrap(
            "Reset your password",
            '<p style="margin:0;font-size:14px;line-height:1.6">'
            "Use the link below to choose a new password.</p>"
            + _button(url, "Choose a new password")
            + f'<p style="margin:14px 0 0;font-size:12px;color:#8b857c">'
            f"Expires in {minutes} minutes.</p>",
        ),
    )


def password_changed_email(to: str) -> Email:
    """Sent after a successful reset — the alarm bell if it was not the user."""
    return Email(
        to=to,
        subject="Your password was changed",
        text=(
            "Your password was just changed and all other sessions were signed "
            "out.\n\nIf this was not you, reset your password immediately."
        ),
        html=_wrap(
            "Your password was changed",
            '<p style="margin:0;font-size:14px;line-height:1.6">'
            "Your password was just changed, and every other session was signed out.<br>"
            "<strong>If this was not you, reset your password immediately.</strong></p>",
        ),
    )
