"""Reranking scorers -- stage 2 of the retrieve-then-rerank funnel.

Why a second stage exists at all
--------------------------------
Dense retrieval compares a query vector against chunk vectors that were computed
**before the query existed**. A bi-encoder embeds the two texts independently,
which is exactly what makes it fast -- all chunk vectors precompute, and search
is nearest-neighbour over millions of them. The cost is that each chunk vector is
a *query-agnostic* summary: it had to compress everything about that chunk into
fixed dimensions without knowing what would be asked.

A **cross-encoder** does the opposite. It consumes ``(query, chunk)`` jointly, so
it can relate every query term to every chunk term: it can notice that the query
says "forever" and the chunk says "loops indefinitely" (same thing), or that the
query says v2 and the chunk says v3 (disqualifying). A bi-encoder cannot do this
in principle, because the two texts never meet in one forward pass.

The price is linear cost with no precomputation -- impossible over 54 chunks'
worth of corpus at scale, trivial over the 60 candidates stage 1 hands over.
That asymmetry is the whole point of the funnel: a cheap filter makes an
expensive, accurate method affordable.

Backends
--------
``SurrogateCrossEncoder`` (default, stdlib)
    Computes genuine **query-document interaction features** -- IDF-weighted term
    coverage, proximity of query terms within the chunk, phrase/bigram matching,
    identifier and version agreement, first-match position. These require seeing
    both texts together, which is the defining property of a cross-encoder, so
    the architectural shape is right even though the scoring function is
    hand-designed rather than learned. It is **not** a trained cross-encoder and
    the README reports it as a surrogate; ``BgeCrossEncoder`` is the real thing.

``BgeCrossEncoder``
    ``BAAI/bge-reranker-v2-m3`` via sentence-transformers (``local`` extra).

``LlmListwiseReranker``
    Claude ranks the top-N as a list, which can apply reasoning no pairwise
    scorer can. Highest quality, highest latency -- reported separately.
"""

from __future__ import annotations

import math
import re
from collections import Counter

from ..tokenize import tokenize
from ..types import Chunk, QueryAnalysis

__all__ = ["SurrogateCrossEncoder", "BgeCrossEncoder", "build_reranker"]


class SurrogateCrossEncoder:
    """Feature-based joint query-document scorer, pure stdlib.

    Every feature below is a *joint* function of query and chunk -- none can be
    precomputed per chunk, which is what distinguishes this from the bi-encoder
    stage rather than duplicating it.
    """

    name = "surrogate"

    #: Feature weights. Tuned by hand against the gold set; the relative ordering
    #: matters more than the exact values.
    W_COVERAGE = 0.34       # fraction of query signal present at all
    W_PROXIMITY = 0.16      # are the matches near each other, or scattered?
    W_PHRASE = 0.14         # contiguous bigram matches
    W_IDENTIFIER = 0.18     # exact identifier hits (429, max_retries, v3.2)
    W_POSITION = 0.08       # answers tend to appear early in a chunk
    W_VERSION = 0.10        # version agreement between query and chunk

    def __init__(self, config, idf: dict[str, float] | None = None) -> None:
        self.config = config
        self.idf = idf or {}

    def fit_idf(self, chunks: list[Chunk]) -> None:
        """Derive IDF over the candidate set so rare terms dominate coverage.

        Without IDF weighting, matching "the" counts as much as matching "429",
        and coverage becomes a length artefact rather than a relevance signal.
        """
        df: Counter[str] = Counter()
        for chunk in chunks:
            df.update(set(tokenize(chunk.text)))
        n = max(1, len(chunks))
        self.idf = {t: math.log(1.0 + (n + 1.0) / (c + 0.5)) for t, c in df.items()}

    # -- scoring -------------------------------------------------------------

    def score(self, query: str, chunk: Chunk, analysis: QueryAnalysis | None = None) -> float:
        q_tokens = tokenize(query)
        if not q_tokens:
            return 0.0
        d_tokens = tokenize(chunk.text)
        if not d_tokens:
            return 0.0

        q_unique = list(dict.fromkeys(q_tokens))
        d_positions: dict[str, list[int]] = {}
        for i, tok in enumerate(d_tokens):
            d_positions.setdefault(tok, []).append(i)

        coverage = self._coverage(q_unique, d_positions)
        proximity = self._proximity(q_unique, d_positions, len(d_tokens))
        phrase = self._phrase(q_tokens, d_tokens)
        identifier = self._identifier(analysis, chunk)
        position = self._position(q_unique, d_positions, len(d_tokens))
        version = self._version(analysis, chunk)

        return (
            self.W_COVERAGE * coverage
            + self.W_PROXIMITY * proximity
            + self.W_PHRASE * phrase
            + self.W_IDENTIFIER * identifier
            + self.W_POSITION * position
            + self.W_VERSION * version
        )

    def score_batch(
        self, query: str, chunks: list[Chunk], analysis: QueryAnalysis | None = None
    ) -> list[float]:
        if not self.idf:
            self.fit_idf(chunks)
        return [self.score(query, c, analysis) for c in chunks]

    # -- features ------------------------------------------------------------

    def _coverage(self, q_unique: list[str], d_positions: dict[str, list[int]]) -> float:
        """IDF-weighted fraction of the query's informative terms present."""
        total = sum(self.idf.get(t, 1.0) for t in q_unique)
        if total <= 0:
            return 0.0
        hit = sum(self.idf.get(t, 1.0) for t in q_unique if t in d_positions)
        return hit / total

    def _proximity(
        self, q_unique: list[str], d_positions: dict[str, list[int]], doc_len: int
    ) -> float:
        """Inverse of the smallest window containing the matched query terms.

        Terms clustered in one passage indicate the chunk is *about* the query;
        the same terms scattered across a long chunk usually means they co-occur
        incidentally. A bi-encoder has no access to this at all.
        """
        matched = [d_positions[t] for t in q_unique if t in d_positions]
        if len(matched) < 2:
            return 1.0 if matched else 0.0
        firsts = [min(p) for p in matched]
        lasts = [max(p) for p in matched]
        span = max(lasts) - min(firsts) + 1
        ideal = len(matched)
        return min(1.0, ideal / max(span, ideal))

    def _phrase(self, q_tokens: list[str], d_tokens: list[str]) -> float:
        """Contiguous bigram overlap -- word order carries meaning.

        "retry forever" and "forever retry" have identical bag-of-words
        representations; only sequence-aware matching separates them.
        """
        if len(q_tokens) < 2:
            return 0.0
        q_bigrams = {(a, b) for a, b in zip(q_tokens, q_tokens[1:])}
        d_bigrams = {(a, b) for a, b in zip(d_tokens, d_tokens[1:])}
        return len(q_bigrams & d_bigrams) / len(q_bigrams)

    def _identifier(self, analysis: QueryAnalysis | None, chunk: Chunk) -> float:
        """Exact-match rate on the identifiers extracted from the query.

        Weighted heavily because on a support corpus an identifier match is close
        to proof of relevance -- a chunk containing ``max_retries`` when the user
        asked about ``max_retries`` is almost certainly on topic.
        """
        if analysis is None or not analysis.entities:
            return 0.0
        text = chunk.text.lower()
        hits = sum(1 for e in analysis.entities if e.lower() in text)
        return hits / len(analysis.entities)

    def _position(
        self, q_unique: list[str], d_positions: dict[str, list[int]], doc_len: int
    ) -> float:
        """Earlier first-match scores higher.

        Both the breadcrumb header and a well-written section lead with the
        topic, so an early match is evidence the chunk is *about* the query
        rather than mentioning it in passing.
        """
        firsts = [min(d_positions[t]) for t in q_unique if t in d_positions]
        if not firsts or doc_len == 0:
            return 0.0
        return 1.0 - (min(firsts) / doc_len)

    def _version(self, analysis: QueryAnalysis | None, chunk: Chunk) -> float:
        """Reward version agreement, penalise disagreement.

        The single clearest example of a judgement only a joint scorer can make:
        "v3.2" and "v2.4" are near-identical in embedding space and could not be
        more different for answering. Neutral (0.5) when either side is silent,
        so absence of a version is never treated as disagreement.
        """
        if analysis is None or not analysis.version_mentioned:
            return 0.5
        chunk_version = chunk.version
        if not chunk_version:
            return 0.5
        q_major = analysis.version_mentioned.split(".")[0]
        c_major = str(chunk_version).split(".")[0]
        if q_major != c_major:
            return 0.0
        return 1.0 if analysis.version_mentioned == str(chunk_version) else 0.75


class BgeCrossEncoder:
    """Real trained cross-encoder via sentence-transformers (optional extra)."""

    name = "cross_encoder"

    def __init__(self, config) -> None:  # pragma: no cover - optional path
        try:
            from sentence_transformers import CrossEncoder
        except ImportError as exc:
            raise ImportError(
                "rerank.backend='cross_encoder' requires: pip install -e '.[local]'"
            ) from exc
        model = config.get("rerank.cross_encoder_model", "BAAI/bge-reranker-v2-m3")
        self._model = CrossEncoder(model)

    def fit_idf(self, chunks: list[Chunk]) -> None:  # pragma: no cover
        return None

    def score_batch(
        self, query: str, chunks: list[Chunk], analysis: QueryAnalysis | None = None
    ) -> list[float]:  # pragma: no cover
        # Score against header + body: the breadcrumb carries the version and
        # authority signal the model should see.
        pairs = [(query, c.text) for c in chunks]
        raw = self._model.predict(pairs)
        return [1.0 / (1.0 + math.exp(-float(s))) for s in raw]

    def score(
        self, query: str, chunk: Chunk, analysis: QueryAnalysis | None = None
    ) -> float:  # pragma: no cover
        return self.score_batch(query, [chunk], analysis)[0]


def build_reranker(config):
    backend = str(config.get("rerank.backend", "surrogate")).lower()
    if backend in {"cross_encoder", "bge"}:
        return BgeCrossEncoder(config)
    return SurrogateCrossEncoder(config)
