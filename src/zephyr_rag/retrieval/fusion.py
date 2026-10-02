"""Reciprocal Rank Fusion -- combining ranked lists without comparing scores.

The naive combination is ``0.5 * bm25 + 0.5 * cosine``, and it is broken for a
reason worth understanding because it generalises.

**BM25 scores are unbounded and corpus-dependent.** A strong match might score
17.6 (as ``forum:t_0041:p2`` does on this corpus) or 3.2, depending on IDF values
and document lengths. There is no ceiling and the absolute number means nothing.

**Cosine similarity occupies a narrow band.** In practice almost everything lands
between roughly 0.4 and 0.85, even poor matches. The useful signal lives in a
sliver of the range.

Adding them lets BM25's scale dominate arbitrarily, and the mixing weight you
choose is really a scale-correction factor with a relevance preference tangled
into it -- two different things in one number, tuned on one corpus and wrong on
the next.

RRF discards magnitudes and keeps only ranks, which *are* trustworthy::

    RRF(d) = sum over lists of  1 / (k + rank(d))        k = 60

Properties that fall out for free:

* **agreement wins** -- a document ranked highly by two independent methods
  scores above one ranked highly by a single method, which is exactly the
  evidence-combining behaviour wanted, with no calibration.
* **the curve is gentle** -- at ``k=60``, rank 1 and rank 8 differ by ~10%, so
  fusion is forgiving of small rank differences and sensitive mainly to
  *appearing at all*. Smaller ``k`` sharpens the preference for top positions.
* **scale-free** -- no normalisation, nothing to tune per corpus.

The cost is real: if BM25's top hit scored 40 and the runner-up 2, that gap was
meaningful and RRF erases it. RRF's value is being hard to misuse, not being
optimal.
"""

from __future__ import annotations

from collections import defaultdict

from ..types import Chunk

__all__ = ["reciprocal_rank_fusion", "fuse_ranked_lists", "RankedList"]

#: ``(chunk, score, rank)`` triples as returned by the index search methods.
RankedList = list[tuple[Chunk, float, int]]


def reciprocal_rank_fusion(
    ranked_lists: dict[str, RankedList],
    *,
    k: int = 60,
    list_weights: dict[str, float] | None = None,
) -> dict[str, float]:
    """Fuse several ranked lists into one score per chunk id.

    Args:
        ranked_lists: Named lists, e.g. ``{"bm25_q0": [...], "dense_q1": [...]}``.
            The names are carried into the trace so a result can be explained.
        k: RRF constant. Larger values flatten the rank preference.
        list_weights: Optional per-list multiplier. Defaults to 1.0 for every
            list.

    Returns:
        ``chunk_id -> fused score``. Only chunks appearing in at least one list
        are present; absence is not zero, it is exclusion.

    Why weighting is necessary once query expansion is in play
    ----------------------------------------------------------
    Unweighted RRF counts every list equally, and that interacts badly with
    expansion. With one original query plus two expansions across two retrievers
    there are six lists, and at ``k=60`` the gap between rank 1 and rank 20 is
    only about 25%. A broadly-relevant chunk that appears mid-list in all six
    therefore outscores the chunk ranked **first** for the user's actual question
    but absent from the looser paraphrases.

    This was not hypothetical -- it was observed on this corpus. Before
    weighting, "Why do my jobs retry forever when the API returns 429?" returned
    three unrelated forum threads, while ``forum:t_0041:p2`` (the correct answer,
    BM25 rank 1 with a score of 17.6) fell outside the top 3. Expansion was
    actively destroying precision in exchange for recall nobody needed.

    Down-weighting expansion lists restores the ordering while keeping the
    vocabulary-mismatch coverage that expansion exists to provide.
    """
    weights = list_weights or {}
    fused: dict[str, float] = defaultdict(float)
    for name, entries in ranked_lists.items():
        weight = weights.get(name, 1.0)
        if weight <= 0.0:
            continue
        for chunk, _score, rank in entries:
            fused[chunk.chunk_id] += weight / (k + rank)
    return dict(fused)


def fuse_ranked_lists(
    ranked_lists: dict[str, RankedList],
    *,
    k: int = 60,
    list_weights: dict[str, float] | None = None,
) -> tuple[dict[str, float], dict[str, dict[str, float]]]:
    """RRF plus a per-chunk breakdown of where the score came from.

    The breakdown is what makes requirement 6 meaningful: the trace can record
    that a chunk ranked 1st lexically and 7th densely, rather than only that it
    ended up with a fused score of 0.031.

    Returns:
        ``(fused_scores, components)`` where ``components[chunk_id]`` holds
        ``{"<list>_rank": rank, "<list>_score": raw_score, ...}``.
    """
    fused = reciprocal_rank_fusion(ranked_lists, k=k, list_weights=list_weights)
    components: dict[str, dict[str, float]] = defaultdict(dict)
    for name, entries in ranked_lists.items():
        for chunk, score, rank in entries:
            components[chunk.chunk_id][f"{name}_rank"] = float(rank)
            components[chunk.chunk_id][f"{name}_score"] = float(score)
    for chunk_id, score in fused.items():
        components[chunk_id]["rrf"] = score
        components[chunk_id]["lists_matched"] = float(
            sum(1 for name in ranked_lists if f"{name}_rank" in components[chunk_id])
        )
    return fused, dict(components)
