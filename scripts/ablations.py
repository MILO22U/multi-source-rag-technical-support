#!/usr/bin/env python
"""Run the full ablation study, and optionally the alpha sweep.

    python scripts/ablations.py --config config/default.yaml --alpha-sweep

Each row isolates one component's marginal contribution; row 9 is the
uniform-chunking control that tests whether per-source chunking was worth it.
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from zephyr_rag.cli import main  # noqa: E402

if __name__ == "__main__":
    raise SystemExit(main(["ablate", *sys.argv[1:]]))
