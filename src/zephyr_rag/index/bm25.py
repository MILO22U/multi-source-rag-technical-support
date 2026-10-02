"""BM25-Okapi lexical index, pure standard library.

BM25 scores a document by the query terms it contains, with three corrections
that are each ordinary common sense:

* **saturating term frequency** -- a chunk containing ``429`` five times is more
  about 429s than one containing it once, but not five times more.
* **inverse document frequency** -- rare terms are informative. ``429`` appears
  in a handful of chunks; ``the`` appears in all of them.
* **length normalisation** -- a long chunk contains query terms by accident, so
  length is penalised.

Why this is not optional on a technical-support corpus: the highest-value query
terms here are identifiers (``429``, ``max_retries``, ``v3.2``, ``X-Zephyr-Key``),
and embeddings systematically blur exactly those. ``max_retries`` and
``max_retry_delay`` are near-identical in embedding space and are different
configuration keys with different answers. BM25 does not care -- a rare token is
a rare token.

Its complementary weakness is total blindness to paraphrase: a chunk saying
"the backoff schedule may loop indefinitely" scores zero against the query
"jobs retry forever". That is precisely the gap the dense index fills, and the
reason hybrid retrieval beats either component alone.
"""

from __future__ import annotations

import math
from collections import Counter

from ..tokenize import tokenize
from ..types import Chunk

__all__ = ["BM25Index"]


class BM25Index:
    """Okapi BM25 over a chunk collection.

    Args:
        chunks: Documents to index.
        k1: Term-frequency saturation. Higher means term repetition keeps
            mattering for longer; 1.2-2.0 is the usual range.
        b: Length-normalisation strength. 0 disables it, 1 applies it fully.
    """

    def __init__(self, chunks: list[Chunk], *, k1: float = 1.5, b: float = 0.75) -> None:
        self.k1 = k1
        self.b = b
        self.chunks = chunks
        self.doc_tokens: list[list[str]] = [tokenize(c.text) for c in chunks]
        self.doc_freqs: list[Counter[str]] = [Counter(toks) for toks in self.doc_tokens]
        self.doc_lens: list[int] = [len(toks) for toks in self.doc_tokens]
        self.avg_len: float = (sum(self.doc_lens) / len(self.doc_lens)) if self.doc_lens else 0.0

        # Document frequency per term, then IDF.
        df: Counter[str] = Counter()
        for freqs in self.doc_freqs:
            df.update(freqs.keys())
        self.df = df

        n = len(chunks)
        # Lucene-style IDF: always positive, so a term appearing in every
        # document contributes ~0 rather than a negative score that would make
        # matching a common term actively harmful.
        self.idf: dict[str, float] = {
            term: math.log(1.0 + (n - freq + 0.5) / (freq + 0.5)) for term, freq in df.items()
        }

        # term -> [(doc_index, term_frequency)], so scoring touches only the
        # documents that contain a query term.
        self.postings: dict[str, list[tuple[int, int]]] = {}
        for idx, freqs in enumerate(self.doc_freqs):
            for term, tf in freqs.items():
                self.postings.setdefault(term, []).append((idx, tf))

    # -- search --------------------------------------------------------------

    def search(self, query: str, top_k: int = 20) -> list[tuple[Chunk, float]]:
        """Return the ``top_k`` highest-scoring chunks for ``query``."""
        scores = self.score_all(query)
        ranked = sorted(scores.items(), key=lambda kv: (-kv[1], kv[0]))[:top_k]
        return [(self.chunks[i], s) for i, s in ranked if s > 0.0]

    def score_all(self, query: str) -> dict[int, float]:
        """Raw BM25 score per document index for every document that matches.

        Returned unnormalised on purpose. BM25 scores are unbounded and
        corpus-dependent, so they are never compared directly against cosine
        similarity; fusion operates on ranks instead (see ``retrieval/fusion.py``).
        """
        q_terms = tokenize(query)
        if not q_terms:
            return {}

        scores: dict[int, float] = {}
        for term, q_tf in Counter(q_terms).items():
            postings = self.postings.get(term)
            if not postings:
                continue
            idf = self.idf.get(term, 0.0)
            for doc_idx, tf in postings:
                dl = self.doc_lens[doc_idx]
                norm = 1.0 - self.b + self.b * (dl / self.avg_len if self.avg_len else 1.0)
                contribution = idf * (tf * (self.k1 + 1.0)) / (tf + self.k1 * norm)
                # Repeated query terms count once per occurrence.
                scores[doc_idx] = scores.get(doc_idx, 0.0) + contribution * q_tf
        return scores

    def __len__(self) -> int:
        return len(self.chunks)

    def __repr__(self) -> str:
        return f"BM25Index(docs={len(self.chunks)}, vocab={len(self.df)})"
