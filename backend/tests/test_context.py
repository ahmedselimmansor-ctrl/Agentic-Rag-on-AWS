"""Context-window assembly: priority order and hard budget enforcement."""

from __future__ import annotations

import uuid
from types import SimpleNamespace

from app.config import settings
from app.db.models import Role
from app.services.context import build_messages
from app.services.retrieval import RetrievedChunk
from app.services.tokens import count_message_tokens


def chunk(text: str, index: int = 0) -> RetrievedChunk:
    return RetrievedChunk(
        chunk_id=uuid.uuid4(),
        document_id=uuid.uuid4(),
        filename=f"doc{index}.pdf",
        content=text,
        context_header=None,
        ordinal=index,
        page_from=index + 1,
        page_to=index + 1,
        modality="text",
        rerank_score=0.9 - index * 0.01,
    )


def message(role: Role, content: str):
    return SimpleNamespace(role=role, content=content)


def test_system_prompt_and_question_always_present():
    result = build_messages(question="What is the refund window?", chunks=[], history=[])

    assert result.messages[0]["role"] == "system"
    assert result.messages[-1]["role"] == "user"
    assert "What is the refund window?" in result.messages[-1]["content"]


def test_no_context_is_stated_rather_than_hidden():
    result = build_messages(question="anything", chunks=[], history=[])

    assert "No passages were retrieved" in result.messages[-1]["content"]
    assert result.sources == []


def test_passages_are_numbered_for_citation():
    chunks = [chunk("Refunds are processed within 30 days.", i) for i in range(3)]
    result = build_messages(question="refund window?", chunks=chunks, history=[])

    body = result.messages[-1]["content"]
    assert "[1]" in body and "[2]" in body and "[3]" in body
    assert [s["index"] for s in result.sources] == [1, 2, 3]
    assert len(result.used_chunk_ids) == 3


def test_passage_budget_drops_the_weakest_passages():
    # Each chunk is far too large to all fit inside max_retrieved_context_tokens.
    fat = "word " * 4000
    chunks = [chunk(fat, i) for i in range(20)]
    result = build_messages(question="q", chunks=chunks, history=[])

    assert result.dropped_passages > 0
    assert len(result.sources) < len(chunks)
    # What survives is the top of the reranked list, in order.
    assert [s["index"] for s in result.sources] == list(range(1, len(result.sources) + 1))


def test_history_is_trimmed_from_the_oldest_end():
    history = [
        message(Role.user if i % 2 == 0 else Role.assistant, f"turn {i} " + "filler " * 500)
        for i in range(30)
    ]
    result = build_messages(question="latest", chunks=[], history=history)

    kept = [m for m in result.messages if m["role"] in ("user", "assistant")][:-1]
    assert result.dropped_history > 0
    assert kept, "some recent history should survive"
    # The most recent turn that fits must be present; the oldest must not.
    assert "turn 29" in kept[-1]["content"] or "turn 28" in kept[-1]["content"]
    assert not any("turn 0 " in m["content"] for m in kept)


def test_history_never_opens_with_an_assistant_turn():
    history = [
        message(Role.assistant, "dangling reply"),
        message(Role.user, "a question"),
        message(Role.assistant, "an answer"),
    ]
    result = build_messages(question="next", chunks=[], history=history)

    conversation = [m for m in result.messages if m["role"] != "system"]
    assert conversation[0]["role"] == "user"


def test_total_stays_within_the_model_budget():
    fat = "word " * 5000
    result = build_messages(
        question="q",
        chunks=[chunk(fat, i) for i in range(40)],
        history=[message(Role.user, fat) for _ in range(40)],
        summary=fat,
        memory_block="- (fact) the user prefers concise answers",
    )

    assert count_message_tokens(result.messages) <= settings.input_token_budget


def test_memory_block_lands_in_the_system_prompt():
    result = build_messages(
        question="q",
        chunks=[],
        history=[],
        memory_block="- (preference) prefers Python examples",
    )

    assert "prefers Python examples" in result.messages[0]["content"]


def test_image_attachment_produces_multimodal_content():
    result = build_messages(
        question="what is in this picture?",
        chunks=[],
        history=[],
        attachments=[{"url": "https://example.com/a.png", "mime_type": "image/png"}],
    )

    content = result.messages[-1]["content"]
    assert isinstance(content, list)
    assert any(p.get("type") == "image_url" for p in content)
    assert any(p.get("type") == "text" for p in content)


def test_web_results_are_cited_after_document_passages():
    result = build_messages(
        question="q",
        chunks=[chunk("local passage", 0)],
        history=[],
        web_results=[{"title": "A page", "url": "https://ex.com", "snippet": "web text"}],
    )

    kinds = [s.get("kind") for s in result.sources]
    assert kinds == [None, "web"]
    assert result.sources[1]["index"] == 2
