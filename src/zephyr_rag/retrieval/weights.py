"""Source weighting, authority, and recency -- the core of requirement 3.

Three signals, kept deliberately separate because they answer different questions
and need different machinery:

**Relevance** (elsewhere: BM25, dense, reranker) -- *is this about the question?*
Query-dependent and learned.

**Source weight** (here) -- *how much should I trust this source for this kind of
question?* Query-dependent via intent, but a policy decision rather than a
learned one.

**Authority and recency** (here) -- *should I believe this particular chunk?*
Query-**in**dependent properties of the chunk, computable once at index time.

Conflating them is a common design error. Keeping them apart is what lets the
trace explain a ranking and lets the ablation table isolate each contribution.

The final stage-1 score::

    S(c) = w[intent][source] * RRF(c) * (1 + beta * authority(c)) * recency(c)

Multiplicative rather than additive so that a weak signal attenuates rather than
being swamped: a zero-relevance chunk cannot be rescued by high authority, which
is the correct behaviour.
"""

from __future__ import annotations

import math
from datetime import date, datetime

from ..types import Chunk, Intent

__all__ = ["SourceWeighter"]


def _parse_date(value: object) -> date | None:
    if not value:
        return None
    text = str(value)[:10]
    try:
        return datetime.strptime(text, "%Y-%m-%d").date()
    except ValueError:
        return None


class SourceWeighter:
    """Applies intent-conditioned source weights, authority and recency decay."""

    def __init__(self, config) -> None:
        self.config = config
        self.weights: dict[str, dict[str, float]] = config.get("retrieval.weights", {}) or {}
        self.beta = float(config.get("retrieval.authority.beta", 0.30))

        self.docs_type_prior: dict[str, float] = config.get(
            "retrieval.authority.docs_type_prior", {}
        ) or {}
        self.w_accepted = float(config.get("retrieval.authority.forum_accepted_weight", 0.5))
        self.w_votes = float(config.get("retrieval.authority.forum_votes_weight", 0.3))
        self.w_staff = float(config.get("retrieval.authority.forum_staff_weight", 0.2))
        self.votes_saturation = float(config.get("retrieval.authority.forum_votes_saturation", 40))
        self.blog_staff = float(config.get("retrieval.authority.blog_staff_bonus", 1.0))
        self.blog_guest = float(config.get("retrieval.authority.blog_guest_bonus", 0.3))

        self.recency_enabled = bool(config.get("retrieval.recency.enabled", True))
        # Bounds recency's influence so it acts as a tie-breaker rather than a
        # primary ranking signal. See the docstring of :meth:`score`.
        self.beta_recency = float(config.get("retrieval.recency.strength", 0.5))
        self.half_life = float(config.get("retrieval.recency.half_life_days", 400))
        self.floor = float(config.get("retrieval.recency.floor", 0.6))
        self.skip_intents = set(config.get("retrieval.recency.skip_for_intents", []) or [])
        # A fixed reference date, never date.today(): a moving reference makes
        # recency-weighted metrics drift between runs and breaks reproducibility.
        self.reference_date = _parse_date(config.get("retrieval.recency.reference_date")) or date(2026, 10, 1)

    # -- source weight -------------------------------------------------------

    def source_weight(self, source: str, intent: Intent | str) -> float:
        """Weight for ``source`` under ``intent``.

        The matrix encodes the project's central claim: documentation dominates
        reference lookups (1.00 vs 0.45 forum), forums *outrank* documentation
        for troubleshooting (1.00 vs 0.65), and blogs dominate conceptual
        questions (1.00 vs 0.70 docs). A static prior cannot express that.
        """
        key = str(intent)
        profile = self.weights.get(key) or self.weights.get("unknown") or {}
        return float(profile.get(source, 0.7))

    # -- authority -----------------------------------------------------------

    def authority(self, chunk: Chunk) -> float:
        """Query-independent credibility in ``[0, 1]``."""
        meta = chunk.metadata
        if chunk.source == "docs":
            return float(self.docs_type_prior.get(str(meta.get("doc_type", "reference")), 0.8))

        if chunk.source == "forum":
            if meta.get("unanswered"):
                return 0.10  # a symptom report, not an answer
            accepted = 1.0 if meta.get("is_accepted") else 0.0
            votes = min(float(meta.get("votes", 0)) / self.votes_saturation, 1.0)
            role = str(meta.get("author_role", "user"))
            staff = 1.0 if role == "staff" else (0.6 if role == "mvp" else 0.0)
            score = self.w_accepted * accepted + self.w_votes * votes + self.w_staff * staff
            # A corroborated answer (someone confirmed it worked) is stronger
            # evidence than an uncorroborated one of equal votes.
            if meta.get("corroborating_followups"):
                score = min(1.0, score + 0.05)
            return score

        if chunk.source == "blog":
            role = str(meta.get("author_role", "guest"))
            return self.blog_staff if role == "staff" else self.blog_guest
        return 0.5

    # -- recency -------------------------------------------------------------

    def recency(self, chunk: Chunk, intent: Intent | str) -> float:
        """Exponential age decay with a floor, skipped for timeless intents.

        Two refinements over naive decay, both load-bearing:

        * **skip for conceptual/opinion** -- a 2024 post explaining *why* a queue
          needs idempotency is as true now as then. Decaying it penalises the
          source that is actually best for that intent.
        * **floor** -- age alone must never bury a canonical reference page. Docs
          get a flat 1.0 regardless of ``last_updated``: a reference page is
          maintained, and its edit date reflects documentation churn rather than
          how current the described behaviour is.
        """
        if not self.recency_enabled or str(intent) in self.skip_intents:
            return 1.0
        if chunk.source == "docs":
            return 1.0

        meta = chunk.metadata
        published = _parse_date(
            meta.get("published_at") or meta.get("created_at") or meta.get("last_updated")
        )
        if published is None:
            return 1.0

        age_days = max(0, (self.reference_date - published).days)
        decay = math.exp(-math.log(2.0) * age_days / self.half_life)
        return max(self.floor, decay)

    # -- combination ---------------------------------------------------------

    def score(
        self,
        chunk: Chunk,
        rrf_score: float,
        intent: Intent | str,
    ) -> tuple[float, dict[str, float]]:
        """Combine fused relevance with source weight, authority and recency.

        .. math::
            S(c) = w_{intent}[src] \\cdot RRF(c)
                   \\cdot (1 + \\beta_a A(c))
                   \\cdot (1 + \\beta_r (R(c) - 1))

        Note the shape of the recency term. Applying ``R(c)`` as a direct
        multiplier is the obvious implementation and it is **wrong**, for a
        reason that is specific to rank-based fusion and was measured on this
        corpus rather than reasoned about in the abstract.

        RRF deliberately discards score magnitude, which compresses relevance
        into a narrow band: across the top candidates for one query the fused
        scores spanned 0.045-0.056, roughly a 25% spread. Raw recency decay spans
        ``[floor, 1.0]`` -- a 67% spread. A modifier with a wider dynamic range
        than the signal it modifies does not modify it, it *replaces* it.

        Observed consequence: for "Why do my jobs retry forever when the API
        returns 429?", ``forum:t_0041:p2`` was rank 1 in all six ranked lists
        with a BM25 score of 14.8-21.1 against a runner-up at 4.5-7.1 -- a 3-4x
        lexical advantage -- and still finished 6th, purely because it was seven
        months old (recency 0.694) while less relevant but fresher chunks scored
        0.86-0.91.

        Scaling the deviation by ``beta_r`` bounds recency's influence to a
        tie-breaker, which is the role it should have had from the start. The
        same reasoning already applied to authority via ``beta_a``; recency was
        the oversight.

        Returns:
            ``(score, components)``. The component dict is written verbatim into
            the query trace, so any ranking decision can be reconstructed from
            the log without re-running the query.
        """
        w = self.source_weight(chunk.source, intent)
        a = self.authority(chunk)
        r = self.recency(chunk, intent)
        recency_factor = 1.0 + self.beta_recency * (r - 1.0)
        score = w * rrf_score * (1.0 + self.beta * a) * recency_factor
        return score, {
            "source_weight": w,
            "authority": a,
            "recency": r,
            "recency_factor": recency_factor,
            "weighted_score": score,
        }
