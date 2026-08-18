"""Upload + document management — backs the (+) button in the prompt box."""

from __future__ import annotations

import logging
import uuid

from fastapi import (
    APIRouter,
    BackgroundTasks,
    File,
    Form,
    HTTPException,
    Query,
    Response,
    UploadFile,
    status,
)
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from app.api.deps import CurrentUser, DbSession, UploadUser
from app.config import settings
from app.db.models import Conversation, Document, DocumentStatus
from app.schemas.chat import DocumentOut
from app.services import storage
from app.services.ingestion import delete_document, ingest_document

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/documents", tags=["documents"])

IMAGE_PREFIX = "image/"


@router.post("", response_model=DocumentOut, status_code=status.HTTP_202_ACCEPTED)
async def upload_document(
    session: DbSession,
    user: UploadUser,
    background: BackgroundTasks,
    file: UploadFile = File(...),
    conversation_id: uuid.UUID | None = Form(default=None),
) -> DocumentOut:
    """Store the file, register it, and kick off ingestion in the background.
    Returns immediately with status=pending; the client polls until ready."""
    data = await file.read()
    if not data:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "Empty file")
    if len(data) > settings.max_upload_bytes:
        raise HTTPException(
            status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
            f"File exceeds the {settings.max_upload_bytes // (1024 * 1024)} MB limit",
        )

    if conversation_id is not None:
        owned = (
            await session.execute(
                select(Conversation.id).where(
                    Conversation.id == conversation_id, Conversation.user_id == user.id
                )
            )
        ).scalar_one_or_none()
        if owned is None:
            raise HTTPException(status.HTTP_404_NOT_FOUND, "Conversation not found")

    stored = storage.store_bytes(
        data, file.filename or "upload", content_type=file.content_type
    )

    # Same bytes uploaded twice: reuse the existing document instead of re-embedding.
    existing = (
        await session.execute(
            select(Document).where(
                Document.user_id == user.id, Document.sha256 == stored.sha256
            )
        )
    ).scalar_one_or_none()
    if existing is not None:
        if existing.status == DocumentStatus.failed:
            existing.status = DocumentStatus.pending
            existing.error = None
            await session.commit()
            background.add_task(ingest_document, existing.id)
        return DocumentOut.model_validate(existing)

    document = Document(
        user_id=user.id,
        conversation_id=conversation_id,
        filename=file.filename or "upload",
        mime_type=stored.mime_type,
        size_bytes=stored.size_bytes,
        storage_uri=stored.uri,
        sha256=stored.sha256,
        status=DocumentStatus.pending,
    )
    session.add(document)
    try:
        await session.flush()
    except IntegrityError:
        await session.rollback()
        document = (
            await session.execute(
                select(Document).where(
                    Document.user_id == user.id, Document.sha256 == stored.sha256
                )
            )
        ).scalar_one()
        return DocumentOut.model_validate(document)

    document_id = document.id
    result = DocumentOut.model_validate(document)
    # Commit before scheduling: the background task opens its own session.
    await session.commit()
    background.add_task(ingest_document, document_id)
    return result


@router.get("", response_model=list[DocumentOut])
async def list_documents(
    session: DbSession,
    user: CurrentUser,
    conversation_id: uuid.UUID | None = Query(default=None),
    limit: int = Query(default=100, ge=1, le=500),
) -> list[Document]:
    query = select(Document).where(Document.user_id == user.id)
    if conversation_id is not None:
        query = query.where(Document.conversation_id == conversation_id)
    rows = (
        await session.execute(query.order_by(Document.created_at.desc()).limit(limit))
    ).scalars().all()
    return list(rows)


@router.get("/{document_id}", response_model=DocumentOut)
async def get_document(document_id: uuid.UUID, session: DbSession, user: CurrentUser) -> Document:
    document = (
        await session.execute(
            select(Document).where(Document.id == document_id, Document.user_id == user.id)
        )
    ).scalar_one_or_none()
    if document is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Document not found")
    return document


@router.get("/{document_id}/url")
async def get_document_url(
    document_id: uuid.UUID, session: DbSession, user: CurrentUser
) -> dict[str, str]:
    """Presigned URL — used to hand an uploaded image to the vision model and to
    preview attachments in the UI."""
    document = (
        await session.execute(
            select(Document).where(Document.id == document_id, Document.user_id == user.id)
        )
    ).scalar_one_or_none()
    if document is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Document not found")
    return {"url": storage.presigned_url(document.storage_uri), "mime_type": document.mime_type}


@router.post("/{document_id}/reingest", response_model=DocumentOut)
async def reingest(
    document_id: uuid.UUID, session: DbSession, user: CurrentUser, background: BackgroundTasks
) -> Document:
    document = (
        await session.execute(
            select(Document).where(Document.id == document_id, Document.user_id == user.id)
        )
    ).scalar_one_or_none()
    if document is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Document not found")

    document.status = DocumentStatus.pending
    document.error = None
    await session.commit()
    background.add_task(ingest_document, document_id)
    return document


@router.delete("/{document_id}", status_code=status.HTTP_204_NO_CONTENT)
async def remove_document(
    document_id: uuid.UUID, user: CurrentUser, session: DbSession
) -> Response:
    # The service opens its own session; drop ours first to avoid a lock overlap.
    await session.commit()
    deleted = await delete_document(document_id, user.id)
    if not deleted:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Document not found")
    return Response(status_code=status.HTTP_204_NO_CONTENT)
