"""Hybrid retriever -- orchestrates stage 1 of the funnel.

Flow per query::

    analyse -> per-source (BM25 x N queries, dense x N queries)
            -> RRF fusion within each source
            -> intent weight x authority x recency
            -> version filter (hard)
            -> per-source floor + global pool

Two details here are easy to omit and expensive to omit.

**Stage 1 optimises recall, not precision.** Its job is not to be right, it is to
not *lose* the answer: a gold chunk that never enters the candidate pool can
never be recovered, because a reranker can only reorder what it is given. Stage-1
recall is the hard ceiling on the entire system, which is why the pool is 60 and
not 10.

**The per-source floor is load-bearing.** ``retrieval.min_per_source`` guarantees
each source contributes candidates when it has anything plausible. Without it,
good weighting concentrates the pool on one source, the final chunk set becomes
single-source, and cross-source contradiction detection silently never fires --
requirement 5 becomes dead code that still passes its unit tests. Requirements
3/4 and requirement 5 are in genuine tension here, and this is where it is
resolved.
"""

from __future__ import annotations

from ..index.store import IndexStore
from ..types import Chunk, QueryAnalysis, ScoredChunk, SOURCES
from .analyzer import build_analyzer
from .fusion import fuse_ranked_lists
from .weights import SourceWeighter

__all__ = ["HybridRetriever", "RetrievalResult"]


class RetrievalResult:
    """Stage-1 output plus the diagnostics the trace needs."""

    def __init__(
        self,
        analysis: QueryAnalysis,
        candidates: list[ScoredChunk],
        per_source_stats: dict[str, dict[str, float]],
        weights_applied: dict[str, float],
    ) -> None:
        self.analysis = analysis
        self.candidates = candidates
        self.per_source_stats = per_source_stats
        self.weights_applied = weights_applied

    @property
    def source_distribution(self) -> dict[str, int]:
        dist: dict[str, int] = {}
        for sc in self.candidates:
            dist[sc.chunk.source] = dist.get(sc.chunk.source, 0) + 1
        return dist


class HybridRetriever:
    """Per-source hybrid retrieval with fusion and weighting."""

    def __init__(self, store: IndexStore, config) -> None:
        self.store = store
        self.config = config
        self.analyzer = build_analyzer(config)
        self.weighter = SourceWeighter(config)

        self.top_k_per_source = int(config.get("retrieval.top_k_per_source", 20))
        self.pool_size = int(config.get("retrieval.candidate_pool", 60))
        self.min_per_source = int(config.get("retrieval.min_per_source", 2))
        self.rrf_k = int(config.get("retrieval.rrf_k", 60))
        self.expansion_weight = float(
            config.get("retrieval.query_analysis.expansion_weight", 0.35)
        )
        self.hard_version_filter = bool(
            config.get("retrieval.query_analysis.version_filter_is_hard", True)
        )

    # -- public --------------------------------------------------------------

    def retrieve(self, query: str, analysis: QueryAnalysis | None = None) -> RetrievalResult:
        analysis = analysis or self.analyzer.analyze(query)
        queries = analysis.all_queries

        per_source_scored: dict[str, list[ScoredChunk]] = {}
        per_source_stats: dict[str, dict[str, float]] = {}

        for source in SOURCES:
            index = self.store.indices.get(source)
            if index is None or not len(index):
                per_source_stats[source] = {"bm25_hits": 0, "dense_hits": 0, "fused": 0}
                continue

            # Each expansion is its own ranked list, so a chunk found by several
            # phrasings accumulates RRF votes -- agreement across phrasings is
            # the same kind of evidence as agreement across retrievers.
            ranked_lists = {}
            list_weights: dict[str, float] = {}
            bm25_hits = dense_hits = 0
            for qi, q in enumerate(queries):
                # The original query (qi == 0) carries full weight; expansions are
                # down-weighted. Without this, a chunk appearing mid-list across
                # every paraphrase outscores the chunk ranked first for the actual
                # question -- measured on this corpus, it pushed the correct answer
                # to the 429 query out of the top 3 entirely.
                weight = 1.0 if qi == 0 else self.expansion_weight
                bm25 = index.search_bm25(q, self.top_k_per_source)
                dense = index.search_dense(q, self.top_k_per_source)
                if bm25:
                    ranked_lists[f"bm25_q{qi}"] = bm25
                    list_weights[f"bm25_q{qi}"] = weight
                    bm25_hits += len(bm25)
                if dense:
                    ranked_lists[f"dense_q{qi}"] = dense
                    list_weights[f"dense_q{qi}"] = weight
                    dense_hits += len(dense)

            if not ranked_lists:
                per_source_stats[source] = {"bm25_hits": 0, "dense_hits": 0, "fused": 0}
                continue

            fused, components = fuse_ranked_lists(
                ranked_lists, k=self.rrf_k, list_weights=list_weights
            )

            scored: list[ScoredChunk] = []
            for chunk_id, rrf_score in fused.items():
                chunk = self.store.get(chunk_id)
                if chunk is None or not self._passes_version_filter(chunk, analysis):
                    continue
                weighted, weight_parts = self.weighter.score(chunk, rrf_score, analysis.intent)
                scored.append(
                    ScoredChunk(
                        chunk=chunk,
                        retrieval_score=weighted,
                        final_score=weighted,
                        components={**components.get(chunk_id, {}), **weight_parts},
                    )
                )

            scored.sort(key=lambda sc: (-sc.retrieval_score, sc.chunk.chunk_id))
            per_source_scored[source] = scored
            per_source_stats[source] = {
                "bm25_hits": float(bm25_hits),
                "dense_hits": float(dense_hits),
                "fused": float(len(fused)),
                "top_score": round(scored[0].retrieval_score, 6) if scored else 0.0,
            }

        candidates = self._assemble_pool(per_source_scored)
        candidates = [sc.with_ranks(before=i + 1) for i, sc in enumerate(candidates)]

        weights_applied = {
            source: self.weighter.source_weight(source, analysis.intent) for source in SOURCES
        }
        return RetrievalResult(analysis, candidates, per_source_stats, weights_applied)

    # -- internals -----------------------------------------------------------

    def _passes_version_filter(self, chunk: Chunk, analysis: QueryAnalysis) -> bool:
        """Hard filter on explicitly mentioned major versions.

        A hard constraint deserves a hard tool: "I'm on v2.4" should *exclude*
        v3-only content, not merely down-weight it, because a soft penalty still
        lets a strongly-matching v3 chunk win and hand the user an answer that is
        wrong for their installation.

        Filtering is on the **major** version only. Minor versions carry real
        behavioural differences in this corpus (3.0 vs 3.1 for ``Retry-After``),
        and excluding a 3.0 bug report from a 3.2 query would delete exactly the
        evidence the empirical-override rule needs. Changelogs and migration
        guides are always exempt -- they are inherently cross-version.
        """
        if not (self.hard_version_filter and analysis.version_mentioned):
            return True

        meta = chunk.metadata
        if meta.get("doc_type") in {"changelog", "guide"}:
            return True
        if str(meta.get("product_area")) == "migration":
            return True

        chunk_version = chunk.version
        if not chunk_version:
            return True

        wanted_major = analysis.version_mentioned.split(".")[0]
        return str(chunk_version).split(".")[0] == wanted_major

    def _assemble_pool(self, per_source: dict[str, list[ScoredChunk]]) -> list[ScoredChunk]:
        """Build the candidate pool, guaranteeing per-source representation first.

        Reserved slots are filled before the open competition, so a source that
        would lose every head-to-head comparison still contributes the evidence
        that makes cross-source conflict detection possible.
        """
        pool: list[ScoredChunk] = []
        taken: set[str] = set()

        for source in SOURCES:
            for sc in per_source.get(source, [])[: self.min_per_source]:
                if sc.chunk.chunk_id not in taken:
                    pool.append(sc)
                    taken.add(sc.chunk.chunk_id)

        remaining = [
            sc
            for source in SOURCES
            for sc in per_source.get(source, [])
            if sc.chunk.chunk_id not in taken
        ]
        remaining.sort(key=lambda sc: (-sc.retrieval_score, sc.chunk.chunk_id))

        for sc in remaining:
            if len(pool) >= self.pool_size:
                break
            pool.append(sc)
            taken.add(sc.chunk.chunk_id)

        pool.sort(key=lambda sc: (-sc.retrieval_score, sc.chunk.chunk_id))
        return pool
