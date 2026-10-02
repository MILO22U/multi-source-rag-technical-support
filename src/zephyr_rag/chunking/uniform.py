"""Uniform chunker -- the ABLATION CONTROL, not a production strategy.

This is the naive approach that source-specific chunking is meant to beat: one
fixed-size splitter applied to every source, ignoring heading structure, ignoring
thread shape, ignoring code fences.

It exists so the README's ablation table can contain a control row. Without it,
"we implemented per-source chunking" is an assertion; with it, the claim becomes
a measurement. If this row scores close to the per-source configuration on this
corpus, that is a finding worth reporting honestly rather than hiding.

Enable with ``chunking.strategy: uniform``.
"""

from __future__ import annotations

from ..ingest.loaders import RawDocument
from ..tokenize import estimate_tokens
from ..types import Chunk
from .base import make_chunk

__all__ = ["UniformChunker"]


class UniformChunker:
    """Fixed-size overlapping windows over raw text, identical for every source."""

    source = "*"

    def __init__(self, config) -> None:
        self.chunk_tokens = int(config.get("chunking.uniform.chunk_tokens", 1000))
        self.overlap_tokens = int(config.get("chunking.uniform.overlap_tokens", 200))

    def chunk(self, doc: RawDocument) -> list[Chunk]:
        # For forum threads, flatten every post into one blob -- which is
        # precisely the failure mode the thread-aware chunker avoids: competing
        # answers end up sharing a single retrieval score, and any disagreement
        # between them is buried inside a chunk where the detector cannot see it.
        if doc.source == "forum" and doc.posts:
            text = f"{doc.title}\n\n" + "\n\n".join(str(p.get("body", "")) for p in doc.posts)
        else:
            text = f"{doc.title}\n\n{doc.body}"

        words = text.split()
        if not words:
            return []

        # Approximate token budgets in words; the control does not need precision.
        words_per_chunk = max(1, int(self.chunk_tokens / 1.3))
        overlap_words = max(0, int(self.overlap_tokens / 1.3))
        step = max(1, words_per_chunk - overlap_words)

        chunks: list[Chunk] = []
        for i, start in enumerate(range(0, len(words), step)):
            piece = " ".join(words[start : start + words_per_chunk])
            if not piece.strip():
                continue
            chunks.append(
                make_chunk(
                    chunk_id=f"{doc.source}:{doc.doc_id}:u{i}",
                    source=doc.source,
                    body=piece,
                    context_header="",  # no breadcrumb: the control gets no context repair
                    metadata={**doc.metadata, "chunker": "uniform", "window_index": i},
                    parent_id=f"{doc.source}:{doc.doc_id}",
                )
            )
            if start + words_per_chunk >= len(words):
                break
        return chunks

    def token_count(self, text: str) -> int:
        return estimate_tokens(text)
