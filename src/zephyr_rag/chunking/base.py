"""Chunker protocol and shared helpers.

A chunk should be the smallest unit that still stands alone as an answer. That
single sentence drives every decision in this package, and it pulls in two
directions: smaller chunks give sharper embeddings but sever context, larger
chunks preserve context but dilute the retrieval signal.

The three concrete chunkers escape that trade-off differently, because the three
sources have genuinely different natural answer units:

========  ==========================  ======================================
source    natural answer unit         strategy
========  ==========================  ======================================
docs      a heading section           walk the authored heading tree
forum     one question-answer pair    pair the question with each answer
blog      a narrative passage         overlapping sentence-bounded windows
========  ==========================  ======================================

All of them repair severed context the same way: a breadcrumb header is
prepended to the indexed text (see :mod:`..chunking.contextual`), so an orphaned
"set this to false" chunk still carries what "this" refers to.
"""

from __future__ import annotations

import re
from typing import Iterable, Protocol

from ..ingest.loaders import RawDocument
from ..tokenize import estimate_tokens, slugify
from ..types import Chunk

__all__ = ["Chunker", "split_on_paragraphs", "dedupe_slug", "CODE_FENCE_RE", "iter_fenced_blocks"]


CODE_FENCE_RE = re.compile(r"^\s*(?:```|~~~)")


class Chunker(Protocol):
    """Every chunker takes one document and returns zero or more chunks."""

    source: str

    def chunk(self, doc: RawDocument) -> list[Chunk]:  # pragma: no cover - protocol
        ...


def iter_fenced_blocks(lines: Iterable[str]) -> list[bool]:
    """Return a per-line mask marking lines inside fenced code blocks.

    Needed because headings, paragraph breaks and sentence boundaries inside a
    code block are not real boundaries. Splitting a code fence produces a sample
    that *looks* valid and is not, which is strictly worse than an oversized
    chunk -- so the docs chunker consults this mask before cutting anywhere.
    """
    mask: list[bool] = []
    inside = False
    for line in lines:
        is_fence = bool(CODE_FENCE_RE.match(line))
        if is_fence:
            # The fence line itself belongs to the block at both open and close.
            mask.append(True)
            inside = not inside
        else:
            mask.append(inside)
    return mask


def split_on_paragraphs(text: str, max_tokens: int, overlap_sentences: int = 1) -> list[str]:
    """Split oversized text at paragraph boundaries, never inside a code fence.

    A section that exceeds the budget is divided at blank lines. If a single
    paragraph (typically one large code block) still exceeds the budget it is
    emitted whole and over budget: a truncated code sample is a correctness bug,
    an oversized chunk is only a cost inefficiency.

    Args:
        text: Section body.
        max_tokens: Soft budget per piece.
        overlap_sentences: Trailing sentences from the previous piece repeated at
            the start of the next, so a thought spanning a cut survives intact.
    """
    if estimate_tokens(text) <= max_tokens:
        return [text]

    lines = text.splitlines()
    mask = iter_fenced_blocks(lines)

    # Group into paragraphs, treating fenced blocks as atomic.
    paragraphs: list[str] = []
    buf: list[str] = []
    for line, in_code in zip(lines, mask):
        if not line.strip() and not in_code:
            if buf:
                paragraphs.append("\n".join(buf))
                buf = []
        else:
            buf.append(line)
    if buf:
        paragraphs.append("\n".join(buf))

    pieces: list[str] = []
    current: list[str] = []
    current_tokens = 0
    for para in paragraphs:
        para_tokens = estimate_tokens(para)
        if current and current_tokens + para_tokens > max_tokens:
            pieces.append("\n\n".join(current))
            tail = _trailing_sentences("\n\n".join(current), overlap_sentences)
            current = [tail] if tail else []
            current_tokens = estimate_tokens(tail) if tail else 0
        current.append(para)
        current_tokens += para_tokens
    if current:
        pieces.append("\n\n".join(current))

    return [p for p in pieces if p.strip()]


def _trailing_sentences(text: str, count: int) -> str:
    """Last ``count`` sentences of ``text``, used as inter-piece overlap.

    Returns empty when the tail would be a code fence, since repeating half a
    code block at the head of the next chunk is noise rather than context.
    """
    if count <= 0:
        return ""
    from ..tokenize import sentences

    tail = sentences(text)[-count:]
    joined = " ".join(tail).strip()
    if "```" in joined or len(joined) > 400:
        return ""
    return joined


def dedupe_slug(slug: str, seen: set[str]) -> str:
    """Make ``slug`` unique within a document.

    Repeated headings ("Overview" under two parents) would otherwise collide into
    one chunk id, and since the gold set addresses chunks by id a collision
    silently merges two labels.
    """
    base = slug or "section"
    candidate = base
    n = 2
    while candidate in seen:
        candidate = f"{base}-{n}"
        n += 1
    seen.add(candidate)
    return candidate


def make_chunk(
    *,
    chunk_id: str,
    source: str,
    body: str,
    context_header: str,
    metadata: dict,
    parent_id: str | None = None,
) -> Chunk:
    """Assemble a :class:`Chunk`, computing the indexed text and token count.

    ``text`` (header + body) is what gets embedded and indexed; ``display_text``
    (body alone) is what gets cited. Keeping them separate stops the generator
    from quoting breadcrumbs back at the user as if they were prose.
    """
    body = body.strip()
    indexed = f"{context_header}\n\n{body}" if context_header else body
    return Chunk(
        chunk_id=chunk_id,
        source=source,  # type: ignore[arg-type]
        text=indexed,
        display_text=body,
        context_header=context_header,
        metadata=metadata,
        token_count=estimate_tokens(indexed),
        parent_id=parent_id,
    )


def slug_for(title: str, fallback: str = "section") -> str:
    return slugify(title) or fallback
