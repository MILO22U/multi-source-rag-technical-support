"""Dense retrieval backends.

The job of a dense index is to match *meaning* where BM25 can only match words:
a chunk saying "the backoff schedule may loop indefinitely" should be retrievable
by the query "jobs retry forever" despite sharing no terms.

Three backends sit behind one protocol:

``LsaBackend`` (default, stdlib)
    Latent Semantic Analysis -- TF-IDF followed by truncated SVD. Genuinely
    captures term co-occurrence, so synonyms and paraphrases land near each
    other, and it needs no model download, no GPU and no network. That makes
    every number in the README reproducible on any Python 3.10+ install, which
    is why it is the default. It is weaker than a trained encoder, and the gap
    between it and ``SentenceTransformerBackend`` is itself a reportable result
    rather than something to hide.

``SentenceTransformerBackend``
    A real neural encoder (``BAAI/bge-m3``). Activates with the ``local`` extra.

``VoyageBackend``
    Hosted embeddings (``voyage-3-large``). Note that **Anthropic has no
    embeddings endpoint** -- Claude is used for intent routing, contradiction
    judging and synthesis in this project, never for vectors.

Implementation note on LSA
--------------------------
SVD is computed through the Gram matrix rather than on the term-document matrix
directly. With ``n`` chunks and a vocabulary of ``V`` terms, ``A`` is ``n x V``
(here 54 x ~2500), so decomposing ``A^T A`` would mean a 2500 x 2500
eigenproblem. ``A A^T`` is only ``n x n``, and its eigenvectors give the same
left singular vectors. Jacobi rotation on a 54 x 54 symmetric matrix is exact
and instant, which is what makes a from-scratch dense index practical here.
"""

from __future__ import annotations

import math
from collections import Counter
from typing import Protocol, Sequence

from ..tokenize import tokenize
from ..types import Chunk

__all__ = ["EmbeddingBackend", "LsaBackend", "SentenceTransformerBackend", "VoyageBackend", "build_backend"]

Vector = list[float]


class EmbeddingBackend(Protocol):
    """Protocol every dense backend satisfies.

    ``fit`` sees the whole collection (LSA needs global statistics); neural
    backends ignore it beyond caching.
    """

    dims: int

    def fit(self, chunks: Sequence[Chunk]) -> None: ...
    def embed_documents(self, chunks: Sequence[Chunk]) -> list[Vector]: ...
    def embed_query(self, query: str) -> Vector: ...


# --------------------------------------------------------------------------- #
# Linear algebra helpers (stdlib only)
# --------------------------------------------------------------------------- #


def _l2_normalise(vec: Vector) -> Vector:
    norm = math.sqrt(sum(v * v for v in vec))
    return [v / norm for v in vec] if norm > 1e-12 else vec


def cosine(a: Vector, b: Vector) -> float:
    """Cosine similarity of two vectors, assuming neither is degenerate."""
    if not a or not b:
        return 0.0
    dot = sum(x * y for x, y in zip(a, b))
    na = math.sqrt(sum(x * x for x in a))
    nb = math.sqrt(sum(y * y for y in b))
    return dot / (na * nb) if na > 1e-12 and nb > 1e-12 else 0.0


def _jacobi_eigen(matrix: list[list[float]], iterations: int = 24) -> tuple[list[float], list[list[float]]]:
    """Eigendecompose a symmetric matrix by cyclic Jacobi rotation.

    Chosen over power iteration because it returns the full spectrum in one pass
    with no convergence tuning, and because at ``n <= ~200`` its cubic cost is
    irrelevant. Exact to floating point for symmetric input.

    Returns:
        ``(eigenvalues, eigenvectors)`` where ``eigenvectors[i][j]`` is component
        ``i`` of eigenvector ``j``, sorted by descending eigenvalue.
    """
    n = len(matrix)
    a = [row[:] for row in matrix]
    v = [[1.0 if i == j else 0.0 for j in range(n)] for i in range(n)]

    for _ in range(iterations):
        off_diagonal = sum(a[i][j] ** 2 for i in range(n) for j in range(i + 1, n))
        if off_diagonal < 1e-18:
            break
        for p in range(n - 1):
            for q in range(p + 1, n):
                if abs(a[p][q]) < 1e-15:
                    continue
                theta = (a[q][q] - a[p][p]) / (2.0 * a[p][q])
                t = (1.0 if theta >= 0 else -1.0) / (abs(theta) + math.sqrt(theta * theta + 1.0))
                c = 1.0 / math.sqrt(t * t + 1.0)
                s = t * c
                for k in range(n):
                    akp, akq = a[k][p], a[k][q]
                    a[k][p] = c * akp - s * akq
                    a[k][q] = s * akp + c * akq
                for k in range(n):
                    apk, aqk = a[p][k], a[q][k]
                    a[p][k] = c * apk - s * aqk
                    a[q][k] = s * apk + c * aqk
                for k in range(n):
                    vkp, vkq = v[k][p], v[k][q]
                    v[k][p] = c * vkp - s * vkq
                    v[k][q] = s * vkp + c * vkq

    eigenvalues = [a[i][i] for i in range(n)]
    order = sorted(range(n), key=lambda i: -eigenvalues[i])
    sorted_values = [eigenvalues[i] for i in order]
    sorted_vectors = [[v[row][i] for i in order] for row in range(n)]
    return sorted_values, sorted_vectors


# --------------------------------------------------------------------------- #
# LSA
# --------------------------------------------------------------------------- #


class LsaBackend:
    """TF-IDF + truncated SVD dense retrieval, implemented from scratch."""

    def __init__(self, config) -> None:
        self.dims = int(config.get("index.dense.dims", 256))
        self.min_df = int(config.get("index.dense.min_df", 2))
        self.sublinear_tf = bool(config.get("index.dense.sublinear_tf", True))
        self.iterations = int(config.get("index.dense.svd_iterations", 24))

        self.idf: dict[str, float] = {}
        self._doc_vectors: list[Vector] = []
        self._tfidf: list[dict[str, float]] = []
        # Projection pieces: query coords need U, the singular values, and the
        # training TF-IDF vectors (V is never materialised -- it would be V x k).
        self._u: list[list[float]] = []
        self._singular: list[float] = []
        self._k = 0

    # -- fitting -------------------------------------------------------------

    def fit(self, chunks: Sequence[Chunk]) -> None:
        if not chunks:
            self._doc_vectors = []
            return

        tokenised = [tokenize(c.text) for c in chunks]
        df: Counter[str] = Counter()
        for toks in tokenised:
            df.update(set(toks))

        n = len(chunks)
        # min_df drops hapax terms, which in a small corpus are mostly noise and
        # would otherwise dominate the leading singular directions.
        effective_min_df = self.min_df if n > 4 else 1
        vocab = {t for t, c in df.items() if c >= effective_min_df}
        self.idf = {t: math.log((1.0 + n) / (1.0 + df[t])) + 1.0 for t in vocab}

        self._tfidf = [self._vectorise(toks) for toks in tokenised]

        # Gram matrix of L2-normalised TF-IDF vectors: G = A A^T, n x n.
        gram = [[0.0] * n for _ in range(n)]
        for i in range(n):
            vi = self._tfidf[i]
            gram[i][i] = sum(w * w for w in vi.values())
            for j in range(i + 1, n):
                vj = self._tfidf[j]
                shorter, longer = (vi, vj) if len(vi) <= len(vj) else (vj, vi)
                dot = sum(w * longer.get(t, 0.0) for t, w in shorter.items())
                gram[i][j] = gram[j][i] = dot

        values, vectors = _jacobi_eigen(gram, self.iterations)
        self._k = min(self.dims, sum(1 for v in values if v > 1e-9), n)
        if self._k == 0:
            self._doc_vectors = [[0.0] for _ in range(n)]
            return

        self._u = [[vectors[row][d] for d in range(self._k)] for row in range(n)]
        self._singular = [math.sqrt(max(values[d], 0.0)) for d in range(self._k)]

        # Document coordinates in latent space: X = U * S^(1/2).
        self._doc_vectors = [
            _l2_normalise([self._u[i][d] * math.sqrt(self._singular[d]) for d in range(self._k)])
            for i in range(n)
        ]

    def _vectorise(self, tokens: list[str]) -> dict[str, float]:
        counts = Counter(t for t in tokens if t in self.idf)
        if not counts:
            return {}
        vec = {
            t: (1.0 + math.log(tf) if self.sublinear_tf else float(tf)) * self.idf[t]
            for t, tf in counts.items()
        }
        norm = math.sqrt(sum(w * w for w in vec.values()))
        return {t: w / norm for t, w in vec.items()} if norm > 1e-12 else vec

    # -- inference -----------------------------------------------------------

    def embed_documents(self, chunks: Sequence[Chunk]) -> list[Vector]:
        if not self._doc_vectors:
            self.fit(chunks)
        return self._doc_vectors

    def embed_query(self, query: str) -> Vector:
        """Fold a query into the latent space.

        Standard LSA folding-in: ``y_d = (1/s_d) * sum_i U[i][d] * <q, a_i>``,
        then scaled by ``sqrt(s_d)`` to match the document coordinate scaling.
        A query sharing no vocabulary with the fitted corpus yields a zero
        vector, which correctly scores 0.0 against everything rather than
        producing spurious similarity.
        """
        if not self._u or self._k == 0:
            return []
        q = self._vectorise(tokenize(query))
        if not q:
            return [0.0] * self._k

        # <q, a_i> for every training document.
        projections = [
            sum(w * self._tfidf[i].get(t, 0.0) for t, w in q.items()) for i in range(len(self._tfidf))
        ]
        coords: Vector = []
        for d in range(self._k):
            s = self._singular[d]
            if s < 1e-9:
                coords.append(0.0)
                continue
            acc = sum(self._u[i][d] * projections[i] for i in range(len(projections)))
            coords.append((acc / s) * math.sqrt(s))
        return _l2_normalise(coords)


# --------------------------------------------------------------------------- #
# Optional neural backends
# --------------------------------------------------------------------------- #


class SentenceTransformerBackend:
    """Real neural embeddings via sentence-transformers (optional extra)."""

    def __init__(self, config) -> None:
        try:
            from sentence_transformers import SentenceTransformer
        except ImportError as exc:  # pragma: no cover - optional path
            raise ImportError(
                "index.dense.backend='sentence_transformers' requires: pip install -e '.[local]'"
            ) from exc
        model_name = config.get("index.dense.sentence_transformers_model", "BAAI/bge-m3")
        self._model = SentenceTransformer(model_name)
        self.dims = int(self._model.get_sentence_embedding_dimension())

    def fit(self, chunks: Sequence[Chunk]) -> None:  # pragma: no cover
        return None

    def embed_documents(self, chunks: Sequence[Chunk]) -> list[Vector]:  # pragma: no cover
        texts = [c.text for c in chunks]
        return [list(map(float, v)) for v in self._model.encode(texts, normalize_embeddings=True)]

    def embed_query(self, query: str) -> Vector:  # pragma: no cover
        return list(map(float, self._model.encode([query], normalize_embeddings=True)[0]))


class VoyageBackend:
    """Hosted embeddings via Voyage AI (optional).

    Anthropic does not serve an embeddings endpoint; Claude is used elsewhere in
    this pipeline for intent routing, contradiction judging and synthesis.
    """

    def __init__(self, config) -> None:  # pragma: no cover - optional path
        try:
            import voyageai
        except ImportError as exc:
            raise ImportError("index.dense.backend='voyage' requires: pip install voyageai") from exc
        self._client = voyageai.Client()
        self._model = config.get("index.dense.voyage_model", "voyage-3-large")
        self.dims = 1024

    def fit(self, chunks: Sequence[Chunk]) -> None:  # pragma: no cover
        return None

    def embed_documents(self, chunks: Sequence[Chunk]) -> list[Vector]:  # pragma: no cover
        texts = [c.text for c in chunks]
        out: list[Vector] = []
        for start in range(0, len(texts), 128):
            batch = texts[start : start + 128]
            out.extend(self._client.embed(batch, model=self._model, input_type="document").embeddings)
        return out

    def embed_query(self, query: str) -> Vector:  # pragma: no cover
        return self._client.embed([query], model=self._model, input_type="query").embeddings[0]


def build_backend(config) -> EmbeddingBackend:
    """Instantiate the backend named by ``index.dense.backend``."""
    name = str(config.get("index.dense.backend", "lsa")).lower()
    if name == "lsa":
        return LsaBackend(config)
    if name in {"sentence_transformers", "st", "bge"}:
        return SentenceTransformerBackend(config)
    if name == "voyage":
        return VoyageBackend(config)
    raise ValueError(f"unknown dense backend: {name!r}")
