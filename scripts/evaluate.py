#!/usr/bin/env python
"""Score the gold set and print retrieval, reranking and conflict metrics.

    python scripts/evaluate.py --config config/default.yaml --per-query
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from zephyr_rag.cli import main  # noqa: E402

if __name__ == "__main__":
    raise SystemExit(main(["evaluate", *sys.argv[1:]]))
