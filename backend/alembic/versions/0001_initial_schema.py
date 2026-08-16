"""initial schema: users, conversations, messages, documents, chunks, memory

Revision ID: 0001
Revises:
Create Date: 2026-08-15
"""
from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from pgvector.sqlalchemy import Vector
from sqlalchemy.dialects import postgresql

from app.config import settings

revision: str = "0001"
down_revision: str | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

DIM = settings.embedding_dim

TSV_EXPR = "to_tsvector('english', coalesce(context_header, '') || ' ' || content)"


def upgrade() -> None:
    op.execute("CREATE EXTENSION IF NOT EXISTS vector")
    op.execute("CREATE EXTENSION IF NOT EXISTS pg_trgm")
    op.execute('CREATE EXTENSION IF NOT EXISTS "pgcrypto"')  # gen_random_uuid()

    message_role = postgresql.ENUM(
        "user", "assistant", "system", "tool", name="message_role", create_type=False
    )
    document_status = postgresql.ENUM(
        "pending", "parsing", "chunking", "embedding", "ready", "failed",
        name="document_status", create_type=False,
    )
    memory_kind = postgresql.ENUM(
        "fact", "preference", "entity", name="memory_kind", create_type=False
    )
    message_role.create(op.get_bind(), checkfirst=True)
    document_status.create(op.get_bind(), checkfirst=True)
    memory_kind.create(op.get_bind(), checkfirst=True)

    # ------------------------------------------------------------- users ----
    op.create_table(
        "users",
        sa.Column("id", postgresql.UUID(as_uuid=True), server_default=sa.text("gen_random_uuid()"), nullable=False),
        sa.Column("email", sa.String(320), nullable=False),
        sa.Column("display_name", sa.String(200)),
        sa.Column("settings_json", postgresql.JSONB, server_default="{}", nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("email"),
    )
    op.create_index("ix_users_created_at", "users", ["created_at"])

    # ----------------------------------------------------- conversations ----
    op.create_table(
        "conversations",
        sa.Column("id", postgresql.UUID(as_uuid=True), server_default=sa.text("gen_random_uuid()"), nullable=False),
        sa.Column("user_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("title", sa.String(300), server_default="New chat", nullable=False),
        sa.Column("archived", sa.Boolean, server_default="false", nullable=False),
        sa.Column("summary", sa.Text),
        sa.Column("summarized_through", sa.Integer, server_default="0", nullable=False),
        sa.Column("last_message_at", sa.DateTime(timezone=True)),
        sa.Column("metadata_json", postgresql.JSONB, server_default="{}", nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
    )
    op.create_index("ix_conversations_created_at", "conversations", ["created_at"])
    op.create_index(
        "ix_conversations_user_recent",
        "conversations",
        ["user_id", "archived", "last_message_at"],
    )

    # --------------------------------------------------------- messages ----
    op.create_table(
        "messages",
        sa.Column("id", postgresql.UUID(as_uuid=True), server_default=sa.text("gen_random_uuid()"), nullable=False),
        sa.Column("conversation_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("ordinal", sa.Integer, nullable=False),
        sa.Column("role", message_role, nullable=False),
        sa.Column("content", sa.Text, server_default="", nullable=False),
        sa.Column("sources_json", postgresql.JSONB, server_default="[]", nullable=False),
        sa.Column("tool_calls_json", postgresql.JSONB, server_default="[]", nullable=False),
        sa.Column("attachments_json", postgresql.JSONB, server_default="[]", nullable=False),
        sa.Column("prompt_tokens", sa.Integer, server_default="0", nullable=False),
        sa.Column("completion_tokens", sa.Integer, server_default="0", nullable=False),
        sa.Column("latency_ms", sa.Integer, server_default="0", nullable=False),
        sa.Column("error", sa.Text),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.ForeignKeyConstraint(["conversation_id"], ["conversations.id"], ondelete="CASCADE"),
        sa.UniqueConstraint("conversation_id", "ordinal", name="uq_message_ordinal"),
    )
    op.create_index("ix_messages_created_at", "messages", ["created_at"])
    op.create_index(
        "ix_messages_conversation_ordinal", "messages", ["conversation_id", "ordinal"]
    )

    # -------------------------------------------------------- documents ----
    op.create_table(
        "documents",
        sa.Column("id", postgresql.UUID(as_uuid=True), server_default=sa.text("gen_random_uuid()"), nullable=False),
        sa.Column("user_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("conversation_id", postgresql.UUID(as_uuid=True)),
        sa.Column("filename", sa.String(500), nullable=False),
        sa.Column("mime_type", sa.String(200), server_default="application/octet-stream", nullable=False),
        sa.Column("size_bytes", sa.BigInteger, server_default="0", nullable=False),
        sa.Column("storage_uri", sa.Text, nullable=False),
        sa.Column("sha256", sa.String(64)),
        sa.Column("status", document_status, server_default="pending", nullable=False),
        sa.Column("error", sa.Text),
        sa.Column("page_count", sa.Integer, server_default="0", nullable=False),
        sa.Column("chunk_count", sa.Integer, server_default="0", nullable=False),
        sa.Column("metadata_json", postgresql.JSONB, server_default="{}", nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["conversation_id"], ["conversations.id"], ondelete="CASCADE"),
        sa.UniqueConstraint("user_id", "sha256", name="uq_document_user_sha"),
    )
    op.create_index("ix_documents_created_at", "documents", ["created_at"])
    op.create_index("ix_documents_sha256", "documents", ["sha256"])
    op.create_index("ix_documents_user_conv", "documents", ["user_id", "conversation_id"])

    # ----------------------------------------------------------- chunks ----
    op.create_table(
        "chunks",
        sa.Column("id", postgresql.UUID(as_uuid=True), server_default=sa.text("gen_random_uuid()"), nullable=False),
        sa.Column("document_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("user_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("conversation_id", postgresql.UUID(as_uuid=True)),
        sa.Column("ordinal", sa.Integer, nullable=False),
        sa.Column("content", sa.Text, nullable=False),
        sa.Column("context_header", sa.Text),
        sa.Column("token_count", sa.Integer, server_default="0", nullable=False),
        sa.Column("page_from", sa.Integer),
        sa.Column("page_to", sa.Integer),
        sa.Column("modality", sa.String(20), server_default="text", nullable=False),
        sa.Column("image_uri", sa.Text),
        sa.Column("embedding", Vector(DIM)),
        sa.Column("tsv", postgresql.TSVECTOR, sa.Computed(TSV_EXPR, persisted=True)),
        sa.Column("metadata_json", postgresql.JSONB, server_default="{}", nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.ForeignKeyConstraint(["document_id"], ["documents.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["conversation_id"], ["conversations.id"], ondelete="CASCADE"),
        sa.UniqueConstraint("document_id", "ordinal", name="uq_chunk_ordinal"),
        sa.CheckConstraint("modality in ('text','image')", name="ck_chunk_modality"),
    )
    op.create_index("ix_chunks_created_at", "chunks", ["created_at"])
    op.create_index("ix_chunks_user_conv", "chunks", ["user_id", "conversation_id"])

    # Dense index. HNSW beats IVFFlat on recall-per-latency and needs no training
    # pass, so it is correct even on an empty table at deploy time.
    op.execute(
        "CREATE INDEX ix_chunks_embedding_hnsw ON chunks "
        "USING hnsw (embedding vector_cosine_ops) "
        "WITH (m = 16, ef_construction = 64)"
    )
    # Sparse index.
    op.execute("CREATE INDEX ix_chunks_tsv ON chunks USING gin (tsv)")
    # Trigram index for filename/substring lookups in the UI.
    op.execute("CREATE INDEX ix_documents_filename_trgm ON documents USING gin (filename gin_trgm_ops)")

    # ------------------------------------------------ long_term_memories ----
    op.create_table(
        "long_term_memories",
        sa.Column("id", postgresql.UUID(as_uuid=True), server_default=sa.text("gen_random_uuid()"), nullable=False),
        sa.Column("user_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("kind", memory_kind, server_default="fact", nullable=False),
        sa.Column("content", sa.Text, nullable=False),
        sa.Column("embedding", Vector(DIM)),
        sa.Column("salience", sa.Float, server_default="0.5", nullable=False),
        sa.Column("source_conversation_id", postgresql.UUID(as_uuid=True)),
        sa.Column("last_used_at", sa.DateTime(timezone=True)),
        sa.Column("use_count", sa.Integer, server_default="0", nullable=False),
        sa.Column("superseded_by", postgresql.UUID(as_uuid=True)),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["source_conversation_id"], ["conversations.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["superseded_by"], ["long_term_memories.id"], ondelete="SET NULL"),
    )
    op.create_index("ix_long_term_memories_created_at", "long_term_memories", ["created_at"])
    op.create_index("ix_ltm_user_active", "long_term_memories", ["user_id", "superseded_by"])
    op.execute(
        "CREATE INDEX ix_ltm_embedding_hnsw ON long_term_memories "
        "USING hnsw (embedding vector_cosine_ops) "
        "WITH (m = 16, ef_construction = 64)"
    )

    # --------------------------------------------------- tool_invocations ---
    op.create_table(
        "tool_invocations",
        sa.Column("id", postgresql.UUID(as_uuid=True), server_default=sa.text("gen_random_uuid()"), nullable=False),
        sa.Column("message_id", postgresql.UUID(as_uuid=True)),
        sa.Column("conversation_id", postgresql.UUID(as_uuid=True)),
        sa.Column("tool_name", sa.String(100), nullable=False),
        sa.Column("arguments_json", postgresql.JSONB, server_default="{}", nullable=False),
        sa.Column("result_json", postgresql.JSONB, server_default="{}", nullable=False),
        sa.Column("ok", sa.Boolean, server_default="true", nullable=False),
        sa.Column("error", sa.Text),
        sa.Column("duration_ms", sa.Integer, server_default="0", nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.ForeignKeyConstraint(["message_id"], ["messages.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["conversation_id"], ["conversations.id"], ondelete="CASCADE"),
    )
    op.create_index("ix_tool_invocations_created_at", "tool_invocations", ["created_at"])
    op.create_index("ix_tool_inv_conv", "tool_invocations", ["conversation_id", "created_at"])


def downgrade() -> None:
    op.drop_table("tool_invocations")
    op.drop_table("long_term_memories")
    op.drop_table("chunks")
    op.drop_table("documents")
    op.drop_table("messages")
    op.drop_table("conversations")
    op.drop_table("users")
    for enum_name in ("memory_kind", "document_status", "message_role"):
        op.execute(f"DROP TYPE IF EXISTS {enum_name}")
