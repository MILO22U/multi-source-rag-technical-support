#!/usr/bin/env python
"""Run the interactive chat app.

    python scripts/app.py
    python scripts/app.py --port 8080 --no-browser
    python scripts/app.py --set rerank.alpha=1.0      # serve any experiment config

Builds the indices once, then serves a local chat UI at http://127.0.0.1:8000/.
Stdlib only -- no Flask, no node, no API key.
"""
import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from zephyr_rag.config import load_config  # noqa: E402
from zephyr_rag.webapp import serve  # noqa: E402

if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=8000)
    ap.add_argument("--no-browser", action="store_true")
    ap.add_argument("--config", default=None)
    ap.add_argument("--set", dest="overrides", action="append", default=[], metavar="KEY=VALUE")
    args = ap.parse_args()

    config = load_config(args.config, args.overrides)
    raise SystemExit(serve(config, host=args.host, port=args.port, open_browser=not args.no_browser))
