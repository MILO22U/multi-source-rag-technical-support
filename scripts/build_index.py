#!/usr/bin/env python
"""Load the corpus, chunk it per source, and build the indices.

    python scripts/build_index.py --config config/default.yaml --dump-chunks

Inspecting logs/chunks.jsonl by hand after this step is strongly recommended:
structural chunking bugs are invisible once text has been embedded.
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from zephyr_rag.cli import main  # noqa: E402

if __name__ == "__main__":
    raise SystemExit(main(["ingest", *sys.argv[1:]]))
