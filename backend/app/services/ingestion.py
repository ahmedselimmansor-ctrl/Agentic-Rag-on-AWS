"""Ingestion pipeline: parse -> chunk -> embed -> persist.

Runs as a FastAPI background task. Status transitions are written to the
`documents` row so the UI can poll a file from "pending" to "ready".
"""

from __future__ import annotations

import logging
import uuid
from dataclasses import dataclass

from sqlalchemy import delete as sql_delete
from sqlalchemy import select, update

from app.config import settings
from app.db.models import Chunk, Document, DocumentStatus
from app.db.session import session_scope
from app.services import ocr, storage
from app.services.chunking import ChunkDraft, chunk_document
from app.services.embeddings import EmbedInput, embed
from app.services.parsing import ParsedBlock, UnsupportedFileType, parse

logger = logging.getLogger(__name__)


@dataclass(slots=True)
class IngestResult:
    document_id: uuid.UUID
    chunk_count: int
    page_count: int
    status: DocumentStatus
    error: str | None = None


async def ingest_document(document_id: uuid.UUID) -> IngestResult:
    """Idempotent: re-running replaces the document's existing chunks."""
    async with session_scope() as session:
        doc = await session.get(Document, document_id)
        if doc is None:
            raise ValueError(f"document {document_id} not found")
        storage_uri = doc.storage_uri
        mime_type = doc.mime_type
        filename = doc.filename
        user_id = doc.user_id
        conversation_id = doc.conversation_id

    local_path: str | None = None
    try:
        await _set_status(document_id, DocumentStatus.parsing)
        local_path = storage.download_to_temp(storage_uri)
        parsed = parse(local_path, mime_type, storage_uri=storage_uri)

        if not parsed.blocks:
            # No text layer — most likely a scan. Try OCR before giving up.
            parsed = _ocr_fallback(parsed, storage_uri, mime_type, filename)

        await _set_status(document_id, DocumentStatus.chunking)
        drafts = chunk_document(parsed)
        if not drafts:
            raise UnsupportedFileType(f"{filename} produced no chunks")

        await _set_status(document_id, DocumentStatus.embedding)
        vectors = await _embed_drafts(drafts)

        async with session_scope() as session:
            # Replace prior chunks so re-ingestion cannot leave stale passages behind.
            await session.execute(sql_delete(Chunk).where(Chunk.document_id == document_id))
            session.add_all(
                [
                    Chunk(
                        document_id=document_id,
                        user_id=user_id,
                        conversation_id=conversation_id,
                        ordinal=draft.ordinal,
                        content=draft.content,
                        context_header=draft.context_header,
                        token_count=draft.token_count,
                        page_from=draft.page_from,
                        page_to=draft.page_to,
                        modality=draft.modality,
                        image_uri=draft.image_uri,
                        embedding=vector,
                        metadata_json={"filename": filename, **draft.metadata},
                    )
                    for draft, vector in zip(drafts, vectors, strict=True)
                ]
            )
            await session.execute(
                update(Document)
                .where(Document.id == document_id)
                .values(
                    status=DocumentStatus.ready,
                    chunk_count=len(drafts),
                    page_count=parsed.page_count,
                    error=None,
                    metadata_json=parsed.metadata,
                )
            )

        logger.info("ingested %s: %d chunks", filename, len(drafts))
        return IngestResult(document_id, len(drafts), parsed.page_count, DocumentStatus.ready)

    except Exception as exc:  # noqa: BLE001 - surface any failure to the UI
        logger.exception("ingestion failed for %s", document_id)
        await _set_status(document_id, DocumentStatus.failed, error=str(exc)[:2000])
        return IngestResult(document_id, 0, 0, DocumentStatus.failed, error=str(exc))
    finally:
        if local_path:
            storage.cleanup_temp(local_path, storage_uri)


def _ocr_fallback(parsed, storage_uri: str, mime_type: str, filename: str):  # noqa: ANN001, ANN202
    """Run OCR on a PDF that yielded no text. Raises with an actionable message
    when OCR is unavailable, so the UI can explain what to do."""
    is_pdf = "pdf" in (mime_type or "").lower() or filename.lower().endswith(".pdf")

    if not is_pdf:
        raise UnsupportedFileType(f"No extractable content in {filename}.")

    if not ocr.is_available():
        raise UnsupportedFileType(
            f"{filename} has no text layer (it is most likely a scan). "
            "OCR requires S3 storage; set UPLOAD_BACKEND=s3 to enable it."
        )

    logger.info("no text layer in %s, falling back to OCR", filename)
    try:
        pages = ocr.extract_pdf(storage_uri)
    except ocr.OCRUnavailable as exc:
        raise UnsupportedFileType(f"{filename} needs OCR, which failed: {exc}") from exc

    if not pages:
        raise UnsupportedFileType(f"OCR found no text in {filename}.")

    parsed.blocks = [
        ParsedBlock(text=page.text, page=page.page) for page in pages if page.text.strip()
    ]
    parsed.page_count = len(pages)
    parsed.metadata = {**(parsed.metadata or {}), "ocr": True}
    logger.info("OCR recovered %d pages from %s", len(pages), filename)
    return parsed


async def _embed_drafts(drafts: list[ChunkDraft]) -> list[list[float]]:
    """Text chunks embed their header + content; image chunks embed the picture."""
    inputs: list[EmbedInput] = []
    for draft in drafts:
        if draft.modality == "image" and draft.image_uri:
            inputs.append(
                EmbedInput(
                    text=draft.context_header or None,
                    image_url=storage.presigned_url(draft.image_uri),
                )
            )
        else:
            inputs.append(EmbedInput(text=draft.embed_text))
    return await embed(inputs)


async def _set_status(
    document_id: uuid.UUID, status: DocumentStatus, *, error: str | None = None
) -> None:
    async with session_scope() as session:
        await session.execute(
            update(Document)
            .where(Document.id == document_id)
            .values(status=status, error=error)
        )


async def delete_document(document_id: uuid.UUID, user_id: uuid.UUID) -> bool:
    """Remove the row (chunks cascade) and the underlying blob."""
    async with session_scope() as session:
        doc = (
            await session.execute(
                select(Document).where(
                    Document.id == document_id, Document.user_id == user_id
                )
            )
        ).scalar_one_or_none()
        if doc is None:
            return False
        uri = doc.storage_uri
        await session.delete(doc)

    storage.delete(uri)
    return True


def max_upload_bytes() -> int:
    return settings.max_upload_bytes
