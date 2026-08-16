"""Cross-encoder reranking via Alibaba Cloud Model Studio (`qwen3-rerank`).

The hybrid retriever is tuned for recall — it returns ~60 candidates. The reranker
is what turns that into precision: it scores each (query, passage) pair jointly
instead of comparing two independently-produced vectors.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

from app.config import settings
from app.services.http import UpstreamError, post_json

logger = logging.getLogger(__name__)

RERANK_PATH = "/api/v1/services/rerank/text-rerank/text-rerank"

# The provider caps documents per request; longer candidate lists are truncated.
MAX_DOCUMENTS = 100
# Passages longer than this are truncated before scoring to stay within the
# reranker's own context limit.
MAX_DOC_CHARS = 4000


@dataclass(slots=True)
class RerankResult:
    index: int
    score: float


def _headers() -> dict[str, str]:
    if not settings.dashscope_api_key:
        raise RuntimeError("DASHSCOPE_API_KEY is not set")
    return {
        "Authorization": f"Bearer {settings.dashscope_api_key}",
        "Content-Type": "application/json",
    }


async def rerank(
    query: str, documents: list[str], top_n: int | None = None
) -> list[RerankResult]:
    """Score passages against the query. Returns indices into `documents`,
    highest score first. Falls back to identity order if the service is down —
    a degraded answer beats no answer."""
    if not documents:
        return []

    docs = [d[:MAX_DOC_CHARS] for d in documents[:MAX_DOCUMENTS]]
    n = min(top_n or settings.rerank_top_n, len(docs))

    url = settings.dashscope_base_url.rstrip("/") + RERANK_PATH
    payload = {
        "model": settings.rerank_model,
        "input": {"query": query, "documents": docs},
        "parameters": {"top_n": n, "return_documents": False},
    }

    try:
        data = await post_json(url, service="dashscope-rerank", headers=_headers(), json_body=payload)
    except (UpstreamError, RuntimeError) as exc:
        logger.warning("rerank unavailable, falling back to fusion order: %s", exc)
        return [RerankResult(index=i, score=0.0) for i in range(n)]

    results = (data.get("output") or {}).get("results")
    if not isinstance(results, list):
        logger.warning("unexpected rerank response shape: %s", data)
        return [RerankResult(index=i, score=0.0) for i in range(n)]

    out = [
        RerankResult(index=int(r["index"]), score=float(r.get("relevance_score", 0.0)))
        for r in results
        if isinstance(r.get("index"), int) and int(r["index"]) < len(docs)
    ]
    out.sort(key=lambda r: r.score, reverse=True)
    return out[:n]
