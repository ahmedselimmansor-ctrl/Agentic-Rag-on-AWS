"""email verification and password reset tokens

Revision ID: 0003
Revises: 0002
Create Date: 2026-08-18
"""
from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0003"
down_revision: str | None = "0002"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "users", sa.Column("email_verified_at", sa.DateTime(timezone=True), nullable=True)
    )

    purpose = postgresql.ENUM(
        "email_verification",
        "password_reset",
        name="one_time_token_purpose",
        create_type=False,
    )
    purpose.create(op.get_bind(), checkfirst=True)

    op.create_table(
        "one_time_tokens",
        sa.Column("id", postgresql.UUID(as_uuid=True), server_default=sa.text("gen_random_uuid()"), nullable=False),
        sa.Column("user_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("purpose", purpose, nullable=False),
        sa.Column("token_hash", sa.String(64), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("used_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
        sa.UniqueConstraint("token_hash", name="uq_one_time_token_hash"),
    )
    op.create_index("ix_one_time_tokens_created_at", "one_time_tokens", ["created_at"])
    op.create_index(
        "ix_one_time_tokens_user_purpose",
        "one_time_tokens",
        ["user_id", "purpose", "used_at"],
    )

    # Existing accounts predate verification; treating them as unverified would
    # lock out every current user the moment REQUIRE_EMAIL_VERIFICATION is set.
    op.execute("UPDATE users SET email_verified_at = created_at WHERE password_hash IS NOT NULL")


def downgrade() -> None:
    op.drop_index("ix_one_time_tokens_user_purpose", table_name="one_time_tokens")
    op.drop_index("ix_one_time_tokens_created_at", table_name="one_time_tokens")
    op.drop_table("one_time_tokens")
    op.execute("DROP TYPE IF EXISTS one_time_token_purpose")
    op.drop_column("users", "email_verified_at")
