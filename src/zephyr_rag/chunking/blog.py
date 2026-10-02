"""Blog chunker -- overlapping sentence-bounded windows over narrative prose.

Blog posts have no reliable structural seams. Prose flows, headings are decorative
rather than semantic, and a paragraph break often falls mid-argument. So this
chunker falls back to the general-purpose strategy: fixed-size windows with
overlap, cut only at sentence boundaries.

Overlap exists so that a thought spanning a cut survives intact in at least one
chunk. Without it, the sentence pair that actually answers a question can land
with half in each of two chunks and match neither well.

An optional ``semantic_split`` mode cuts where adjacent sentences diverge in
embedding space (a topic shift) instead of at a fixed length. It is off by
default and exists to be measured: semantic chunking is widely assumed to beat
fixed windows and frequently does not, and a documented negative result is a
real finding.

Blog chunk ids are ordinal (``blog:post-id:w0``) because prose has no stable
heading to address. The gold set therefore labels blog relevance at document
level, which :meth:`Chunk.matches_label` supports via prefix matching.
"""

from __future__ import annotations

from ..ingest.loaders import RawDocument
from ..tokenize import estimate_tokens, sentences, tokenize
from ..types import Chunk
from .base import make_chunk
from .contextual import build_header

__all__ = ["BlogChunker"]


class BlogChunker:
    """Slide a sentence-aligned window over a blog post body."""

    source = "blog"

    def __init__(self, config) -> None:
        self.config = config
        self.window_tokens = int(config.get("chunking.blog.window_tokens", 700))
        self.overlap_ratio = float(config.get("chunking.blog.overlap_ratio", 0.15))
        self.semantic = bool(config.get("chunking.blog.semantic_split", False))
        self.percentile = float(config.get("chunking.blog.semantic_percentile", 25))

    def chunk(self, doc: RawDocument) -> list[Chunk]:
        sents = sentences(doc.body)
        if not sents:
            return []

        groups = self._semantic_windows(sents) if self.semantic else self._sliding_windows(sents)
        header = build_header("blog", self.config, metadata=doc.metadata)

        chunks: list[Chunk] = []
        for i, group in enumerate(groups):
            body = " ".join(group).strip()
            if not body:
                continue
            chunks.append(
                make_chunk(
                    chunk_id=f"blog:{doc.doc_id}:w{i}",
                    source="blog",
                    body=body,
                    context_header=header,
                    metadata={
                        **doc.metadata,
                        "window_index": i,
                        "window_count": len(groups),
                        "chunker": "semantic_window" if self.semantic else "sliding_window",
                    },
                    parent_id=f"blog:{doc.doc_id}",
                )
            )
        return chunks

    # -- strategies ----------------------------------------------------------

    def _sliding_windows(self, sents: list[str]) -> list[list[str]]:
        """Fixed-token windows with ``overlap_ratio`` carried between them."""
        target_overlap = int(self.window_tokens * self.overlap_ratio)
        windows: list[list[str]] = []
        current: list[str] = []
        current_tokens = 0

        for sentence in sents:
            t = estimate_tokens(sentence)
            if current and current_tokens + t > self.window_tokens:
                windows.append(current)
                # Seed the next window with trailing sentences worth ~overlap tokens.
                carry: list[str] = []
                carried = 0
                for prev in reversed(current):
                    pt = estimate_tokens(prev)
                    if carried + pt > target_overlap:
                        break
                    carry.insert(0, prev)
                    carried += pt
                current = carry
                current_tokens = carried
            current.append(sentence)
            current_tokens += t

        if current:
            windows.append(current)
        return windows

    def _semantic_windows(self, sents: list[str]) -> list[list[str]]:
        """Cut where consecutive sentences are least similar (a topic shift).

        Similarity is Jaccard overlap on normalised tokens rather than embedding
        cosine. That keeps this path dependency-free and is a reasonable proxy on
        prose, but it is weaker than a true embedding-based split -- which is
        part of why this mode is off by default and reported as an ablation
        rather than used as the headline configuration.
        """
        if len(sents) < 3:
            return [sents]

        token_sets = [set(tokenize(s)) for s in sents]
        sims: list[float] = []
        for a, b in zip(token_sets, token_sets[1:]):
            union = a | b
            sims.append(len(a & b) / len(union) if union else 0.0)

        if not sims:
            return [sents]
        ordered = sorted(sims)
        idx = max(0, min(len(ordered) - 1, int(len(ordered) * self.percentile / 100)))
        threshold = ordered[idx]

        windows: list[list[str]] = []
        current = [sents[0]]
        current_tokens = estimate_tokens(sents[0])
        for i, sentence in enumerate(sents[1:]):
            t = estimate_tokens(sentence)
            boundary = sims[i] <= threshold and current_tokens >= self.window_tokens * 0.4
            if boundary or current_tokens + t > self.window_tokens:
                windows.append(current)
                current = []
                current_tokens = 0
            current.append(sentence)
            current_tokens += t
        if current:
            windows.append(current)
        return windows
