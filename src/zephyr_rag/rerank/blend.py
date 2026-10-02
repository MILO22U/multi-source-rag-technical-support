"""Score blending and the final selection stage.

The subtle failure mode this module exists to prevent
-----------------------------------------------------
A reranker scores **topical relevance only**. It knows nothing about source
weighting, chunk authority, recency, or version filters -- every signal
requirement 3 is built from. So the natural-looking implementation::

    final_ranking = sort_by(cross_encoder_score)    # WRONG

silently discards all of requirement 3. The system still works, the tests still
pass, and the intent-conditioned weight matrix has no effect on the output
whatsoever.

So scores are **blended**::

    S_final = alpha * CE_norm + (1 - alpha) * S_retrieval_norm

``alpha = 1.0`` is reranker-only (requirement 3 discarded); ``alpha = 0.0`` is
weighting-only (requirement 4 discarded). The sweep over alpha in
``scripts/ablations.py`` is the most informative single figure in the report
precisely because its endpoints are those two degenerate systems, and the
question is whether any blend beats both.

Normalisation is **within the candidate set, never global**. Cross-query
normalisation leaks information between queries and makes per-query scores
incomparable, which would quietly corrupt every aggregate metric.
"""

from __future__ import annotations

from ..types import Chunk, QueryAnalysis, ScoredChunk

__all__ = ["minmax_normalise", "blend_scores", "apply_caps"]


def minmax_normalise(values: list[float]) -> list[float]:
    """Scale to ``[0, 1]`` within this list only.

    A degenerate range (all values equal) maps to 0.5 rather than 0.0 or 1.0:
    mapping to an extreme would assert a confidence the data does not support.
    """
    if not values:
        return []
    lo, hi = min(values), max(values)
    if hi - lo < 1e-12:
        return [0.5] * len(values)
    return [(v - lo) / (hi - lo) for v in values]


def blend_scores(
    candidates: list[ScoredChunk],
    ce_scores: list[float],
    *,
    alpha: float,
) -> list[ScoredChunk]:
    """Combine reranker and retrieval scores, preserving both for the trace.

    Args:
        candidates: Stage-1 output, each already carrying ``rank_before``.
        ce_scores: Reranker score per candidate, same order.
        alpha: Weight on the reranker. 0.7 is the configured default.

    Returns:
        Candidates sorted by blended score, with ``rank_after`` assigned. Both
        component scores and both ranks are retained so the report can quantify
        how much work reranking actually did.
    """
    if not candidates:
        return []

    ce_norm = minmax_normalise(ce_scores)
    retrieval_norm = minmax_normalise([sc.retrieval_score for sc in candidates])

    blended: list[ScoredChunk] = []
    for sc, ce_raw, ce_n, ret_n in zip(candidates, ce_scores, ce_norm, retrieval_norm):
        final = alpha * ce_n + (1.0 - alpha) * ret_n
        blended.append(
            ScoredChunk(
                chunk=sc.chunk,
                retrieval_score=sc.retrieval_score,
                final_score=final,
                ce_score=ce_raw,
                rank_before=sc.rank_before,
                rank_after=-1,
                components={
                    **sc.components,
                    "ce_raw": ce_raw,
                    "ce_norm": ce_n,
                    "retrieval_norm": ret_n,
                    "alpha": alpha,
                    "blended": final,
                },
            )
        )

    blended.sort(key=lambda sc: (-sc.final_score, sc.chunk.chunk_id))
    return [sc.with_ranks(after=i + 1) for i, sc in enumerate(blended)]


def apply_caps(
    ranked: list[ScoredChunk],
    *,
    final_k: int,
    max_per_source: int,
    max_per_document: int,
) -> list[ScoredChunk]:
    """Select the final set subject to per-source and per-document quotas.

    Caps are not a diversity nicety here -- they are what keeps requirement 5
    alive. A conflict is detected by comparing chunks against each other, so a
    final set drawn entirely from one source has nothing to compare and
    contradiction detection silently never fires.

    This is the direct tension between requirements 3/4 and 5: retrieval and
    reranking that are *too* effective at identifying the single best source
    starve conflict detection of the cross-source evidence it needs. Caps resolve
    it by bounding how completely any one source may win.

    Overflow behaviour matters: if caps leave fewer than ``final_k`` slots filled,
    the remaining slots are filled by score order ignoring caps. Returning a
    short list would be worse -- it would withhold relevant context purely to
    satisfy a diversity quota.
    """
    selected: list[ScoredChunk] = []
    per_source: dict[str, int] = {}
    per_doc: dict[str, int] = {}
    deferred: list[ScoredChunk] = []

    for sc in ranked:
        if len(selected) >= final_k:
            break
        source = sc.chunk.source
        doc = sc.chunk.doc_prefix
        if per_source.get(source, 0) >= max_per_source or per_doc.get(doc, 0) >= max_per_document:
            deferred.append(sc)
            continue
        selected.append(sc)
        per_source[source] = per_source.get(source, 0) + 1
        per_doc[doc] = per_doc.get(doc, 0) + 1

    for sc in deferred:
        if len(selected) >= final_k:
            break
        selected.append(sc)

    # Re-rank the selected set so rank_after reflects final presentation order.
    selected.sort(key=lambda sc: (-sc.final_score, sc.chunk.chunk_id))
    return [sc.with_ranks(after=i + 1) for i, sc in enumerate(selected)]
