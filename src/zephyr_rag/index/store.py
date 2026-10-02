"""Per-source index store.

The central structural decision: **one index pair per source, never one global
index.**

Corpus asymmetry is why. This corpus holds 23 documentation chunks, 20 forum
chunks and 11 blog chunks, and in a realistic deployment the ratios are far more
lopsided -- forums are verbose (people restate the question, chat, say thanks)
while documentation is terse and information-dense. A single global index lets
the chattiest source dominate results through sheer surface area, not through
relevance. Nobody chooses that; it is an artefact of how much text each source
happens to contain.

Retrieving top-k *per source* and combining afterwards makes source
representation a decision the system makes explicitly, which is what requirement
3 is actually asking for.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from ..types import Chunk, SOURCES
from .bm25 import BM25Index
from .embeddings import EmbeddingBackend, build_backend, cosine

__all__ = ["SourceIndex", "IndexStore", "build_store"]


@dataclass
class SourceIndex:
    """Lexical + dense index over one source's chunks."""

    source: str
    chunks: list[Chunk]
    bm25: BM25Index | None = None
    dense: EmbeddingBackend | None = None
    vectors: list[list[float]] = field(default_factory=list)

    # -- search --------------------------------------------------------------

    def search_bm25(self, query: str, top_k: int) -> list[tuple[Chunk, float, int]]:
        """Return ``(chunk, score, rank)`` triples, rank being 1-indexed.

        Ranks are returned alongside scores because fusion consumes ranks, not
        scores -- BM25 magnitudes are unbounded and not comparable to cosine.
        """
        if self.bm25 is None:
            return []
        hits = self.bm25.search(query, top_k=top_k)
        return [(chunk, score, i + 1) for i, (chunk, score) in enumerate(hits)]

    def search_dense(self, query: str, top_k: int) -> list[tuple[Chunk, float, int]]:
        if self.dense is None or not self.vectors:
            return []
        q = self.dense.embed_query(query)
        if not q:
            return []
        scored = [(i, cosine(q, vec)) for i, vec in enumerate(self.vectors)]
        scored = [(i, s) for i, s in scored if s > 1e-6]
        scored.sort(key=lambda kv: (-kv[1], kv[0]))
        return [(self.chunks[i], s, rank + 1) for rank, (i, s) in enumerate(scored[:top_k])]

    def __len__(self) -> int:
        return len(self.chunks)


class IndexStore:
    """All three per-source indices, plus a flat chunk lookup."""

    def __init__(self, indices: dict[str, SourceIndex]) -> None:
        self.indices = indices
        self.by_id: dict[str, Chunk] = {
            chunk.chunk_id: chunk for index in indices.values() for chunk in index.chunks
        }

    @property
    def all_chunks(self) -> list[Chunk]:
        return [c for source in SOURCES for c in self.indices.get(source, SourceIndex(source, [])).chunks]

    def get(self, chunk_id: str) -> Chunk | None:
        return self.by_id.get(chunk_id)

    def counts(self) -> dict[str, int]:
        return {source: len(index) for source, index in self.indices.items()}

    def __repr__(self) -> str:
        return f"IndexStore({self.counts()}, total={len(self.by_id)})"


def build_store(chunks_by_source: dict[str, list[Chunk]], config) -> IndexStore:
    """Build the lexical and dense index for each source.

    The dense backend is fitted **per source**. That is deliberate: LSA's latent
    dimensions are derived from the co-occurrence structure of the collection it
    is fitted on, and fitting one model across all three sources would let the
    dominant source's vocabulary shape the latent space the others are projected
    into. Per-source fitting keeps each space faithful to its own corpus, and
    since comparison happens on ranks rather than scores, the spaces never need
    to be mutually calibrated.
    """
    use_bm25 = bool(config.get("index.bm25.enabled", True))
    use_dense = bool(config.get("index.dense.enabled", True))
    k1 = float(config.get("index.bm25.k1", 1.5))
    b = float(config.get("index.bm25.b", 0.75))

    indices: dict[str, SourceIndex] = {}
    for source, chunks in chunks_by_source.items():
        index = SourceIndex(source=source, chunks=list(chunks))
        if use_bm25 and chunks:
            index.bm25 = BM25Index(index.chunks, k1=k1, b=b)
        if use_dense and chunks:
            backend = build_backend(config)
            backend.fit(index.chunks)
            index.dense = backend
            index.vectors = backend.embed_documents(index.chunks)
        indices[source] = index

    return IndexStore(indices)
