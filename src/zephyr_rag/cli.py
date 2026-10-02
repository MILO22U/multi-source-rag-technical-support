"""Command-line interface.

    python -m zephyr_rag.cli serve
    python -m zephyr_rag.cli ingest  --dump-chunks
    python -m zephyr_rag.cli query   "why do jobs retry forever on 429?" --explain
    python -m zephyr_rag.cli search  "429 retry" --source forum
    python -m zephyr_rag.cli evaluate
    python -m zephyr_rag.cli ablate

Every subcommand accepts ``--config`` and repeatable ``--set key.path=value``, so
any experiment is reproducible from its command line alone.
"""

from __future__ import annotations

import argparse
import sys

from .config import load_config


def _utf8_stdout() -> None:
    """Force UTF-8 output.

    Windows consoles default to cp1252, where any non-Latin-1 character in a
    corpus passage raises UnicodeEncodeError and takes down the whole command.
    Reconfiguring with ``errors="replace"`` keeps output readable instead.
    """
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[union-attr]
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[union-attr]
    except AttributeError:  # pragma: no cover - very old interpreters
        pass


def _common(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--config", default=None, help="path to a YAML config")
    parser.add_argument(
        "--set",
        dest="overrides",
        action="append",
        default=[],
        metavar="KEY=VALUE",
        help="override a config value by dotted path (repeatable)",
    )


def main(argv: list[str] | None = None) -> int:
    _utf8_stdout()

    parser = argparse.ArgumentParser(prog="zephyr-rag", description="Multi-source RAG for technical support")
    sub = parser.add_subparsers(dest="command", required=True)

    p_ingest = sub.add_parser("ingest", help="load corpus, chunk, build indices")
    p_ingest.add_argument("--dump-chunks", action="store_true", help="write logs/chunks.jsonl")
    _common(p_ingest)

    p_query = sub.add_parser("query", help="answer one question")
    p_query.add_argument("question", nargs="+")
    p_query.add_argument("--explain", action="store_true", help="print the full decision trace")
    p_query.add_argument("--no-log", action="store_true", help="do not append to the trace log")
    _common(p_query)

    p_search = sub.add_parser("search", help="raw index lookup (BM25 vs dense, side by side)")
    p_search.add_argument("question", nargs="+")
    p_search.add_argument("--source", default=None, choices=["docs", "forum", "blog"])
    p_search.add_argument("-k", type=int, default=5)
    _common(p_search)

    p_eval = sub.add_parser("evaluate", help="score the gold set")
    p_eval.add_argument("--per-query", action="store_true")
    _common(p_eval)

    p_serve = sub.add_parser("serve", help="run the interactive chat app in a browser")
    p_serve.add_argument("--host", default="127.0.0.1")
    p_serve.add_argument("--port", type=int, default=8000)
    p_serve.add_argument("--no-browser", action="store_true")
    _common(p_serve)

    p_ablate = sub.add_parser("ablate", help="run the ablation study")
    p_ablate.add_argument("--alpha-sweep", action="store_true")
    _common(p_ablate)

    args = parser.parse_args(argv)
    config = load_config(args.config, args.overrides)

    if args.command == "ingest":
        return _cmd_ingest(config, args)
    if args.command == "query":
        return _cmd_query(config, args)
    if args.command == "search":
        return _cmd_search(config, args)
    if args.command == "evaluate":
        from .scripts_support import run_evaluation

        return run_evaluation(config, per_query=args.per_query)
    if args.command == "serve":
        from .webapp import serve

        return serve(config, host=args.host, port=args.port, open_browser=not args.no_browser)
    if args.command == "ablate":
        from .scripts_support import run_ablations

        return run_ablations(config, alpha_sweep=args.alpha_sweep)
    return 1


def _cmd_ingest(config, args) -> int:
    from .pipeline import Pipeline

    pipeline = Pipeline.build(config, dump_chunks=args.dump_chunks)
    counts = pipeline.chunk_counts()
    print(f"chunks indexed: {counts}  total={sum(counts.values())}")
    if args.dump_chunks:
        print(f"chunk dump:     {config.path('chunk_dump')}")
    return 0


def _cmd_query(config, args) -> int:
    from .pipeline import Pipeline

    question = " ".join(args.question)
    pipeline = Pipeline.build(config)
    result = pipeline.query(question, write_trace=not args.no_log)

    if args.explain:
        print(result.trace.explain())
        print()
    print(result.text)
    return 0


def _cmd_search(config, args) -> int:
    """Raw index comparison -- the fastest way to see hybrid retrieval working.

    BM25 should win on literal identifiers and the dense arm on paraphrases. If
    both return identical lists, the dense backend is misconfigured.
    """
    from .chunking import chunk_corpus
    from .index import build_store
    from .ingest.loaders import load_corpus

    query = " ".join(args.question)
    store = build_store(chunk_corpus(load_corpus(config), config), config)
    sources = [args.source] if args.source else ["docs", "forum", "blog"]

    for source in sources:
        index = store.indices.get(source)
        if index is None or not len(index):
            continue
        print(f"\n--- {source} ({len(index)} chunks)")
        print("  BM25")
        for chunk, score, rank in index.search_bm25(query, args.k):
            print(f"    {rank}. {score:7.3f}  {chunk.chunk_id}")
        print("  DENSE")
        for chunk, score, rank in index.search_dense(query, args.k):
            print(f"    {rank}. {score:7.3f}  {chunk.chunk_id}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
