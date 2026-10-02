#!/usr/bin/env python
"""Answer one question, optionally printing the full decision trace.

    python scripts/query.py --query "why do jobs retry forever on 429?" --explain
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from zephyr_rag.cli import main  # noqa: E402

if __name__ == "__main__":
    argv = sys.argv[1:]
    if "--query" in argv:
        i = argv.index("--query")
        argv = argv[:i] + argv[i + 1 :]
    raise SystemExit(main(["query", *argv]))
