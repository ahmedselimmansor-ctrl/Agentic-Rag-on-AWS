"""Structure-aware chunking.

Two rules drive the design:

1. A chunk never silently spans a heading change — the heading trail is carried
   into `context_header` so the embedded text knows where it came from. A bare
   passage reading "It must be renewed annually" is useless; prefixed with
   "Billing > Enterprise plans" it is retrievable.
2. Oversized blocks split on sentence boundaries, not mid-word, and consecutive
   chunks overlap so a fact straddling a boundary survives in at least one chunk.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from app.config import settings
from app.services.parsing import ParsedBlock, ParsedDocument
from app.services.tokens import count_tokens

_SENTENCE_END = re.compile(r"(?<=[.!?。！？])\s+|\n{2,}")


@dataclass(slots=True)
class ChunkDraft:
    content: str
    ordinal: int
    token_count: int
    context_header: str | None = None
    page_from: int | None = None
    page_to: int | None = None
    modality: str = "text"
    image_uri: str | None = None
    metadata: dict = field(default_factory=dict)

    @property
    def embed_text(self) -> str:
        """What actually gets embedded — header included."""
        if self.context_header:
            return f"{self.context_header}\n\n{self.content}"
        return self.content


def chunk_document(
    parsed: ParsedDocument,
    *,
    target_tokens: int | None = None,
    overlap_tokens: int | None = None,
    min_tokens: int | None = None,
) -> list[ChunkDraft]:
    target = target_tokens or settings.chunk_target_tokens
    overlap = overlap_tokens or settings.chunk_overlap_tokens
    minimum = min_tokens or settings.chunk_min_tokens

    drafts: list[ChunkDraft] = []
    ordinal = 0

    for group_key, blocks in _group_by_heading(parsed.blocks):
        for block in blocks:
            if block.kind == "image":
                drafts.append(
                    ChunkDraft(
                        content=block.text,
                        ordinal=ordinal,
                        token_count=count_tokens(block.text),
                        context_header=group_key or None,
                        page_from=block.page,
                        page_to=block.page,
                        modality="image",
                        image_uri=block.image_uri,
                    )
                )
                ordinal += 1

        text_blocks = [b for b in blocks if b.kind != "image"]
        if not text_blocks:
            continue

        for piece in _pack(text_blocks, target, overlap):
            if piece.token_count < minimum and drafts and drafts[-1].context_header == (group_key or None):
                # Too small to stand alone — fold it into the previous chunk.
                prev = drafts[-1]
                prev.content = f"{prev.content}\n\n{piece.content}"
                prev.token_count = count_tokens(prev.content)
                prev.page_to = piece.page_to or prev.page_to
                continue
            piece.ordinal = ordinal
            piece.context_header = group_key or None
            drafts.append(piece)
            ordinal += 1

    return drafts


def _group_by_heading(
    blocks: list[ParsedBlock],
) -> list[tuple[str, list[ParsedBlock]]]:
    """Consecutive blocks sharing a heading trail form one group."""
    groups: list[tuple[str, list[ParsedBlock]]] = []
    current_key: str | None = None
    current: list[ParsedBlock] = []

    for block in blocks:
        key = " > ".join(block.heading_path)
        if current_key is None:
            current_key = key
        if key != current_key:
            if current:
                groups.append((current_key, current))
            current_key, current = key, []
        # A heading block's own text is redundant once it is in the header.
        if block.kind == "heading":
            continue
        current.append(block)

    if current:
        groups.append((current_key or "", current))
    elif current_key is not None and not groups:
        groups.append((current_key, []))
    return groups


def _pack(blocks: list[ParsedBlock], target: int, overlap: int) -> list[ChunkDraft]:
    """Greedily fill chunks to `target` tokens, splitting blocks that overflow."""
    units: list[tuple[str, int | None]] = []
    for block in blocks:
        if count_tokens(block.text) <= target:
            units.append((block.text, block.page))
        else:
            for sentence in _split_oversized(block.text, target):
                units.append((sentence, block.page))

    drafts: list[ChunkDraft] = []
    buf: list[str] = []
    buf_tokens = 0
    page_from: int | None = None
    page_to: int | None = None

    def flush() -> None:
        nonlocal buf, buf_tokens, page_from, page_to
        if not buf:
            return
        content = "\n\n".join(buf).strip()
        drafts.append(
            ChunkDraft(
                content=content,
                ordinal=0,  # assigned by the caller
                token_count=count_tokens(content),
                page_from=page_from,
                page_to=page_to,
            )
        )
        # Seed the next buffer with the tail of this one so facts spanning a
        # boundary appear in both chunks.
        carry = _tail_tokens(content, overlap)
        buf = [carry] if carry else []
        buf_tokens = count_tokens(carry) if carry else 0
        page_from = page_to

    for text, page in units:
        t = count_tokens(text)
        if buf and buf_tokens + t > target:
            flush()
        if page is not None:
            page_from = page if page_from is None else page_from
            page_to = page
        buf.append(text)
        buf_tokens += t

    # Final flush without seeding an overlap tail.
    if buf:
        content = "\n\n".join(buf).strip()
        if content:
            drafts.append(
                ChunkDraft(
                    content=content,
                    ordinal=0,
                    token_count=count_tokens(content),
                    page_from=page_from,
                    page_to=page_to,
                )
            )
    return drafts


def _split_oversized(text: str, target: int) -> list[str]:
    sentences = [s.strip() for s in _SENTENCE_END.split(text) if s and s.strip()]
    out: list[str] = []
    buf: list[str] = []
    buf_tokens = 0

    for sentence in sentences:
        t = count_tokens(sentence)
        if t > target:
            # A single sentence longer than the target (minified code, a giant
            # table row) — hard-split on whitespace.
            if buf:
                out.append(" ".join(buf))
                buf, buf_tokens = [], 0
            out.extend(_hard_split(sentence, target))
            continue
        if buf_tokens + t > target and buf:
            out.append(" ".join(buf))
            buf, buf_tokens = [], 0
        buf.append(sentence)
        buf_tokens += t

    if buf:
        out.append(" ".join(buf))
    return out


def _hard_split(text: str, target: int) -> list[str]:
    words = text.split(" ")
    out: list[str] = []
    buf: list[str] = []
    buf_tokens = 0
    for word in words:
        t = count_tokens(word) or 1
        if buf_tokens + t > target and buf:
            out.append(" ".join(buf))
            buf, buf_tokens = [], 0
        buf.append(word)
        buf_tokens += t
    if buf:
        out.append(" ".join(buf))
    return out


def _tail_tokens(text: str, overlap: int) -> str:
    """Take the last ~`overlap` tokens, snapped to a sentence start."""
    if overlap <= 0:
        return ""
    sentences = [s for s in _SENTENCE_END.split(text) if s and s.strip()]
    tail: list[str] = []
    total = 0
    for sentence in reversed(sentences):
        t = count_tokens(sentence)
        if total + t > overlap and tail:
            break
        tail.insert(0, sentence.strip())
        total += t
    return " ".join(tail)
