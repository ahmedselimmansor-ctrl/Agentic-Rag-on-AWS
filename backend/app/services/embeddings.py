"""Embeddings via Alibaba Cloud Model Studio (DashScope).

`tongyi-embedding-vision-flash` is a multimodal embedding model: text and images
land in the same vector space, so an image chunk and a text query are directly
comparable. That is why `Chunk.modality` exists — image chunks embed the picture
itself rather than a caption of it.
"""

from __future__ import annotations

import asyncio
import base64
import logging
import mimetypes
from dataclasses import dataclass
from pathlib import Path

from app.config import settings
from app.services.http import UpstreamError, post_json
from app.services.resilience import get_breaker

logger = logging.getLogger(__name__)

MULTIMODAL_PATH = "/api/v1/services/embeddings/multimodal-embedding/multimodal-embedding"


@dataclass(slots=True)
class EmbedInput:
    """One item to embed: text, an image, or both fused into a single vector."""

    text: str | None = None
    image_url: str | None = None      # https:// or data: URI
    image_path: str | None = None     # local file, inlined as a data URI

    def to_content(self) -> list[dict[str, str]]:
        content: list[dict[str, str]] = []
        if self.image_url:
            content.append({"image": self.image_url})
        elif self.image_path:
            content.append({"image": _file_to_data_uri(self.image_path)})
        if self.text:
            content.append({"text": self.text})
        if not content:
            raise ValueError("EmbedInput requires text or an image")
        return content


def _file_to_data_uri(path: str) -> str:
    mime = mimetypes.guess_type(path)[0] or "image/png"
    raw = Path(path).read_bytes()
    return f"data:{mime};base64,{base64.b64encode(raw).decode()}"


def _headers() -> dict[str, str]:
    if not settings.dashscope_api_key:
        raise RuntimeError("DASHSCOPE_API_KEY is not set")
    return {
        "Authorization": f"Bearer {settings.dashscope_api_key}",
        "Content-Type": "application/json",
    }


async def embed(inputs: list[EmbedInput]) -> list[list[float]]:
    """Embed a batch, preserving input order. Batches are chunked to the
    provider's per-request cap and issued concurrently."""
    if not inputs:
        return []

    batches = [
        inputs[i : i + settings.embedding_batch_size]
        for i in range(0, len(inputs), settings.embedding_batch_size)
    ]
    results = await asyncio.gather(*(_embed_batch(b) for b in batches))
    flat: list[list[float]] = []
    for r in results:
        flat.extend(r)

    if len(flat) != len(inputs):
        raise UpstreamError(
            "dashscope-embedding", None,
            f"expected {len(inputs)} vectors, got {len(flat)}",
        )
    return flat


async def _embed_batch(batch: list[EmbedInput]) -> list[list[float]]:
    """Guarded by a breaker: embedding failures are the one thing that stops
    both ingestion and retrieval, so hammering a dead provider is expensive."""
    return await get_breaker("embeddings").call(_embed_batch_uncached, batch)


async def _embed_batch_uncached(batch: list[EmbedInput]) -> list[list[float]]:
    url = settings.dashscope_base_url.rstrip("/") + MULTIMODAL_PATH
    payload = {
        "model": settings.embedding_model,
        "input": {"contents": [item.to_content() for item in batch]},
    }
    data = await post_json(url, service="dashscope-embedding", headers=_headers(), json_body=payload)

    embeddings = (data.get("output") or {}).get("embeddings")
    if not isinstance(embeddings, list):
        raise UpstreamError("dashscope-embedding", None, f"unexpected response: {data}")

    # The API may return results out of order; `index` is authoritative.
    ordered = sorted(embeddings, key=lambda e: e.get("index", 0))
    vectors = [e["embedding"] for e in ordered]

    for v in vectors:
        if len(v) != settings.embedding_dim:
            raise UpstreamError(
                "dashscope-embedding", None,
                f"dimension mismatch: model returned {len(v)}, "
                f"EMBEDDING_DIM is {settings.embedding_dim}. "
                "Update EMBEDDING_DIM and re-run migrations.",
            )
    return vectors


async def embed_texts(texts: list[str]) -> list[list[float]]:
    return await embed([EmbedInput(text=t) for t in texts])


async def embed_query(query: str) -> list[float]:
    vectors = await embed_texts([query])
    return vectors[0]
