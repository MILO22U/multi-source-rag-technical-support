"""Per-query tracing (Requirement 6).

In a multi-source system, source attribution is not plumbing -- it is **part of
the answer's meaning**. "Retries default to 5" and "the v3.2 reference page says
5, though a 2025 blog post says 3 because it changed in v3.0" are different
answers with different usefulness, and the difference is entirely provenance.

The design target for a trace record is that it answers *"why did the system say
that?"* without re-running the query. Concretely, every record carries:

* the query, and the intent the analyzer assigned
* the source weights that intent selected
* per-source candidate counts from each retrieval arm
* per-chunk scores **and ``rank_before`` / ``rank_after``**
* conflicts found, and which precedence rule fired on each
* citations, source distribution, stage latencies

``rank_before``/``rank_after`` is the field most often omitted and most needed:
nearly every interesting question about the system -- is reranking doing anything,
is stage-1 recall adequate, is the blend weight sane -- is answerable from that
pair alone, and it cannot be reconstructed afterwards.

Tracing is also a design discipline. If a decision cannot be logged, the pipeline
probably has no explicit decision point there -- it has an accident. Writing this
schema early forces the implicit to become explicit.
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from ..types import Answer, QueryAnalysis, Resolution, ScoredChunk

__all__ = ["QueryTrace", "TraceWriter"]


@dataclass
class QueryTrace:
    """Accumulates one query's decision record, then serialises it."""

    query: str
    trace_id: str = ""
    timestamp: str = ""
    config_name: str = "default"
    analysis: dict[str, Any] = field(default_factory=dict)
    weights_applied: dict[str, float] = field(default_factory=dict)
    retrieval: dict[str, Any] = field(default_factory=dict)
    rerank: dict[str, Any] = field(default_factory=dict)
    final_chunks: list[dict[str, Any]] = field(default_factory=list)
    source_distribution: dict[str, int] = field(default_factory=dict)
    conflicts: list[dict[str, Any]] = field(default_factory=list)
    citations: list[dict[str, Any]] = field(default_factory=list)
    answer_text: str = ""
    refused: bool = False
    generator: str = ""
    timing_ms: dict[str, float] = field(default_factory=dict)

    _marks: dict[str, float] = field(default_factory=dict, repr=False)

    def __post_init__(self) -> None:
        if not self.trace_id:
            self.trace_id = f"q_{int(time.time() * 1000) % 10_000_000:07d}"
        if not self.timestamp:
            self.timestamp = time.strftime("%Y-%m-%dT%H:%M:%S")

    # -- timing --------------------------------------------------------------

    def start(self, stage: str) -> None:
        self._marks[stage] = time.perf_counter()

    def stop(self, stage: str) -> None:
        if stage in self._marks:
            self.timing_ms[stage] = round((time.perf_counter() - self._marks.pop(stage)) * 1000, 2)

    # -- recording -----------------------------------------------------------

    def record_analysis(self, analysis: QueryAnalysis) -> None:
        self.analysis = {
            "intent": str(analysis.intent),
            "product_area": analysis.product_area,
            "version_mentioned": analysis.version_mentioned,
            "expanded_queries": analysis.expanded_queries,
            "entities": analysis.entities,
            "requires_canonical_answer": analysis.requires_canonical_answer,
            "classifier": analysis.classifier,
        }

    def record_retrieval(
        self,
        per_source_stats: dict[str, dict[str, float]],
        candidates: list[ScoredChunk],
        weights: dict[str, float],
    ) -> None:
        self.weights_applied = {k: round(v, 4) for k, v in weights.items()}
        dist: dict[str, int] = {}
        for sc in candidates:
            dist[sc.chunk.source] = dist.get(sc.chunk.source, 0) + 1
        self.retrieval = {
            "per_source": per_source_stats,
            "candidates_considered": len(candidates),
            "candidate_distribution": dist,
        }

    def record_rerank(self, stats: dict[str, float]) -> None:
        self.rerank = {k: (round(v, 4) if isinstance(v, float) else v) for k, v in stats.items()}

    def record_final(self, chunks: list[ScoredChunk]) -> None:
        self.final_chunks = [
            {
                "chunk_id": sc.chunk.chunk_id,
                "source": sc.chunk.source,
                "rank_before": sc.rank_before,
                "rank_after": sc.rank_after,
                "rank_delta": sc.rank_delta,
                "retrieval_score": round(sc.retrieval_score, 6),
                "ce_score": round(sc.ce_score, 6) if sc.ce_score is not None else None,
                "final_score": round(sc.final_score, 6),
                "authority": round(float(sc.components.get("authority", 0.0)), 4),
                "recency": round(float(sc.components.get("recency", 1.0)), 4),
                "source_weight": round(float(sc.components.get("source_weight", 0.0)), 4),
                "version": sc.chunk.version,
                "context_header": sc.chunk.context_header,
            }
            for sc in chunks
        ]
        dist: dict[str, int] = {}
        for sc in chunks:
            dist[sc.chunk.source] = dist.get(sc.chunk.source, 0) + 1
        self.source_distribution = dist

    def record_conflicts(self, resolutions: list[Resolution], findings: list) -> None:
        """Log resolutions *and* non-conflict findings.

        Non-conflicts are recorded deliberately: the false-positive rate is only
        measurable if the detector's negative judgements are visible too.
        """
        self.conflicts = [
            {
                "chunk_a": r.finding.chunk_a_id,
                "chunk_b": r.finding.chunk_b_id,
                "relationship": str(r.finding.relationship),
                "topic": r.finding.topic,
                "confidence": round(r.finding.confidence, 3),
                "claim_a": r.finding.claim_a[:240],
                "claim_b": r.finding.claim_b[:240],
                "rule_fired": r.rule_fired,
                "winner": r.winner,
                "loser": r.loser,
                "explanation": r.explanation,
                "disclosed": r.must_disclose,
                "detector": r.finding.detector,
            }
            for r in resolutions
        ]
        self.retrieval["non_conflict_findings"] = [
            {
                "chunk_a": f.chunk_a_id,
                "chunk_b": f.chunk_b_id,
                "relationship": str(f.relationship),
                "topic": f.topic,
            }
            for f in findings
            if not f.relationship.is_conflict
        ]

    def record_answer(self, answer: Answer) -> None:
        self.answer_text = answer.text
        self.refused = answer.refused
        self.generator = answer.generator
        self.citations = [
            {
                "chunk_id": c.chunk_id,
                "source": c.source,
                "sentence_index": c.sentence_index,
                "cited_text": c.cited_text[:200],
            }
            for c in answer.citations
        ]

    # -- output --------------------------------------------------------------

    def to_dict(self) -> dict[str, Any]:
        self.timing_ms["total"] = round(sum(v for k, v in self.timing_ms.items() if k != "total"), 2)
        return {
            "trace_id": self.trace_id,
            "timestamp": self.timestamp,
            "config": self.config_name,
            "query": self.query,
            "analysis": self.analysis,
            "weights_applied": self.weights_applied,
            "retrieval": self.retrieval,
            "rerank": self.rerank,
            "final_chunks": self.final_chunks,
            "source_distribution": self.source_distribution,
            "conflicts": self.conflicts,
            "citations": self.citations,
            "answer_text": self.answer_text,
            "refused": self.refused,
            "generator": self.generator,
            "timing_ms": self.timing_ms,
        }

    def explain(self) -> str:
        """Human-readable trace for ``--explain``.

        The most useful debugging artefact in the project: the rank columns make
        the reranker's contribution visible at a glance.
        """
        a = self.analysis
        lines = [
            "=" * 78,
            f"QUERY   {self.query}",
            f"TRACE   {self.trace_id}   config={self.config}  total={self.timing_ms.get('total', 0)}ms"
            if hasattr(self, "config")
            else f"TRACE   {self.trace_id}",
            "-" * 78,
            f"INTENT  {a.get('intent')}   area={a.get('product_area')}   "
            f"version={a.get('version_mentioned')}   classifier={a.get('classifier')}",
            f"ENTITIES {a.get('entities')}",
            f"WEIGHTS {self.weights_applied}",
            f"POOL    {self.retrieval.get('candidates_considered')} candidates "
            f"{self.retrieval.get('candidate_distribution')}",
            f"RERANK  alpha={self.rerank.get('alpha')}  "
            f"mean_abs_rank_delta={self.rerank.get('mean_abs_rank_delta')}  "
            f"top1_changed={bool(self.rerank.get('top1_changed'))}",
            "-" * 78,
            # ASCII only: this prints to a terminal, and Windows consoles default
            # to cp1252, where a Greek delta raises UnicodeEncodeError and takes
            # the whole command down.
            f"{'#':>2}  {'was':>4}  {'move':>4}  {'ce':>5}  {'final':>5}  chunk",
        ]
        for c in self.final_chunks:
            delta = c["rank_delta"]
            arrow = "  ." if delta == 0 else (f"{delta:+3d}" if delta else "   ")
            ce = f"{c['ce_score']:.3f}" if c["ce_score"] is not None else "  -  "
            lines.append(
                f"{c['rank_after']:>2}  {c['rank_before']:>4}  {arrow:>4}  {ce:>5}  "
                f"{c['final_score']:.3f}  {c['chunk_id']}"
            )
        lines.append("-" * 78)
        lines.append(f"SOURCES {self.source_distribution}")
        if self.conflicts:
            lines.append(f"CONFLICTS ({len(self.conflicts)})")
            for c in self.conflicts:
                lines.append(
                    f"  [{c['relationship']}] {c['topic']}  conf={c['confidence']}"
                )
                lines.append(f"     rule={c['rule_fired']}  ->  winner={c['winner']}")
        else:
            lines.append("CONFLICTS none")
        nonconf = self.retrieval.get("non_conflict_findings") or []
        if nonconf:
            lines.append(
                f"NON-CONFLICTS {len(nonconf)} "
                f"({', '.join(sorted({n['relationship'] for n in nonconf}))})"
            )
        lines.append("=" * 78)
        return "\n".join(lines)


class TraceWriter:
    """Appends trace records to a JSONL file."""

    def __init__(self, path: str | Path, enabled: bool = True) -> None:
        self.path = Path(path)
        self.enabled = enabled
        if self.enabled:
            self.path.parent.mkdir(parents=True, exist_ok=True)

    def write(self, trace: QueryTrace) -> None:
        if not self.enabled:
            return
        with self.path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(trace.to_dict(), ensure_ascii=False) + "\n")
