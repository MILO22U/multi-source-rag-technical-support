"""Maximal Marginal Relevance -- selecting for *marginal* value, not absolute value.

Relevance ranking scores each chunk independently, so it has no notion of "I
already know this". On this corpus documentation and blog posts frequently restate
the same fact in different words, which means a top-8 chosen purely by relevance
can spend several slots on one piece of information.

MMR fixes it by selecting greedily and charging each candidate for its similarity
to what has already been chosen::

    score(c) = lambda * relevance(c) - (1 - lambda) * max_similarity(c, selected)

The concept -- *marginal* rather than absolute value -- recurs throughout
information retrieval and is worth carrying beyond this module.

Similarity here is Jaccard overlap on normalised tokens rather than embedding
cosine. Near-duplicate restatements of the same fact share heavy vocabulary, so a
lexical measure detects exactly the redundancy that matters, and it keeps this
path dependency-free and deterministic.

One important scoping decision: redundancy is penalised, **disagreement is not**.
Two chunks can be lexically similar while making opposite claims (``timeout=0``
"disables the timeout" versus "means fail-fast"), and those are precisely the
pairs requirement 5 needs to see. MMR therefore runs with a moderate lambda and
*after* the per-source caps, so it trims restatement without collapsing the
cross-source pairs that conflict detection depends on.
"""

from __future__ import annotations

from ..tokenize import tokenize
from ..types import ScoredChunk

__all__ = ["jaccard", "mmr_select"]


def jaccard(a: set[str], b: set[str]) -> float:
    if not a or not b:
        return 0.0
    union = a | b
    return len(a & b) / len(union) if union else 0.0


def mmr_select(
    ranked: list[ScoredChunk],
    *,
    k: int,
    lambda_: float = 0.7,
) -> list[ScoredChunk]:
    """Greedily select ``k`` chunks balancing relevance against novelty.

    Args:
        ranked: Candidates in descending relevance order.
        k: How many to select.
        lambda_: 1.0 is pure relevance (no diversity); 0.0 is pure novelty, which
            actively selects for irrelevance and is never what you want.

    Returns:
        Selected chunks with ``rank_after`` renumbered to selection order.
    """
    if not ranked or k <= 0:
        return []
    if lambda_ >= 1.0:
        return ranked[:k]

    token_sets = {sc.chunk.chunk_id: set(tokenize(sc.chunk.display_text)) for sc in ranked}

    selected: list[ScoredChunk] = [ranked[0]]
    remaining = list(ranked[1:])

    while remaining and len(selected) < k:
        best_index, best_score = 0, float("-inf")
        for i, candidate in enumerate(remaining):
            cand_tokens = token_sets[candidate.chunk.chunk_id]
            redundancy = max(
                (jaccard(cand_tokens, token_sets[s.chunk.chunk_id]) for s in selected),
                default=0.0,
            )
            score = lambda_ * candidate.final_score - (1.0 - lambda_) * redundancy
            if score > best_score:
                best_index, best_score = i, score
        selected.append(remaining.pop(best_index))

    return [sc.with_ranks(after=i + 1) for i, sc in enumerate(selected)]
