"""Request/response models for chat, conversations and documents."""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field


class Attachment(BaseModel):
    document_id: uuid.UUID | None = None
    filename: str
    mime_type: str
    # Presigned/data URL — only populated for images passed to the vision model.
    url: str | None = None
    size_bytes: int = 0


class ChatRequest(BaseModel):
    message: str = Field(min_length=1, max_length=32_000)
    conversation_id: uuid.UUID | None = None
    # Maps to the (+) menu and the web-search toggle in the prompt box.
    attachments: list[Attachment] = Field(default_factory=list)
    web_search: bool = False


class MessageOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    ordinal: int
    role: Literal["user", "assistant", "system", "tool"]
    content: str
    sources: list[dict[str, Any]] = Field(default_factory=list, alias="sources_json")
    tool_calls: list[dict[str, Any]] = Field(default_factory=list, alias="tool_calls_json")
    attachments: list[dict[str, Any]] = Field(default_factory=list, alias="attachments_json")
    error: str | None = None
    created_at: datetime


class ConversationOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    title: str
    archived: bool
    created_at: datetime
    updated_at: datetime
    last_message_at: datetime | None = None


class ConversationDetail(ConversationOut):
    messages: list[MessageOut] = Field(default_factory=list)
    summary: str | None = None


class ConversationCreate(BaseModel):
    title: str = "New chat"


class ConversationUpdate(BaseModel):
    title: str | None = Field(default=None, max_length=300)
    archived: bool | None = None


class DocumentOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    filename: str
    mime_type: str
    size_bytes: int
    status: Literal["pending", "parsing", "chunking", "embedding", "ready", "failed"]
    error: str | None = None
    page_count: int
    chunk_count: int
    conversation_id: uuid.UUID | None = None
    created_at: datetime


class MemoryOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    kind: Literal["fact", "preference", "entity"]
    content: str
    salience: float
    use_count: int
    created_at: datetime
    last_used_at: datetime | None = None


class SearchRequest(BaseModel):
    query: str = Field(min_length=1, max_length=2000)
    conversation_id: uuid.UUID | None = None
    top_n: int = Field(default=8, ge=1, le=20)


class SearchHit(BaseModel):
    chunk_id: uuid.UUID
    document_id: uuid.UUID
    filename: str
    label: str
    snippet: str
    page_from: int | None = None
    page_to: int | None = None
    score: float | None = None
    fusion_score: float


class HealthOut(BaseModel):
    status: Literal["ok", "degraded"]
    version: str
    environment: str
    database: bool
    pgvector: bool
    checks: dict[str, str] = Field(default_factory=dict)
