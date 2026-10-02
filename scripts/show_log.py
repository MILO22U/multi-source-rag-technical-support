"""Print the last record of logs/queries.jsonl in a readable, screen-share-friendly form.

    python scripts/show_log.py                   # last record
    python scripts/show_log.py -n 2              # second-to-last record
    python scripts/show_log.py --grep timeout    # last record whose query matches
    python scripts/show_log.py --raw             # the full JSON object, pretty-printed

Requirement 6 asks for logging that tracks which sources were used for each
response. The log itself is newline-delimited JSON (machine-readable, one record
per query); this script is the human view of a single record, used in the demo
video so the audit trail is legible on screen instead of a wrapped blob.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

LOG = Path(__file__).resolve().parents[1] / "logs" / "queries.jsonl"


def main() -> int:
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[union-attr]
    except AttributeError:  # pragma: no cover
        pass

    ap = argparse.ArgumentParser()
    ap.add_argument("-n", type=int, default=1, help="which record from the end (1 = last)")
    ap.add_argument("--grep", default=None, help="last record whose query contains this text (case-insensitive)")
    ap.add_argument("--raw", action="store_true", help="dump the whole record as pretty JSON")
    args = ap.parse_args()

    if not LOG.exists():
        print(f"no log yet at {LOG} -- run scripts/query.py first")
        return 1

    records = [json.loads(line) for line in LOG.read_text(encoding="utf-8").splitlines() if line.strip()]
    if not records:
        print("log is empty")
        return 1
    if args.grep:
        needle = args.grep.lower()
        records = [x for x in records if needle in x.get("query", "").lower()]
        if not records:
            print(f"no logged query contains {args.grep!r}")
            return 1
    if args.n > len(records):
        print(f"only {len(records)} matching records in the log")
        return 1

    r = records[-args.n]

    if args.raw:
        print(json.dumps(r, indent=2, ensure_ascii=False))
        return 0

    print("=" * 78)
    print(f"TRACE    {r['trace_id']}   {r['timestamp']}   config={r['config']}")
    print(f"QUERY    {r['query']}")
    print("-" * 78)
    a = r["analysis"]
    print(f"INTENT   {a['intent']}  area={a['product_area']}  version={a['version_mentioned']}  by={a['classifier']}")
    print(f"WEIGHTS  {r['weights_applied']}")
    print(f"POOL     {r['retrieval']['candidates_considered']} candidates {r['retrieval']['candidate_distribution']}")
    rr = r["rerank"]
    print(f"RERANK   alpha={rr['alpha']}  mean|delta|={rr['mean_abs_rank_delta']}  top1_changed={bool(rr['top1_changed'])}")
    print("-" * 78)
    print("SOURCES USED FOR THIS RESPONSE")
    for src, n in r["source_distribution"].items():
        print(f"  {src:<6} {n} of {len(r['final_chunks'])} final slots")
    print("-" * 78)
    print(f"{'#':>2}  {'was':>3} {'now':>3} {'ret':>6} {'ce':>6} {'final':>6}  {'auth':>4}  chunk")
    for i, c in enumerate(r["final_chunks"], 1):
        print(
            f"{i:>2}  {c['rank_before']:>3} {c['rank_after']:>3} "
            f"{c['retrieval_score']:>6.3f} {c['ce_score']:>6.3f} {c['final_score']:>6.3f} "
            f"{c['authority']:>5.2f}  {c['chunk_id']}"
        )
    print("-" * 78)
    conflicts = r.get("conflicts") or []
    print(f"CONFLICTS {len(conflicts)}")
    for c in conflicts:
        print(f"  [{c.get('relationship')}] {c.get('topic')}  conf={c.get('confidence')}")
        print(f"     rule={c.get('rule_fired')}  ->  winner={c.get('winner')}  (disclosed={c.get('disclosed')})")
    print(f"CITATIONS {len(r.get('citations') or [])}")
    for c in r.get("citations") or []:
        print(f"  {c.get('source','?'):<6} {c.get('chunk_id')}")
    print(f"REFUSED   {r['refused']}   generator={r['generator']}   timing_ms={r['timing_ms']}")
    print("=" * 78)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
