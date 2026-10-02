"""Retrieval, reranking and contradiction metrics.

Why the harness exists before any tuning
----------------------------------------
RAG improvements are deeply counterintuitive. Techniques that obviously ought to
help -- semantic chunking, query expansion, LLM reranking -- frequently do not on
a given corpus, and two measured regressions in this project make the point:
unweighted query expansion pushed the correct answer for the 429 query out of the
top three, and full-strength recency decay overrode a 4x lexical relevance
advantage. Neither was visible without measurement.

Metric choices
--------------
**nDCG@10** is the primary metric: position matters (the list is truncated) and
the gold labels are graded (2 = answers it, 1 = useful context), which plain
precision cannot express.

**Recall@k** at the stage-1 pool measures the hard ceiling -- a gold chunk that
never enters the pool can never be recovered by reranking.

**Source-recall** is specific to this problem and absent from standard IR: did
every source the question *needs* actually appear in the final set? A
single-source answer to a multi-source question is a failure that nDCG scores as
a success.

**Rank displacement** is how requirement 4 is evaluated honestly. If gold chunks
show a mean displacement near zero, reranking is not earning its latency however
good the end-to-end numbers look.

Confidence intervals are bootstrapped over queries, because 15 queries produce
wide intervals and a few points of nDCG is not a result.
"""

from __future__ import annotations

import math
import random
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

from .types import Chunk, Relationship, Resolution, ScoredChunk

__all__ = [
    "GoldQuery",
    "load_gold",
    "dcg",
    "ndcg_at_k",
    "recall_at_k",
    "reciprocal_rank",
    "QueryScore",
    "EvaluationReport",
    "evaluate_query",
    "bootstrap_ci",
]


# --------------------------------------------------------------------------- #
# Gold set
# --------------------------------------------------------------------------- #


@dataclass(frozen=True, slots=True)
class GoldQuery:
    qid: str
    query: str
    intent: str
    relevant: dict[str, int]          # label -> grade
    expected_sources: list[str]
    note: str = ""
    expects_conflict: str | None = None
    expects_refusal: bool = False
    expects_no_conflict: bool = False
    answer_must_contain: list[str] = field(default_factory=list)
    answer_must_not_contain: list[str] = field(default_factory=list)

    def grade_of(self, chunk: Chunk) -> int:
        """Highest grade among the labels this chunk satisfies.

        Exact-or-prefix matching lets labels be written per document or per
        section; the maximum is taken so a section-level label never loses to a
        broader document-level one.
        """
        best = 0
        for label, grade in self.relevant.items():
            if chunk.matches_label(label):
                best = max(best, grade)
        return best


def load_gold(path: str | Path) -> list[GoldQuery]:
    data = yaml.safe_load(Path(path).read_text(encoding="utf-8")) or {}
    out: list[GoldQuery] = []
    for item in data.get("queries", []):
        out.append(
            GoldQuery(
                qid=str(item["qid"]),
                query=str(item["query"]),
                intent=str(item.get("intent", "unknown")),
                relevant={str(r["id"]): int(r["grade"]) for r in (item.get("relevant") or [])},
                expected_sources=list(item.get("expected_sources") or []),
                note=str(item.get("note", "")),
                expects_conflict=item.get("expects_conflict"),
                expects_refusal=bool(item.get("expects_refusal", False)),
                expects_no_conflict=bool(item.get("expects_no_conflict", False)),
                answer_must_contain=list(item.get("answer_must_contain") or []),
                answer_must_not_contain=list(item.get("answer_must_not_contain") or []),
            )
        )
    return out


# --------------------------------------------------------------------------- #
# Ranking metrics
# --------------------------------------------------------------------------- #


def dcg(grades: list[int]) -> float:
    """Discounted cumulative gain with the standard log2 position discount."""
    return sum((2**g - 1) / math.log2(i + 2) for i, g in enumerate(grades))


def ndcg_at_k(grades: list[int], ideal: list[int], k: int) -> float:
    """nDCG@k. Returns 0.0 when no relevant chunk exists, never NaN."""
    actual = dcg(grades[:k])
    best = dcg(sorted(ideal, reverse=True)[:k])
    return actual / best if best > 0 else 0.0


def recall_at_k(grades: list[int], total_relevant: int, k: int) -> float:
    if total_relevant <= 0:
        return 0.0
    return sum(1 for g in grades[:k] if g > 0) / total_relevant


def reciprocal_rank(grades: list[int]) -> float:
    for i, g in enumerate(grades):
        if g > 0:
            return 1.0 / (i + 1)
    return 0.0


# --------------------------------------------------------------------------- #
# Per-query evaluation
# --------------------------------------------------------------------------- #


@dataclass
class QueryScore:
    qid: str
    ndcg10: float = 0.0
    recall5: float = 0.0
    recall10: float = 0.0
    recall20_pool: float = 0.0
    mrr: float = 0.0
    source_recall: float = 0.0
    source_precision: float = 0.0
    gold_rank_before: int = -1
    gold_rank_after: int = -1
    gold_displacement: int = 0
    promoted_into_top3: bool = False
    conflict_expected: str | None = None
    conflict_detected: bool = False
    conflict_relationship: str = ""
    conflict_rule: str = ""
    false_positive_conflict: bool = False
    refusal_expected: bool = False
    refused: bool = False
    contains_ok: bool = True
    excludes_ok: bool = True
    latency_ms: float = 0.0

    @property
    def refusal_correct(self) -> bool:
        return self.refused == self.refusal_expected


def evaluate_query(
    gold: GoldQuery,
    pool: list[ScoredChunk],
    final: list[ScoredChunk],
    resolutions: list[Resolution],
    findings: list,
    answer_text: str,
    refused: bool,
    latency_ms: float = 0.0,
    corpus_chunks: list[Chunk] | None = None,
) -> QueryScore:
    """Score one query across retrieval, reranking, conflict and answer checks.

    Args:
        corpus_chunks: Every chunk in the index. Required for correct nDCG and
            recall denominators -- see the note below.

    .. note::
       The ideal ranking and the relevant-set size are computed from the **whole
       corpus**, not from the label list. A label such as ``docs:retries-and-backoff``
       matches every section of that document via prefix matching, so a single
       label can correspond to five relevant chunks. Using the label count as the
       denominator understates the ideal gain and produces nDCG above 1.0 and
       recall above 1.0 -- both of which this evaluator previously reported.
    """
    score = QueryScore(qid=gold.qid)
    score.refusal_expected = gold.expects_refusal
    score.refused = refused
    score.latency_ms = latency_ms
    score.conflict_expected = gold.expects_conflict

    # -- answer content checks (case-insensitive substring) ------------------
    lowered = answer_text.lower()
    score.contains_ok = all(s.lower() in lowered for s in gold.answer_must_contain)
    score.excludes_ok = not any(s.lower() in lowered for s in gold.answer_must_not_contain)

    if not gold.relevant:
        # Out-of-scope query: ranking metrics are undefined, refusal is the test.
        score.contains_ok = True
        return score

    final_grades = [gold.grade_of(sc.chunk) for sc in final]
    pool_grades = [gold.grade_of(sc.chunk) for sc in pool]

    # Ideal ranking over the true relevant set in the corpus. Falls back to the
    # retrieved chunks when no corpus is supplied, which keeps the metric bounded
    # even if a caller forgets the argument.
    universe = corpus_chunks if corpus_chunks else [sc.chunk for sc in pool]
    relevant_grades = [g for g in (gold.grade_of(c) for c in universe) if g > 0]
    total_relevant = len(relevant_grades) or len(gold.relevant)

    score.ndcg10 = ndcg_at_k(final_grades, relevant_grades, 10)
    score.recall5 = recall_at_k(final_grades, total_relevant, 5)
    score.recall10 = recall_at_k(final_grades, total_relevant, 10)
    # Stage-1 recall is the ceiling on everything downstream, so it is measured
    # against the candidate pool rather than the final set.
    score.recall20_pool = recall_at_k(pool_grades, total_relevant, 20)
    score.mrr = reciprocal_rank(final_grades)

    # -- source representation ----------------------------------------------
    if gold.expected_sources:
        present = {sc.chunk.source for sc in final}
        expected = set(gold.expected_sources)
        score.source_recall = len(expected & present) / len(expected)
        score.source_precision = (
            sum(1 for sc in final if sc.chunk.source in expected) / len(final) if final else 0.0
        )

    # -- reranking contribution ---------------------------------------------
    # Tracked on the single best gold chunk: aggregate displacement over all
    # chunks mixes promotions of marginal context with the one that matters.
    best = None
    for sc in final:
        if gold.grade_of(sc.chunk) >= 2:
            if best is None or sc.rank_after < best.rank_after:
                best = sc
    if best is not None:
        score.gold_rank_before = best.rank_before
        score.gold_rank_after = best.rank_after
        score.gold_displacement = best.rank_delta
        score.promoted_into_top3 = best.rank_before > 3 and best.rank_after <= 3

    # -- conflict handling --------------------------------------------------
    conflict_rels = {str(r.finding.relationship) for r in resolutions}
    all_rels = {str(f.relationship) for f in findings}

    if gold.expects_no_conflict:
        # C11: the detector must see the pair but classify it as non-conflicting.
        score.false_positive_conflict = bool(
            [r for r in resolutions if r.finding.topic == "worker_concurrency_default"]
        )
        score.conflict_detected = "complementary" in all_rels
        score.conflict_relationship = "complementary" if score.conflict_detected else ""
    elif gold.expects_conflict:
        score.conflict_detected = bool(resolutions)
        if resolutions:
            score.conflict_relationship = str(resolutions[0].finding.relationship)
            score.conflict_rule = resolutions[0].rule_fired
    else:
        score.false_positive_conflict = bool(
            [r for r in resolutions if r.finding.relationship is Relationship.CONTRADICT]
        )

    return score


# --------------------------------------------------------------------------- #
# Aggregation
# --------------------------------------------------------------------------- #


@dataclass
class EvaluationReport:
    config_name: str
    scores: list[QueryScore] = field(default_factory=list)

    def mean(self, attr: str) -> float:
        values = [getattr(s, attr) for s in self.scores if getattr(s, attr) is not None]
        numeric = [float(v) for v in values if isinstance(v, (int, float, bool))]
        return sum(numeric) / len(numeric) if numeric else 0.0

    def mean_where_scored(self, attr: str) -> float:
        """Mean over queries that have gold labels (excludes refusal-only cases)."""
        values = [
            float(getattr(s, attr)) for s in self.scores if not s.refusal_expected
        ]
        return sum(values) / len(values) if values else 0.0

    def summary(self) -> dict[str, Any]:
        scored = [s for s in self.scores if not s.refusal_expected]
        displacements = [abs(s.gold_displacement) for s in scored if s.gold_rank_before > 0]
        return {
            "config": self.config_name,
            "queries": len(self.scores),
            "ndcg@10": round(self.mean_where_scored("ndcg10"), 4),
            "recall@5": round(self.mean_where_scored("recall5"), 4),
            "recall@10": round(self.mean_where_scored("recall10"), 4),
            "pool_recall@20": round(self.mean_where_scored("recall20_pool"), 4),
            "mrr": round(self.mean_where_scored("mrr"), 4),
            "source_recall": round(self.mean_where_scored("source_recall"), 4),
            "source_precision": round(self.mean_where_scored("source_precision"), 4),
            "mean_gold_displacement": round(
                sum(displacements) / len(displacements) if displacements else 0.0, 3
            ),
            "promotion_rate_top3": round(
                sum(1 for s in scored if s.promoted_into_top3) / len(scored) if scored else 0.0, 4
            ),
            "conflict_detection_rate": round(self._conflict_rate(), 4),
            "conflict_false_positive_rate": round(
                sum(1 for s in self.scores if s.false_positive_conflict) / len(self.scores)
                if self.scores
                else 0.0,
                4,
            ),
            "refusal_accuracy": round(
                sum(1 for s in self.scores if s.refusal_correct) / len(self.scores)
                if self.scores
                else 0.0,
                4,
            ),
            "answer_contains_pass": round(
                sum(1 for s in self.scores if s.contains_ok) / len(self.scores) if self.scores else 0.0,
                4,
            ),
            "answer_excludes_pass": round(
                sum(1 for s in self.scores if s.excludes_ok) / len(self.scores) if self.scores else 0.0,
                4,
            ),
            "p50_latency_ms": round(self._percentile([s.latency_ms for s in self.scores], 50), 2),
            "p95_latency_ms": round(self._percentile([s.latency_ms for s in self.scores], 95), 2),
        }

    def _conflict_rate(self) -> float:
        expected = [s for s in self.scores if s.conflict_expected and not s.false_positive_conflict]
        if not expected:
            return 0.0
        return sum(1 for s in expected if s.conflict_detected) / len(expected)

    @staticmethod
    def _percentile(values: list[float], pct: float) -> float:
        if not values:
            return 0.0
        ordered = sorted(values)
        idx = min(len(ordered) - 1, int(len(ordered) * pct / 100))
        return ordered[idx]


def bootstrap_ci(
    values: list[float], *, samples: int = 1000, seed: int = 42, alpha: float = 0.05
) -> tuple[float, float]:
    """Bootstrap percentile confidence interval for a mean.

    Resamples over queries, which is the unit of variation. With 15 queries the
    interval is wide, and reporting it is the point -- a 2-point nDCG difference
    on a set this size is not a finding.
    """
    if not values:
        return (0.0, 0.0)
    rng = random.Random(seed)
    n = len(values)
    means: list[float] = []
    for _ in range(samples):
        resample = [values[rng.randrange(n)] for _ in range(n)]
        means.append(sum(resample) / n)
    means.sort()
    lo = means[int(samples * alpha / 2)]
    hi = means[min(samples - 1, int(samples * (1 - alpha / 2)))]
    return (round(lo, 4), round(hi, 4))
