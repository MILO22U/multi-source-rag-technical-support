"""End-to-end pipeline orchestration.

Wires the six stages together and produces one trace record per query::

    analyse -> retrieve (hybrid, per-source, weighted)
            -> rerank (cross-encoder, blend, MMR, caps)
            -> detect & resolve conflicts
            -> synthesise with disclosure
            -> log

Indices are built once in :meth:`build` and reused, because per-query index
construction would dominate latency and make the timing columns in the report
meaningless.
"""

from __future__ import annotations

import json
from pathlib import Path

from .chunking import chunk_corpus
from .config import Config, load_config
from .contradiction import ContradictionEngine
from .generate.synthesize import build_synthesizer
from .index import IndexStore, build_store
from .ingest.loaders import load_corpus
from .observability.trace import QueryTrace, TraceWriter
from .rerank import Reranker
from .retrieval import HybridRetriever, build_analyzer
from .tokenize import tokenize
from .types import Answer, ScoredChunk

__all__ = ["Pipeline", "PipelineResult"]


class PipelineResult:
    """Answer, trace, and the intermediate artefacts the evaluator needs.

    ``pool`` is exposed deliberately: stage-1 recall is the hard ceiling on the
    whole system, so the evaluator has to measure the candidate pool separately
    from the final set. Returning only the final chunks would make a recall
    failure indistinguishable from a ranking failure.
    """

    def __init__(
        self,
        answer: Answer,
        trace: QueryTrace,
        final_chunks: list[ScoredChunk],
        pool: list[ScoredChunk] | None = None,
        resolutions: list | None = None,
        findings: list | None = None,
    ) -> None:
        self.answer = answer
        self.trace = trace
        self.final_chunks = final_chunks
        self.pool = pool or []
        self.resolutions = resolutions or []
        self.findings = findings or []

    @property
    def text(self) -> str:
        return self.answer.text

    @property
    def latency_ms(self) -> float:
        """Sum of stage timings.

        Computed from the stage marks rather than read from ``timing_ms["total"]``:
        that key is only populated by ``to_dict()``, so evaluation runs (which
        suppress trace writing) measured a flat 0.0 ms.
        """
        return round(
            sum(v for k, v in self.trace.timing_ms.items() if k != "total"), 2
        )


class Pipeline:
    """The full multi-source RAG system."""

    def __init__(self, config: Config, store: IndexStore) -> None:
        self.config = config
        self.store = store
        self.analyzer = build_analyzer(config)
        self.retriever = HybridRetriever(store, config)
        self.reranker = Reranker(config)
        self.contradiction = ContradictionEngine(config)
        self.synthesizer = build_synthesizer(config)
        # The synthesizer needs the corpus vocabulary to tell 'we found little'
        # apart from 'this topic is not in the knowledge base' -- see
        # ExtractiveSynthesizer._unknown_ratio.
        from collections import Counter

        corpus_df: Counter[str] = Counter()
        all_chunks = store.all_chunks
        for chunk in all_chunks:
            corpus_df.update(set(tokenize(chunk.text)))
        for target in (self.synthesizer, getattr(self.synthesizer, "_fallback", None)):
            if target is not None and hasattr(target, "corpus_vocab"):
                target.corpus_vocab = set(corpus_df)
                target.corpus_df = dict(corpus_df)
                target.corpus_chunks = len(all_chunks)
        self.writer = TraceWriter(
            config.path("log_file"), enabled=bool(config.get("logging.enabled", True))
        )

    # -- construction --------------------------------------------------------

    @classmethod
    def build(
        cls,
        config: Config | None = None,
        *,
        dump_chunks: bool = False,
    ) -> "Pipeline":
        """Load the corpus, chunk it, build indices, and assemble the pipeline."""
        config = config or load_config()
        corpus = load_corpus(config)
        chunks = chunk_corpus(corpus, config)

        if dump_chunks:
            path = Path(config.path("chunk_dump"))
            path.parent.mkdir(parents=True, exist_ok=True)
            with path.open("w", encoding="utf-8") as fh:
                for source in ("docs", "forum", "blog"):
                    for chunk in chunks.get(source, []):
                        fh.write(
                            json.dumps(
                                {
                                    "chunk_id": chunk.chunk_id,
                                    "source": chunk.source,
                                    "tokens": chunk.token_count,
                                    "context_header": chunk.context_header,
                                    "parent_id": chunk.parent_id,
                                    "chunker": chunk.metadata.get("chunker"),
                                    "version": chunk.version,
                                    "display_text": chunk.display_text,
                                },
                                ensure_ascii=False,
                            )
                            + "\n"
                        )

        store = build_store(chunks, config)
        return cls(config, store)

    # -- query ---------------------------------------------------------------

    def query(self, question: str, *, write_trace: bool = True) -> PipelineResult:
        trace = QueryTrace(query=question, config_name=str(self.config.get("experiment.name", "default")))

        trace.start("analysis")
        analysis = self.analyzer.analyze(question)
        trace.stop("analysis")
        trace.record_analysis(analysis)

        trace.start("retrieve")
        retrieved = self.retriever.retrieve(question, analysis)
        trace.stop("retrieve")
        trace.record_retrieval(retrieved.per_source_stats, retrieved.candidates, retrieved.weights_applied)

        trace.start("rerank")
        final, rerank_stats = self.reranker.rerank(question, retrieved.candidates, analysis)
        trace.stop("rerank")
        trace.record_rerank(rerank_stats)
        trace.record_final(final)

        trace.start("contradiction")
        resolutions, findings = self.contradiction.analyse(final, analysis)
        trace.stop("contradiction")
        trace.record_conflicts(resolutions, findings)

        trace.start("generate")
        answer = self.synthesizer.synthesize(question, final, resolutions)
        trace.stop("generate")
        trace.record_answer(answer)

        if write_trace:
            self.writer.write(trace)
        return PipelineResult(
            answer,
            trace,
            final,
            pool=retrieved.candidates,
            resolutions=resolutions,
            findings=findings,
        )

    # -- introspection -------------------------------------------------------

    def chunk_counts(self) -> dict[str, int]:
        return self.store.counts()
