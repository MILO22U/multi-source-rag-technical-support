"""Reranking: cross-encoder scoring, score blending, diversity (Requirement 4)."""

from __future__ import annotations

from ..types import QueryAnalysis, ScoredChunk
from .blend import apply_caps, blend_scores, minmax_normalise
from .cross_encoder import BgeCrossEncoder, SurrogateCrossEncoder, build_reranker
from .mmr import mmr_select

__all__ = [
    "SurrogateCrossEncoder",
    "BgeCrossEncoder",
    "build_reranker",
    "blend_scores",
    "apply_caps",
    "minmax_normalise",
    "mmr_select",
    "Reranker",
]


class Reranker:
    """Full stage-2 pipeline: score -> blend -> diversify -> cap.

    Ordering is deliberate. Caps run **before** MMR so that source
    representation is guaranteed first and diversity trimming operates inside an
    already-balanced set; running MMR first could drop the only candidate from a
    source and leave the caps nothing to enforce.
    """

    def __init__(self, config) -> None:
        self.config = config
        self.enabled = bool(config.get("rerank.enabled", True))
        self.scorer = build_reranker(config) if self.enabled else None
        self.alpha = float(config.get("rerank.alpha", 0.70))
        self.top_n = int(config.get("rerank.rerank_top_n", 60))
        self.final_k = int(config.get("rerank.final_k", 8))
        self.mmr_enabled = bool(config.get("rerank.mmr.enabled", True))
        self.mmr_lambda = float(config.get("rerank.mmr.lambda", 0.70))
        self.max_per_source = int(config.get("rerank.caps.max_per_source", 5))
        self.max_per_document = int(config.get("rerank.caps.max_per_document", 3))

    def rerank(
        self,
        query: str,
        candidates: list[ScoredChunk],
        analysis: QueryAnalysis | None = None,
    ) -> tuple[list[ScoredChunk], dict[str, float]]:
        """Return ``(final_chunks, stats)``.

        ``stats`` records the reranker's measurable contribution -- mean absolute
        rank displacement and whether the top-1 changed -- which is the honest way
        to evaluate requirement 4. A reranker whose mean displacement is ~0 is not
        earning its latency, however good the end-to-end numbers look.
        """
        if not candidates:
            return [], {"reranked": 0.0}

        if not self.enabled or self.scorer is None:
            final = candidates[: self.final_k]
            final = [sc.with_ranks(after=i + 1) for i, sc in enumerate(final)]
            return final, {"reranked": 0.0, "mean_abs_rank_delta": 0.0, "top1_changed": 0.0}

        pool = candidates[: self.top_n]
        chunks = [sc.chunk for sc in pool]
        self.scorer.fit_idf(chunks)
        ce_scores = self.scorer.score_batch(query, chunks, analysis)

        blended = blend_scores(pool, ce_scores, alpha=self.alpha)

        capped = apply_caps(
            blended,
            final_k=self.final_k,
            max_per_source=self.max_per_source,
            max_per_document=self.max_per_document,
        )
        if self.mmr_enabled:
            capped = mmr_select(capped, k=self.final_k, lambda_=self.mmr_lambda)

        deltas = [abs(sc.rank_delta) for sc in blended[: self.final_k] if sc.rank_before > 0]
        stats = {
            "reranked": float(len(pool)),
            "mean_abs_rank_delta": (sum(deltas) / len(deltas)) if deltas else 0.0,
            "top1_changed": float(
                bool(blended and candidates and blended[0].chunk.chunk_id != candidates[0].chunk.chunk_id)
            ),
            "ce_min": min(ce_scores) if ce_scores else 0.0,
            "ce_max": max(ce_scores) if ce_scores else 0.0,
            "alpha": self.alpha,
        }
        return capped, stats
