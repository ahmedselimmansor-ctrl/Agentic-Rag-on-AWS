"""Hybrid retrieval: dense (pgvector) + sparse (Postgres FTS) -> RRF -> rerank.

Why both indexes: dense search finds paraphrases but misses rare literals — part
numbers, error codes, surnames. Sparse search nails those literals but misses
synonyms. Reciprocal Rank Fusion combines the two ranked lists without needing
their scores to be on a comparable scale, then the cross-encoder reranker does
the precision pass over the fused candidates.
"""

from __future__ import annotations

import logging
import uuid
from dataclasses import dataclass, field

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.services.embeddings import embed_query
from app.services.reranker import rerank

logger = logging.getLogger(__name__)


@dataclass(slots=True)
class RetrievedChunk:
    chunk_id: uuid.UUID
    document_id: uuid.UUID
    filename: str
    content: str
    context_header: str | None
    ordinal: int
    page_from: int | None
    page_to: int | None
    modality: str
    dense_rank: int | None = None
    sparse_rank: int | None = None
    fusion_score: float = 0.0
    rerank_score: float | None = None
    metadata: dict = field(default_factory=dict)

    @property
    def citation_label(self) -> str:
        if self.page_from:
            span = (
                f"p.{self.page_from}"
                if not self.page_to or self.page_to == self.page_from
                else f"pp.{self.page_from}-{self.page_to}"
            )
            return f"{self.filename} ({span})"
        return self.filename

    @property
    def prompt_text(self) -> str:
        header = f"{self.context_header}\n" if self.context_header else ""
        return f"{header}{self.content}".strip()

    def to_source(self, index: int) -> dict:
        return {
            "index": index,
            "chunk_id": str(self.chunk_id),
            "document_id": str(self.document_id),
            "filename": self.filename,
            "label": self.citation_label,
            "snippet": self.content[:400],
            "page_from": self.page_from,
            "page_to": self.page_to,
            "modality": self.modality,
            "score": round(self.rerank_score, 4) if self.rerank_score is not None else None,
            "fusion_score": round(self.fusion_score, 5),
        }


def _vector_literal(vector: list[float]) -> str:
    """pgvector accepts a bracketed text literal; safer than driver array binding."""
    return "[" + ",".join(f"{v:.7g}" for v in vector) + "]"


_SCOPE_SQL = """
  AND c.user_id = :user_id
  AND (
        :conversation_id::uuid IS NULL
        OR c.conversation_id IS NULL
        OR c.conversation_id = :conversation_id::uuid
      )
"""

DENSE_SQL = f"""
SELECT c.id, c.document_id, d.filename, c.content, c.context_header, c.ordinal,
       c.page_from, c.page_to, c.modality, c.metadata_json,
       (c.embedding <=> :qvec::vector) AS distance
FROM chunks c
JOIN documents d ON d.id = c.document_id
WHERE c.embedding IS NOT NULL
  AND d.status = 'ready'
  {_SCOPE_SQL}
ORDER BY c.embedding <=> :qvec::vector
LIMIT :k
"""

SPARSE_SQL = f"""
WITH q AS (SELECT websearch_to_tsquery('english', :query) AS tsq)
SELECT c.id, c.document_id, d.filename, c.content, c.context_header, c.ordinal,
       c.page_from, c.page_to, c.modality, c.metadata_json,
       ts_rank_cd(c.tsv, q.tsq) AS rank
FROM chunks c
JOIN documents d ON d.id = c.document_id
CROSS JOIN q
WHERE c.tsv @@ q.tsq
  AND d.status = 'ready'
  {_SCOPE_SQL}
ORDER BY rank DESC
LIMIT :k
"""


async def hybrid_search(
    session: AsyncSession,
    query: str,
    *,
    user_id: uuid.UUID,
    conversation_id: uuid.UUID | None = None,
    dense_k: int | None = None,
    sparse_k: int | None = None,
) -> list[RetrievedChunk]:
    """Run both indexes and fuse. Either leg failing degrades rather than errors."""
    dense_k = dense_k or settings.dense_top_k
    sparse_k = sparse_k or settings.sparse_top_k

    params_common = {
        "user_id": str(user_id),
        "conversation_id": str(conversation_id) if conversation_id else None,
    }

    dense_rows: list = []
    sparse_rows: list = []

    try:
        qvec = await embed_query(query)
        dense_rows = (
            await session.execute(
                text(DENSE_SQL),
                {**params_common, "qvec": _vector_literal(qvec), "k": dense_k},
            )
        ).mappings().all()
    except Exception as exc:  # noqa: BLE001
        logger.warning("dense retrieval failed: %s", exc)

    if settings.hybrid_enabled and query.strip():
        try:
            sparse_rows = (
                await session.execute(
                    text(SPARSE_SQL), {**params_common, "query": query, "k": sparse_k}
                )
            ).mappings().all()
        except Exception as exc:  # noqa: BLE001 - malformed tsquery must not kill the turn
            logger.warning("sparse retrieval failed: %s", exc)

    return _reciprocal_rank_fusion(dense_rows, sparse_rows)


def _reciprocal_rank_fusion(dense_rows: list, sparse_rows: list) -> list[RetrievedChunk]:
    k = settings.rrf_k
    merged: dict[uuid.UUID, RetrievedChunk] = {}

    def upsert(row, rank: int, leg: str) -> None:
        cid = row["id"]
        item = merged.get(cid)
        if item is None:
            item = RetrievedChunk(
                chunk_id=cid,
                document_id=row["document_id"],
                filename=row["filename"],
                content=row["content"],
                context_header=row["context_header"],
                ordinal=row["ordinal"],
                page_from=row["page_from"],
                page_to=row["page_to"],
                modality=row["modality"],
                metadata=row["metadata_json"] or {},
            )
            merged[cid] = item
        if leg == "dense":
            item.dense_rank = rank
        else:
            item.sparse_rank = rank
        item.fusion_score += 1.0 / (k + rank)

    for rank, row in enumerate(dense_rows, start=1):
        upsert(row, rank, "dense")
    for rank, row in enumerate(sparse_rows, start=1):
        upsert(row, rank, "sparse")

    out = list(merged.values())
    out.sort(key=lambda c: c.fusion_score, reverse=True)
    return out


async def retrieve(
    session: AsyncSession,
    query: str,
    *,
    user_id: uuid.UUID,
    conversation_id: uuid.UUID | None = None,
    top_n: int | None = None,
) -> list[RetrievedChunk]:
    """Full pipeline: hybrid search, cross-encoder rerank, relevance cutoff."""
    candidates = await hybrid_search(
        session, query, user_id=user_id, conversation_id=conversation_id
    )
    if not candidates:
        return []

    n = top_n or settings.rerank_top_n
    results = await rerank(query, [c.prompt_text for c in candidates], top_n=n)

    ranked: list[RetrievedChunk] = []
    for r in results:
        chunk = candidates[r.index]
        chunk.rerank_score = r.score
        ranked.append(chunk)

    # Drop weak matches — padding the prompt with near-irrelevant passages is how
    # RAG systems start hallucinating. If the reranker was unavailable all scores
    # are 0.0, so keep fusion order rather than discarding everything.
    if any(c.rerank_score for c in ranked):
        filtered = [c for c in ranked if (c.rerank_score or 0) >= settings.min_rerank_score]
        if filtered:
            return filtered
    return ranked[:n]


async def fetch_neighbors(
    session: AsyncSession, chunk: RetrievedChunk, window: int = 1
) -> list[str]:
    """Adjacent chunks from the same document — restores context the chunk
    boundary cut off, without re-running retrieval."""
    rows = (
        await session.execute(
            text(
                """
                SELECT content FROM chunks
                WHERE document_id = :doc_id
                  AND ordinal BETWEEN :lo AND :hi
                  AND id <> :self_id
                ORDER BY ordinal
                """
            ),
            {
                "doc_id": str(chunk.document_id),
                "lo": max(0, chunk.ordinal - window),
                "hi": chunk.ordinal + window,
                "self_id": str(chunk.chunk_id),
            },
        )
    ).scalars().all()
    return list(rows)
