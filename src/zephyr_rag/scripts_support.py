"""Evaluation and ablation drivers.

Lives in the library rather than in ``scripts/`` so that both the CLI and the
thin wrapper scripts call exactly the same code path -- a number quoted in the
report can only come from one place.

The ablation table is the report's central argument. Each row removes or replaces
one component, so the row's delta *is* that component's measured contribution.
Three rows exist specifically to test claims that would otherwise be assertions:

* rows 1-3 test whether hybrid retrieval beats either arm alone (it only can if
  BM25 and dense fail on disjoint queries),
* row 5 tests whether intent-conditioned source weighting does anything,
* row 9 -- uniform chunking -- tests whether per-source chunking was worth
  implementing at all.
"""

from __future__ import annotations

from typing import Any

from .config import Config
from .evaluation import (
    EvaluationReport,
    bootstrap_ci,
    evaluate_query,
    load_gold,
)
from .pipeline import Pipeline

__all__ = ["run_evaluation", "run_ablations", "evaluate_config", "ABLATIONS"]


def evaluate_config(config: Config, *, label: str | None = None) -> EvaluationReport:
    """Score the gold set under one configuration."""
    gold = load_gold(config.path("gold_file"))
    pipeline = Pipeline.build(config)
    corpus_chunks = pipeline.store.all_chunks
    report = EvaluationReport(config_name=label or str(config.get("experiment.name", "default")))

    for item in gold:
        # Traces are suppressed during evaluation so a sweep does not write
        # thousands of records; `cli query` is the path that logs.
        result = pipeline.query(item.query, write_trace=False)
        report.scores.append(
            evaluate_query(
                gold=item,
                pool=result.pool,
                final=result.final_chunks,
                resolutions=result.resolutions,
                findings=result.findings,
                answer_text=result.answer.text,
                refused=result.answer.refused,
                latency_ms=result.latency_ms,
                corpus_chunks=corpus_chunks,
            )
        )
    return report


def run_evaluation(config: Config, *, per_query: bool = False) -> int:
    report = evaluate_config(config)
    summary = report.summary()

    print("=" * 78)
    print(f"EVALUATION  config={summary['config']}  queries={summary['queries']}")
    print("=" * 78)

    groups = [
        ("Retrieval", ["ndcg@10", "recall@5", "recall@10", "pool_recall@20", "mrr"]),
        ("Multi-source", ["source_recall", "source_precision"]),
        ("Reranking", ["mean_gold_displacement", "promotion_rate_top3"]),
        (
            "Contradiction",
            ["conflict_detection_rate", "conflict_false_positive_rate"],
        ),
        ("Answer", ["refusal_accuracy", "answer_contains_pass", "answer_excludes_pass"]),
        ("Latency", ["p50_latency_ms", "p95_latency_ms"]),
    ]
    for group, keys in groups:
        print(f"\n{group}")
        for key in keys:
            print(f"  {key:30} {summary[key]}")

    scored = [s for s in report.scores if not s.refusal_expected]
    lo, hi = bootstrap_ci(
        [s.ndcg10 for s in scored], samples=int(config.get("evaluation.bootstrap_samples", 1000))
    )
    print(f"\n  ndcg@10 95% CI                 [{lo}, {hi}]  (n={len(scored)} -- wide by construction)")

    if per_query:
        print("\n" + "-" * 78)
        print(f"{'qid':5} {'nDCG':>6} {'MRR':>6} {'srcR':>5} {'was':>4} {'now':>4} "
              f"{'conflict':20} {'ok':>3}")
        print("-" * 78)
        for s in report.scores:
            flags = ("R" if s.refused else " ") + ("!" if s.false_positive_conflict else " ")
            ok = "OK" if (s.contains_ok and s.excludes_ok and s.refusal_correct) else "--"
            print(
                f"{s.qid:5} {s.ndcg10:6.3f} {s.mrr:6.3f} {s.source_recall:5.2f} "
                f"{s.gold_rank_before:4} {s.gold_rank_after:4} "
                f"{(s.conflict_relationship or '-'):20} {ok:>3} {flags}"
            )
    return 0


#: ``(label, override dict)`` -- each row of the README ablation table.
ABLATIONS: list[tuple[str, dict[str, Any]]] = [
    ("1_bm25_only", {"index.dense.enabled": False, "rerank.enabled": False,
                     "retrieval.query_analysis.expand_queries": False}),
    ("2_dense_only", {"index.bm25.enabled": False, "rerank.enabled": False,
                      "retrieval.query_analysis.expand_queries": False}),
    ("3_hybrid_rrf", {"rerank.enabled": False, "retrieval.query_analysis.expand_queries": False,
                      "chunking.contextual.enabled": False}),
    ("4_plus_context_headers", {"rerank.enabled": False,
                                "retrieval.query_analysis.expand_queries": False}),
    ("5_plus_expansion", {"rerank.enabled": False}),
    ("6_plus_rerank", {"rerank.mmr.enabled": False, "rerank.caps.max_per_source": 8}),
    ("7_plus_mmr_and_caps", {}),
    ("8_rerank_only_alpha1", {"rerank.alpha": 1.0}),
    ("9_control_uniform_chunking", {"chunking.strategy": "uniform"}),
]


def run_ablations(config: Config, *, alpha_sweep: bool = False) -> int:
    """Run every ablation row, then optionally the alpha sweep."""
    rows: list[dict[str, Any]] = []

    print("=" * 108)
    print("ABLATION STUDY")
    print("=" * 108)
    header = (
        f"{'#  configuration':34} {'nDCG@10':>8} {'R@5':>6} {'R@10':>6} {'MRR':>6} "
        f"{'poolR':>6} {'srcR':>6} {'confl':>6} {'FP':>5} {'p50ms':>7}"
    )
    print(header)
    print("-" * 108)

    for label, overrides in ABLATIONS:
        variant = config.variant(label, overrides)
        report = evaluate_config(variant, label=label)
        s = report.summary()
        rows.append(s)
        print(
            f"{label:34} {s['ndcg@10']:8.4f} {s['recall@5']:6.3f} {s['recall@10']:6.3f} "
            f"{s['mrr']:6.3f} {s['pool_recall@20']:6.3f} {s['source_recall']:6.3f} "
            f"{s['conflict_detection_rate']:6.3f} {s['conflict_false_positive_rate']:5.2f} "
            f"{s['p50_latency_ms']:7.1f}"
        )

    print("-" * 108)
    base = next((r for r in rows if r["config"] == "7_plus_mmr_and_caps"), None)
    control = next((r for r in rows if r["config"] == "9_control_uniform_chunking"), None)
    if base and control:
        delta = base["ndcg@10"] - control["ndcg@10"]
        print(
            f"\nPer-source chunking vs uniform control: nDCG@10 "
            f"{control['ndcg@10']:.4f} -> {base['ndcg@10']:.4f}  (delta {delta:+.4f})"
        )

    if alpha_sweep:
        print("\n" + "=" * 60)
        print("ALPHA SWEEP  (0.0 = weighting only, 1.0 = reranker only)")
        print("=" * 60)
        print(f"{'alpha':>6} {'nDCG@10':>9} {'MRR':>7} {'srcR':>7}")
        print("-" * 60)
        for i in range(11):
            alpha = i / 10.0
            variant = config.variant(f"alpha_{alpha:.1f}", {"rerank.alpha": alpha})
            s = evaluate_config(variant, label=f"alpha={alpha:.1f}").summary()
            print(f"{alpha:6.1f} {s['ndcg@10']:9.4f} {s['mrr']:7.3f} {s['source_recall']:7.3f}")

    return 0
