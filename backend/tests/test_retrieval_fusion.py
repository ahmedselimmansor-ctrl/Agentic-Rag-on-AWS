"""Reciprocal Rank Fusion — the piece that makes hybrid search worth having."""

from __future__ import annotations

import uuid

from app.config import settings
from app.services.retrieval import _reciprocal_rank_fusion


def row(chunk_id: uuid.UUID, content: str = "text"):
    return {
        "id": chunk_id,
        "document_id": uuid.uuid4(),
        "filename": "doc.pdf",
        "content": content,
        "context_header": None,
        "ordinal": 0,
        "page_from": None,
        "page_to": None,
        "modality": "text",
        "metadata_json": {},
    }


def test_document_in_both_legs_outranks_either_alone():
    shared, dense_only, sparse_only = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()

    # `shared` is 2nd in both lists; the others are 1st in exactly one.
    fused = _reciprocal_rank_fusion(
        dense_rows=[row(dense_only), row(shared)],
        sparse_rows=[row(sparse_only), row(shared)],
    )

    assert fused[0].chunk_id == shared, "agreement across both indexes should win"
    assert fused[0].dense_rank == 2
    assert fused[0].sparse_rank == 2


def test_scores_follow_the_rrf_formula():
    a = uuid.uuid4()
    fused = _reciprocal_rank_fusion(dense_rows=[row(a)], sparse_rows=[row(a)])

    expected = 2 * (1.0 / (settings.rrf_k + 1))
    assert abs(fused[0].fusion_score - expected) < 1e-9


def test_single_leg_results_still_surface():
    a, b = uuid.uuid4(), uuid.uuid4()
    fused = _reciprocal_rank_fusion(dense_rows=[row(a)], sparse_rows=[row(b)])

    assert {c.chunk_id for c in fused} == {a, b}
    assert fused[0].fusion_score == fused[1].fusion_score


def test_output_is_sorted_descending():
    rows = [row(uuid.uuid4()) for _ in range(5)]
    fused = _reciprocal_rank_fusion(dense_rows=rows, sparse_rows=[])

    scores = [c.fusion_score for c in fused]
    assert scores == sorted(scores, reverse=True)


def test_empty_input_is_empty_output():
    assert _reciprocal_rank_fusion([], []) == []


def test_citation_label_reflects_page_span():
    from app.services.retrieval import RetrievedChunk

    single = RetrievedChunk(
        chunk_id=uuid.uuid4(), document_id=uuid.uuid4(), filename="spec.pdf",
        content="c", context_header=None, ordinal=0, page_from=4, page_to=4, modality="text",
    )
    span = RetrievedChunk(
        chunk_id=uuid.uuid4(), document_id=uuid.uuid4(), filename="spec.pdf",
        content="c", context_header=None, ordinal=0, page_from=4, page_to=6, modality="text",
    )
    none = RetrievedChunk(
        chunk_id=uuid.uuid4(), document_id=uuid.uuid4(), filename="notes.md",
        content="c", context_header=None, ordinal=0, page_from=None, page_to=None, modality="text",
    )

    assert single.citation_label == "spec.pdf (p.4)"
    assert span.citation_label == "spec.pdf (pp.4-6)"
    assert none.citation_label == "notes.md"
