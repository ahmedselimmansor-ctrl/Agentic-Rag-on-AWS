"""Chunking behaviour that the retrieval quality depends on."""

from __future__ import annotations

from app.services.chunking import chunk_document
from app.services.parsing import ParsedBlock, ParsedDocument, _parse_markdown


def _doc(blocks: list[ParsedBlock]) -> ParsedDocument:
    return ParsedDocument(blocks=blocks)


def test_heading_trail_becomes_context_header():
    parsed = _parse_markdown(
        "# Billing\n\n## Enterprise plans\n\nIt must be renewed annually.\n"
    )
    chunks = chunk_document(parsed, target_tokens=200, overlap_tokens=0, min_tokens=1)

    assert chunks, "expected at least one chunk"
    assert chunks[0].context_header == "Billing > Enterprise plans"
    # The header is what makes the passage self-describing once retrieved.
    assert "Billing > Enterprise plans" in chunks[0].embed_text
    assert "renewed annually" in chunks[0].embed_text


def test_chunks_respect_the_token_target():
    body = " ".join(f"sentence number {i}." for i in range(400))
    chunks = chunk_document(
        _doc([ParsedBlock(text=body)]), target_tokens=100, overlap_tokens=10, min_tokens=1
    )

    assert len(chunks) > 1
    # Allow modest slack for the overlap tail carried into each chunk.
    assert all(c.token_count <= 160 for c in chunks), [c.token_count for c in chunks]


def test_consecutive_chunks_overlap():
    sentences = " ".join(f"Fact {i} is distinct and memorable." for i in range(60))
    chunks = chunk_document(
        _doc([ParsedBlock(text=sentences)]), target_tokens=80, overlap_tokens=30, min_tokens=1
    )

    assert len(chunks) >= 2
    tail_words = set(chunks[0].content.split()[-12:])
    head_words = set(chunks[1].content.split()[:12])
    assert tail_words & head_words, "expected the overlap tail to seed the next chunk"


def test_ordinals_are_contiguous_and_sorted():
    parsed = _parse_markdown(
        "# A\n\nAlpha body text here.\n\n# B\n\nBeta body text here.\n\n# C\n\nGamma body.\n"
    )
    chunks = chunk_document(parsed, target_tokens=50, overlap_tokens=0, min_tokens=1)

    assert [c.ordinal for c in chunks] == list(range(len(chunks)))


def test_oversized_single_sentence_is_hard_split():
    monster = "x" * 40 + " " + "y" * 40
    giant = " ".join([monster] * 200)
    chunks = chunk_document(
        _doc([ParsedBlock(text=giant)]), target_tokens=60, overlap_tokens=0, min_tokens=1
    )

    assert len(chunks) > 1
    assert all(c.content.strip() for c in chunks)


def test_image_block_becomes_an_image_chunk():
    parsed = ParsedDocument(
        blocks=[ParsedBlock(text="diagram.png", kind="image", image_uri="s3://b/diagram.png")]
    )
    chunks = chunk_document(parsed, min_tokens=1)

    assert len(chunks) == 1
    assert chunks[0].modality == "image"
    assert chunks[0].image_uri == "s3://b/diagram.png"


def test_empty_document_produces_no_chunks():
    assert chunk_document(_doc([])) == []
