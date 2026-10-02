"""Local chat application for the multi-source RAG system.

    python scripts/app.py              # build indices, serve, open a browser
    zephyr-rag serve --port 8000

A chat UI is the honest interface for this system: the deliverable is an answer
a support engineer would act on, and the parts that matter -- which sources were
used, which conflicts fired, how far reranking moved the evidence -- are per-turn
facts that belong next to the message, not in a terminal scrollback.

Stdlib only (``http.server`` + ``json``), same as the rest of the pipeline, so
the app adds no dependency and no API key to the install.

Endpoints
---------
``GET  /``                 the chat UI (single self-contained HTML file)
``POST /api/ask``          {question} -> answer, citations, conflicts, trace
``GET  /api/meta``         chunk counts, config name, suggested questions
``GET  /api/chunk?id=``    full text of one chunk (citation drawer)
``POST /api/run``          {action: rebuild|evaluate|ablate} -> captured stdout
``GET  /api/log?n=``       recent trace records, newest first

The pipeline is built once at startup and shared; ``ThreadingHTTPServer`` serves
concurrently, and a lock serialises queries because the pipeline holds mutable
per-query trace state.
"""

from __future__ import annotations

import io
import json
import re
import threading
import time
import webbrowser
from contextlib import redirect_stdout
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlparse

from ..config import Config, load_config
from ..pipeline import Pipeline

__all__ = ["serve", "create_app_state"]

STATIC = Path(__file__).resolve().parent / "static"

# Shown as starter chips on an empty conversation. Each one exercises a
# different requirement, so clicking through them in order is a demo.
SUGGESTIONS = [
    {
        "q": "Why do my jobs retry forever when the API returns 429?",
        "tag": "intent weighting",
        "hint": "troubleshooting -> forum outranks docs",
    },
    {
        "q": "How do I see the payload of a dead-lettered job from the CLI?",
        "tag": "reranking",
        "hint": "the answer moves #20 -> #1",
    },
    {
        "q": "Can I set timeout=0 to disable the timeout?",
        "tag": "contradiction",
        "hint": "a 47-vote accepted answer is wrong",
    },
    {
        "q": "What is the default worker concurrency?",
        "tag": "not a conflict",
        "hint": "complementary -> no warning",
    },
    {
        "q": "Why does Zephyr use exponential backoff instead of fixed delays?",
        "tag": "conceptual",
        "hint": "the blog outranks the docs",
    },
    {
        "q": "How do I integrate Zephyr with Kubernetes CronJobs?",
        "tag": "refusal",
        "hint": "out of corpus -> refuse, do not improvise",
    },
]


# Conversational openers are not support questions. Routing "hi" through
# retrieval produces the out-of-scope refusal, which is technically correct and
# conversationally wrong: it tells the user their greeting is missing from the
# knowledge base. Handled in the app layer, never in the pipeline -- the
# evaluated system keeps exactly the behaviour the benchmarks measured.
_SMALL_TALK = {
    "hi", "hii", "hiya", "hey", "heya", "hello", "helo", "yo", "sup", "greetings",
    "good", "morning", "afternoon", "evening", "night",
    "thanks", "thank", "thx", "ty", "cheers", "appreciated",
    "ok", "okay", "cool", "nice", "great", "awesome", "lol", "haha",
    "bye", "goodbye", "cya", "later",
    "test", "testing", "ping", "hola", "namaste",
    "you", "u", "there", "how", "are", "doing", "whats", "up", "a", "the", "is", "it",
}

_SMALL_TALK_REPLY = (
    "Hi — I answer technical support questions about **Zephyr**, a (fictional) "
    "managed job-queue product, using three knowledge sources: the product "
    "documentation, the community forum, and the engineering blog.\n\n"
    "I only answer from those sources. If something is not in them I say so "
    "rather than guessing, and when the sources disagree I tell you that too.\n\n"
    "Try one of these, or ask your own:"
)


def _is_small_talk(question: str) -> bool:
    """True when the input is a greeting rather than a question to retrieve for.

    Deliberately conservative: every token must be in the small-talk vocabulary,
    so "hey, why do jobs retry forever?" still goes to retrieval. Anything
    carrying a digit or an identifier (``429``, ``timeout=0``) never matches.
    """
    tokens = [t for t in re.split(r"[^a-z0-9]+", question.lower()) if t]
    if not tokens or len(tokens) > 6:
        return False
    return all(t in _SMALL_TALK for t in tokens)


class AppState:
    """Shared, process-wide pipeline plus the lock that guards it."""

    def __init__(self, config: Config) -> None:
        self.config = config
        self.lock = threading.Lock()
        self.pipeline = Pipeline.build(config)
        self.built_at = time.strftime("%H:%M:%S")

    def rebuild(self) -> dict[str, int]:
        with self.lock:
            self.pipeline = Pipeline.build(self.config, dump_chunks=True)
            self.built_at = time.strftime("%H:%M:%S")
            return self.pipeline.chunk_counts()

    def ask(self, question: str) -> dict[str, Any]:
        if _is_small_talk(question):
            # No retrieval ran, so there is nothing to log and no trace to show.
            return {
                "kind": "greeting",
                "answer": _SMALL_TALK_REPLY,
                "suggestions": SUGGESTIONS[:4],
            }
        with self.lock:
            result = self.pipeline.query(question)
            store = self.pipeline.store
        return _render_turn(result, store)


def create_app_state(config: Config | None = None) -> AppState:
    return AppState(config or load_config())


# -- response shaping --------------------------------------------------------


def _source_label(chunk) -> str:
    """Human label for a badge: the thing a support engineer would cite."""
    meta = chunk.metadata or {}
    if chunk.source == "docs":
        title = meta.get("title") or chunk.chunk_id.split(":")[1].replace("-", " ").title()
        version = meta.get("version")
        return f"{title}" + (f" (v{version})" if version else "")
    if chunk.source == "forum":
        bits = [meta.get("author_role") or "user"]
        if meta.get("votes") is not None:
            bits.append(f"{meta['votes']} votes")
        if meta.get("accepted"):
            bits.append("accepted")
        title = meta.get("thread_title") or meta.get("title") or "Forum thread"
        return f"{title} — {', '.join(bits)}"
    title = meta.get("title") or "Engineering blog"
    date = meta.get("date") or meta.get("published")
    return f"{title}" + (f" ({date})" if date else "")


def _render_turn(result, store) -> dict[str, Any]:
    trace = result.trace
    doc = trace.to_dict()

    by_id = {sc.chunk.chunk_id: sc for sc in result.final_chunks}

    citations = []
    seen: set[str] = set()
    for cit in result.answer.citations:
        if cit.chunk_id in seen:
            continue
        seen.add(cit.chunk_id)
        sc = by_id.get(cit.chunk_id)
        chunk = sc.chunk if sc else store.get(cit.chunk_id)
        if chunk is None:
            continue
        citations.append(
            {
                "chunk_id": cit.chunk_id,
                "source": chunk.source if isinstance(chunk.source, str) else str(chunk.source),
                "label": _source_label(chunk),
                "quote": cit.cited_text,
                "rank_before": sc.rank_before if sc else None,
                "rank_after": sc.rank_after if sc else None,
                "ce_score": round(sc.ce_score, 3) if sc and sc.ce_score is not None else None,
            }
        )

    conflicts = []
    for res in result.resolutions:
        f = res.finding
        rel = f.relationship.value if hasattr(f.relationship, "value") else str(f.relationship)
        conflicts.append(
            {
                "relationship": rel,
                "topic": f.topic,
                "confidence": round(f.confidence, 2),
                "rule": res.rule_fired,
                "winner": res.winner,
                "loser": res.loser,
                "explanation": res.explanation,
                "claim_a": f.claim_a,
                "claim_b": f.claim_b,
                "chunk_a": f.chunk_a_id,
                "chunk_b": f.chunk_b_id,
                "disclosed": bool(res.must_disclose),
            }
        )

    # Complementary / agreeing pairs: the evidence for the zero false-positive
    # rate. Surfaced separately so the UI can show "considered, not a conflict".
    non_conflicts = []
    for f in result.findings:
        # Same predicate the trace uses: a finding is a non-conflict only if its
        # relationship says so. Filtering by "not in resolutions" instead would
        # relabel a detected contradiction as a clean pair whenever the cascade
        # declined to disclose it.
        if getattr(f.relationship, "is_conflict", False):
            continue
        rel = f.relationship.value if hasattr(f.relationship, "value") else str(f.relationship)
        non_conflicts.append(
            {
                "relationship": rel,
                "topic": f.topic,
                "chunk_a": f.chunk_a_id,
                "chunk_b": f.chunk_b_id,
            }
        )

    moves = [
        {
            "chunk_id": c["chunk_id"],
            "source": c["source"],
            "rank_before": c["rank_before"],
            "rank_after": c["rank_after"],
            "delta": c["rank_delta"],
            "retrieval_score": c["retrieval_score"],
            "ce_score": c["ce_score"],
            "final_score": c["final_score"],
        }
        for c in doc["final_chunks"]
    ]

    return {
        "kind": "answer",
        "trace_id": trace.trace_id,
        "answer": result.answer.text,
        "refused": bool(result.answer.refused),
        "generator": result.answer.generator,
        "intent": doc["analysis"]["intent"],
        "product_area": doc["analysis"]["product_area"],
        "version": doc["analysis"]["version_mentioned"],
        "classifier": doc["analysis"]["classifier"],
        "entities": doc["analysis"]["entities"],
        "weights": doc["weights_applied"],
        "pool": doc["retrieval"]["candidates_considered"],
        "pool_by_source": doc["retrieval"]["candidate_distribution"],
        "source_distribution": doc["source_distribution"],
        "rerank": doc["rerank"],
        "moves": moves,
        "citations": citations,
        "conflicts": conflicts,
        "non_conflicts": non_conflicts,
        "timing_ms": doc["timing_ms"],
        "latency_ms": result.latency_ms,
    }


# -- HTTP --------------------------------------------------------------------


class Handler(BaseHTTPRequestHandler):
    server_version = "zephyr-rag"
    state: AppState  # injected by serve()

    # -- plumbing ----------------------------------------------------------

    def log_message(self, fmt: str, *args) -> None:  # quieter console
        if self.path.startswith("/api/ask"):
            return
        super().log_message(fmt, *args)

    def _send(self, code: int, body: bytes, ctype: str) -> None:
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        try:
            self.wfile.write(body)
        except (BrokenPipeError, ConnectionAbortedError):
            pass  # browser navigated away mid-response; not an error worth raising

    def _json(self, payload: Any, code: int = 200) -> None:
        self._send(code, json.dumps(payload, ensure_ascii=False).encode("utf-8"), "application/json; charset=utf-8")

    def _read_json(self) -> dict[str, Any]:
        length = int(self.headers.get("Content-Length") or 0)
        if not length:
            return {}
        try:
            return json.loads(self.rfile.read(length).decode("utf-8"))
        except (ValueError, UnicodeDecodeError):
            return {}

    # -- routes ------------------------------------------------------------

    def do_GET(self) -> None:  # noqa: N802
        route = urlparse(self.path)
        params = parse_qs(route.query)

        if route.path in ("/", "/index.html"):
            html = (STATIC / "index.html").read_bytes()
            return self._send(200, html, "text/html; charset=utf-8")

        if route.path == "/api/meta":
            counts = self.state.pipeline.chunk_counts()
            return self._json(
                {
                    "counts": counts,
                    "total": sum(counts.values()),
                    "config": str(self.state.config.get("experiment.name", "default")),
                    "built_at": self.state.built_at,
                    "generator": str(self.state.config.get("generation.backend", "extractive")),
                    "suggestions": SUGGESTIONS,
                }
            )

        if route.path == "/api/chunk":
            chunk_id = (params.get("id") or [""])[0]
            chunk = self.state.pipeline.store.get(chunk_id)
            if chunk is None:
                return self._json({"error": f"no such chunk: {chunk_id}"}, 404)
            return self._json(
                {
                    "chunk_id": chunk.chunk_id,
                    "source": chunk.source if isinstance(chunk.source, str) else str(chunk.source),
                    "label": _source_label(chunk),
                    "context_header": chunk.context_header,
                    "text": chunk.display_text,
                    "tokens": chunk.token_count,
                    "metadata": {k: v for k, v in (chunk.metadata or {}).items() if not k.startswith("_")},
                }
            )

        if route.path == "/api/log":
            n = int((params.get("n") or ["10"])[0])
            path = Path(self.state.config.path("log_file"))
            if not path.exists():
                return self._json({"records": []})
            lines = [ln for ln in path.read_text(encoding="utf-8").splitlines() if ln.strip()]
            records = [json.loads(ln) for ln in lines[-n:]][::-1]
            return self._json({"records": records, "total": len(lines), "path": str(path)})

        return self._json({"error": "not found"}, 404)

    def do_POST(self) -> None:  # noqa: N802
        route = urlparse(self.path)
        payload = self._read_json()

        if route.path == "/api/ask":
            question = (payload.get("question") or "").strip()
            if not question:
                return self._json({"error": "empty question"}, 400)
            try:
                return self._json(self.state.ask(question))
            except Exception as exc:  # surface the failure in the UI, keep serving
                return self._json({"error": f"{type(exc).__name__}: {exc}"}, 500)

        if route.path == "/api/run":
            action = payload.get("action")
            try:
                return self._json(self._run_action(action))
            except Exception as exc:
                return self._json({"error": f"{type(exc).__name__}: {exc}"}, 500)

        return self._json({"error": "not found"}, 404)

    def _run_action(self, action: str | None) -> dict[str, Any]:
        """Run one maintenance/benchmark command in-process and capture stdout.

        In-process rather than a subprocess so the running pipeline's config
        (including any --set overrides this server was started with) is the one
        being measured.
        """
        started = time.perf_counter()

        if action == "rebuild":
            counts = self.state.rebuild()
            out = f"chunks indexed: {counts}  total={sum(counts.values())}"
        elif action in ("evaluate", "ablate"):
            from ..scripts_support import run_ablations, run_evaluation

            buf = io.StringIO()
            with redirect_stdout(buf):
                if action == "evaluate":
                    run_evaluation(self.state.config, per_query=True)
                else:
                    run_ablations(self.state.config, alpha_sweep=True)
            out = buf.getvalue()
        else:
            return {"error": f"unknown action: {action}"}

        return {"action": action, "output": out, "elapsed_s": round(time.perf_counter() - started, 2)}


class _Server(ThreadingHTTPServer):
    """Server that refuses to share a port.

    ``ThreadingHTTPServer`` sets ``allow_reuse_address``, which on Windows means
    SO_REUSEADDR lets a second process bind a port that is already serving --
    two pipelines then answer the same URL at random. Turning it off makes the
    collision an error the port walk below can handle.
    """

    allow_reuse_address = False
    daemon_threads = True


def serve(
    config: Config | None = None,
    *,
    host: str = "127.0.0.1",
    port: int = 8000,
    open_browser: bool = True,
) -> int:
    config = config or load_config()
    print("building indices ...")
    state = create_app_state(config)
    counts = state.pipeline.chunk_counts()
    print(f"chunks indexed: {counts}  total={sum(counts.values())}")

    handler = type("BoundHandler", (Handler,), {"state": state})

    # Walk forward a few ports: a half-closed socket from a previous run (or a
    # second copy of the app) should not cost the user a traceback.
    httpd = None
    for candidate in range(port, port + 10):
        try:
            httpd = _Server((host, candidate), handler)
            port = candidate
            break
        except OSError:
            continue
    if httpd is None:
        print(f"ports {port}-{port + 9} are all in use; pass --port with a free one")
        return 1

    url = f"http://{host}:{port}/"
    print(f"\n  Zephyr Support RAG  ->  {url}\n  Ctrl+C to stop\n")
    if open_browser:
        threading.Timer(0.6, lambda: webbrowser.open(url)).start()
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\nstopped")
    finally:
        httpd.server_close()
    return 0
