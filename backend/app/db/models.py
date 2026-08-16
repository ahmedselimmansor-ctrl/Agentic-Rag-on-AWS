"""ORM models — conversations, messages, documents, chunks, long-term memory."""

from __future__ import annotations

import enum
import uuid
from datetime import datetime

from pgvector.sqlalchemy import Vector
from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    Computed,
    DateTime,
    Enum,
    Float,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.dialects.postgresql import JSONB, TSVECTOR
from sqlalchemy.dialects.postgresql import UUID as PGUUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.config import settings
from app.db.base import Base, TimestampMixin, UUIDMixin

EMBEDDING_DIM = settings.embedding_dim


class Role(str, enum.Enum):
    user = "user"
    assistant = "assistant"
    system = "system"
    tool = "tool"


class DocumentStatus(str, enum.Enum):
    pending = "pending"
    parsing = "parsing"
    chunking = "chunking"
    embedding = "embedding"
    ready = "ready"
    failed = "failed"


class MemoryKind(str, enum.Enum):
    fact = "fact"          # durable statement about the user or their domain
    preference = "preference"  # how the user wants answers shaped
    entity = "entity"      # a named thing the user keeps referring to


# --------------------------------------------------------------------- user --
class User(Base, UUIDMixin, TimestampMixin):
    __tablename__ = "users"

    email: Mapped[str] = mapped_column(String(320), unique=True, nullable=False)
    display_name: Mapped[str | None] = mapped_column(String(200))
    settings_json: Mapped[dict] = mapped_column(JSONB, default=dict, server_default="{}")

    conversations: Mapped[list[Conversation]] = relationship(
        back_populates="user", cascade="all, delete-orphan"
    )


# ------------------------------------------------------------- conversation --
class Conversation(Base, UUIDMixin, TimestampMixin):
    """One chat thread. `summary` is the rolling short-term-memory compaction."""

    __tablename__ = "conversations"

    user_id: Mapped[uuid.UUID] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), nullable=False
    )
    title: Mapped[str] = mapped_column(String(300), default="New chat")
    archived: Mapped[bool] = mapped_column(Boolean, default=False, server_default="false")
    # Rolling summary of messages older than the verbatim window.
    summary: Mapped[str | None] = mapped_column(Text)
    # Highest message ordinal already folded into `summary`.
    summarized_through: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    last_message_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    metadata_json: Mapped[dict] = mapped_column(JSONB, default=dict, server_default="{}")

    user: Mapped[User] = relationship(back_populates="conversations")
    messages: Mapped[list[Message]] = relationship(
        back_populates="conversation",
        cascade="all, delete-orphan",
        order_by="Message.ordinal",
    )

    __table_args__ = (
        Index("ix_conversations_user_recent", "user_id", "archived", "last_message_at"),
    )


class Message(Base, UUIDMixin, TimestampMixin):
    __tablename__ = "messages"

    conversation_id: Mapped[uuid.UUID] = mapped_column(
        PGUUID(as_uuid=True),
        ForeignKey("conversations.id", ondelete="CASCADE"),
        nullable=False,
    )
    # Monotonic per-conversation position; drives history windowing + summarization.
    ordinal: Mapped[int] = mapped_column(Integer, nullable=False)
    role: Mapped[Role] = mapped_column(Enum(Role, name="message_role"), nullable=False)
    content: Mapped[str] = mapped_column(Text, default="")
    # Citations shown under the assistant bubble.
    sources_json: Mapped[list] = mapped_column(JSONB, default=list, server_default="[]")
    # Agent trace: tool calls + their results, for the "how did it answer" panel.
    tool_calls_json: Mapped[list] = mapped_column(JSONB, default=list, server_default="[]")
    attachments_json: Mapped[list] = mapped_column(JSONB, default=list, server_default="[]")
    prompt_tokens: Mapped[int] = mapped_column(Integer, default=0)
    completion_tokens: Mapped[int] = mapped_column(Integer, default=0)
    latency_ms: Mapped[int] = mapped_column(Integer, default=0)
    error: Mapped[str | None] = mapped_column(Text)

    conversation: Mapped[Conversation] = relationship(back_populates="messages")

    __table_args__ = (
        UniqueConstraint("conversation_id", "ordinal", name="uq_message_ordinal"),
        Index("ix_messages_conversation_ordinal", "conversation_id", "ordinal"),
    )


# ----------------------------------------------------------------- document --
class Document(Base, UUIDMixin, TimestampMixin):
    __tablename__ = "documents"

    user_id: Mapped[uuid.UUID] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), nullable=False
    )
    # Null => document is in the user's global corpus, not scoped to one thread.
    conversation_id: Mapped[uuid.UUID | None] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("conversations.id", ondelete="CASCADE")
    )
    filename: Mapped[str] = mapped_column(String(500), nullable=False)
    mime_type: Mapped[str] = mapped_column(String(200), default="application/octet-stream")
    size_bytes: Mapped[int] = mapped_column(BigInteger, default=0)
    # s3://bucket/key or file:///path
    storage_uri: Mapped[str] = mapped_column(Text, nullable=False)
    sha256: Mapped[str | None] = mapped_column(String(64), index=True)
    status: Mapped[DocumentStatus] = mapped_column(
        Enum(DocumentStatus, name="document_status"), default=DocumentStatus.pending
    )
    error: Mapped[str | None] = mapped_column(Text)
    page_count: Mapped[int] = mapped_column(Integer, default=0)
    chunk_count: Mapped[int] = mapped_column(Integer, default=0)
    metadata_json: Mapped[dict] = mapped_column(JSONB, default=dict, server_default="{}")

    chunks: Mapped[list[Chunk]] = relationship(
        back_populates="document", cascade="all, delete-orphan"
    )

    __table_args__ = (
        Index("ix_documents_user_conv", "user_id", "conversation_id"),
        UniqueConstraint("user_id", "sha256", name="uq_document_user_sha"),
    )


class Chunk(Base, UUIDMixin, TimestampMixin):
    """A retrievable passage. Carries both the dense vector and the sparse tsvector."""

    __tablename__ = "chunks"

    document_id: Mapped[uuid.UUID] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("documents.id", ondelete="CASCADE"), nullable=False
    )
    user_id: Mapped[uuid.UUID] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), nullable=False
    )
    conversation_id: Mapped[uuid.UUID | None] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("conversations.id", ondelete="CASCADE")
    )
    ordinal: Mapped[int] = mapped_column(Integer, nullable=False)
    content: Mapped[str] = mapped_column(Text, nullable=False)
    # Heading trail / caption prepended at embed time so the vector carries context.
    context_header: Mapped[str | None] = mapped_column(Text)
    token_count: Mapped[int] = mapped_column(Integer, default=0)
    page_from: Mapped[int | None] = mapped_column(Integer)
    page_to: Mapped[int | None] = mapped_column(Integer)
    # "text" | "image" — image chunks embed the picture itself via the vision model.
    modality: Mapped[str] = mapped_column(String(20), default="text", server_default="text")
    image_uri: Mapped[str | None] = mapped_column(Text)
    embedding: Mapped[list[float] | None] = mapped_column(Vector(EMBEDDING_DIM))
    # Postgres maintains this; SQLAlchemy omits Computed columns from INSERT/UPDATE.
    # The header is included so a heading match scores as a body match would.
    tsv: Mapped[str | None] = mapped_column(
        TSVECTOR,
        Computed(
            "to_tsvector('english', coalesce(context_header, '') || ' ' || content)",
            persisted=True,
        ),
    )
    metadata_json: Mapped[dict] = mapped_column(JSONB, default=dict, server_default="{}")

    document: Mapped[Document] = relationship(back_populates="chunks")

    __table_args__ = (
        UniqueConstraint("document_id", "ordinal", name="uq_chunk_ordinal"),
        Index("ix_chunks_user_conv", "user_id", "conversation_id"),
        CheckConstraint("modality in ('text','image')", name="ck_chunk_modality"),
    )


# ------------------------------------------------------------ long-term mem --
class LongTermMemory(Base, UUIDMixin, TimestampMixin):
    """Durable, user-scoped facts distilled from conversations and recalled by vector search."""

    __tablename__ = "long_term_memories"

    user_id: Mapped[uuid.UUID] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), nullable=False
    )
    kind: Mapped[MemoryKind] = mapped_column(
        Enum(MemoryKind, name="memory_kind"), default=MemoryKind.fact
    )
    content: Mapped[str] = mapped_column(Text, nullable=False)
    embedding: Mapped[list[float] | None] = mapped_column(Vector(EMBEDDING_DIM))
    # 0..1 — decays over time, boosted on recall; drives eviction.
    salience: Mapped[float] = mapped_column(Float, default=0.5, server_default="0.5")
    source_conversation_id: Mapped[uuid.UUID | None] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("conversations.id", ondelete="SET NULL")
    )
    last_used_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    use_count: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    superseded_by: Mapped[uuid.UUID | None] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("long_term_memories.id", ondelete="SET NULL")
    )

    __table_args__ = (
        Index("ix_ltm_user_active", "user_id", "superseded_by"),
    )


class ToolInvocation(Base, UUIDMixin, TimestampMixin):
    """Audit trail for tool calls — one row per invocation, for cost + debugging."""

    __tablename__ = "tool_invocations"

    message_id: Mapped[uuid.UUID | None] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("messages.id", ondelete="CASCADE")
    )
    conversation_id: Mapped[uuid.UUID | None] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("conversations.id", ondelete="CASCADE")
    )
    tool_name: Mapped[str] = mapped_column(String(100), nullable=False)
    arguments_json: Mapped[dict] = mapped_column(JSONB, default=dict, server_default="{}")
    result_json: Mapped[dict] = mapped_column(JSONB, default=dict, server_default="{}")
    ok: Mapped[bool] = mapped_column(Boolean, default=True)
    error: Mapped[str | None] = mapped_column(Text)
    duration_ms: Mapped[int] = mapped_column(Integer, default=0)

    __table_args__ = (Index("ix_tool_inv_conv", "conversation_id", "created_at"),)
