"""Corpus loaders -- turn the three on-disk formats into uniform documents.

Each source has a different physical layout, chosen to match how that source is
naturally shaped:

* **docs**  -- one markdown file per page with a JSON frontmatter block. Markdown
  is kept because the docs chunker splits on the real heading tree, which is the
  structure an author deliberately created.
* **forum** -- a single JSON file of threads with nested posts, because a thread
  is a graph of posts, not a document.
* **blog**  -- a single JSON file of posts with the body as one prose string,
  because narrative prose has no structure worth preserving separately.

Loaders normalise all three into :class:`RawDocument`, which the chunkers
consume. Frontmatter/metadata is passed through untouched: the authority and
recency scorers and the contradiction precedence policy all read it later, and
dropping a field here silently disables a downstream signal.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterator

from ..types import Source

__all__ = ["RawDocument", "load_docs", "load_forum", "load_blog", "load_corpus"]


@dataclass(frozen=True, slots=True)
class RawDocument:
    """One pre-chunking unit: a docs page, a forum thread, or a blog post."""

    doc_id: str
    source: Source
    title: str
    body: str
    metadata: dict[str, Any] = field(default_factory=dict)
    #: Forum threads only -- the post list, kept structured for QA pairing.
    posts: list[dict[str, Any]] = field(default_factory=list)


# --------------------------------------------------------------------------- #
# Frontmatter
# --------------------------------------------------------------------------- #


def parse_frontmatter(text: str) -> tuple[dict[str, Any], str]:
    """Split a ``---``-delimited JSON frontmatter block from a markdown body.

    JSON rather than YAML frontmatter so that metadata is machine-checkable with
    the standard library and cannot acquire YAML's ambiguities (``3.10`` parsing
    as a float, bare ``no`` becoming ``False``) -- both of which would corrupt
    version strings, the one field this corpus most depends on.

    Returns:
        ``(metadata, body)``. Missing or malformed frontmatter yields ``({}, text)``
        rather than raising, so a corpus file is never silently skipped.
    """
    stripped = text.lstrip()
    if not stripped.startswith("---"):
        return {}, text

    lines = stripped.splitlines()
    closing = next((i for i, line in enumerate(lines[1:], start=1) if line.strip() == "---"), None)
    if closing is None:
        return {}, text

    raw = "\n".join(lines[1:closing]).strip()
    body = "\n".join(lines[closing + 1 :]).lstrip("\n")
    try:
        meta = json.loads(raw) if raw else {}
    except json.JSONDecodeError:
        return {}, text
    return (meta if isinstance(meta, dict) else {}), body


# --------------------------------------------------------------------------- #
# Per-source loaders
# --------------------------------------------------------------------------- #


def load_docs(docs_dir: str | Path) -> list[RawDocument]:
    """Load documentation pages from a directory of markdown files.

    ``doc_id`` comes from the frontmatter when present and falls back to the
    filename stem. It must be stable: chunk ids embed it, and the gold set
    references chunk ids.
    """
    directory = Path(docs_dir)
    if not directory.is_dir():
        raise FileNotFoundError(f"docs directory not found: {directory}")

    out: list[RawDocument] = []
    for path in sorted(directory.glob("*.md")):
        text = path.read_text(encoding="utf-8")
        meta, body = parse_frontmatter(text)
        doc_id = str(meta.get("doc_id") or path.stem)
        out.append(
            RawDocument(
                doc_id=doc_id,
                source="docs",
                title=str(meta.get("title") or doc_id.replace("-", " ").title()),
                body=body,
                metadata={**meta, "doc_id": doc_id, "source_file": path.name},
            )
        )
    return out


def load_forum(forum_file: str | Path) -> list[RawDocument]:
    """Load forum threads.

    The thread body is the opening post; ``posts`` carries the full structured
    list so the chunker can pair the question with each answer separately. That
    separation matters: merging answers into one chunk would make competing
    answers unrankable and would hide disagreement inside a chunk, where the
    pairwise contradiction detector cannot reach it.
    """
    path = Path(forum_file)
    if not path.exists():
        raise FileNotFoundError(f"forum file not found: {path}")

    payload = json.loads(path.read_text(encoding="utf-8"))
    out: list[RawDocument] = []
    for thread in payload.get("threads", []):
        posts = thread.get("posts", [])
        opening = next((p for p in posts if p.get("parent_post_id") is None), posts[0] if posts else {})
        meta = {k: v for k, v in thread.items() if k != "posts"}
        out.append(
            RawDocument(
                doc_id=str(thread["thread_id"]),
                source="forum",
                title=str(thread.get("title", "")),
                body=str(opening.get("body", "")),
                metadata=meta,
                posts=posts,
            )
        )
    return out


def load_blog(blog_file: str | Path) -> list[RawDocument]:
    """Load blog posts.

    ``published_at`` and ``product_version_at_time`` are mandatory in practice:
    a blog post is a snapshot of a moment, and a chunk that loses its date
    becomes a timeless claim that the contradiction resolver has no basis to
    discount. A missing date is surfaced as a warning rather than silently
    defaulted, because defaulting it would fabricate recency.
    """
    path = Path(blog_file)
    if not path.exists():
        raise FileNotFoundError(f"blog file not found: {path}")

    payload = json.loads(path.read_text(encoding="utf-8"))
    out: list[RawDocument] = []
    for post in payload.get("posts", []):
        meta = {k: v for k, v in post.items() if k != "body"}
        if not meta.get("published_at"):
            import warnings

            warnings.warn(f"blog post {post.get('post_id')!r} has no published_at", stacklevel=2)
        out.append(
            RawDocument(
                doc_id=str(post["post_id"]),
                source="blog",
                title=str(post.get("title", "")),
                body=str(post.get("body", "")),
                metadata=meta,
            )
        )
    return out


def load_corpus(config) -> dict[str, list[RawDocument]]:
    """Load all three sources, keyed by source name.

    Returns a dict rather than a flat list because every downstream stage --
    indexing, retrieval, the per-source candidate floor -- operates per source.
    Flattening here would mean regrouping everywhere else.
    """
    return {
        "docs": load_docs(config.path("docs_dir")),
        "forum": load_forum(config.path("forum_file")),
        "blog": load_blog(config.path("blog_file")),
    }


def iter_documents(corpus: dict[str, list[RawDocument]]) -> Iterator[RawDocument]:
    for source in ("docs", "forum", "blog"):
        yield from corpus.get(source, [])
