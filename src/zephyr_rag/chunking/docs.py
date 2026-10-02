"""Documentation chunker -- structure-aware splitting on the heading tree.

Documentation is the one source that arrives with *authored* structure. Headings
nest, sections are deliberately self-contained, and code blocks are atomic. A
character-count splitter discards all of that and cuts mid-sentence,
mid-code-block, mid-thought.

So this chunker walks the markdown heading tree and emits one chunk per leaf
section, then applies two repairs:

* **merge orphans** -- a section under ``min_tokens`` (a heading with one line of
  text under it) retrieves terribly on its own, so it folds into its sibling or
  parent.
* **split overflow** -- a section over ``max_tokens`` splits at paragraph
  boundaries, never inside a fenced code block.

Chunk ids are built from the heading slug path (``docs:retries-and-backoff:
configuring-backoff``) rather than an ordinal, so the evaluation gold set stays
valid when chunking parameters change.
"""

from __future__ import annotations

import re

from ..ingest.loaders import RawDocument
from ..tokenize import estimate_tokens
from ..types import Chunk
from .base import dedupe_slug, iter_fenced_blocks, make_chunk, slug_for, split_on_paragraphs
from .contextual import build_header

__all__ = ["DocsChunker"]

_HEADING_RE = re.compile(r"^(#{1,6})\s+(.*?)\s*#*\s*$")


class _Section:
    __slots__ = ("level", "title", "lines", "path")

    def __init__(self, level: int, title: str, path: list[str]) -> None:
        self.level = level
        self.title = title
        self.path = path
        self.lines: list[str] = []

    @property
    def body(self) -> str:
        return "\n".join(self.lines).strip()

    @property
    def tokens(self) -> int:
        return estimate_tokens(self.body)


class DocsChunker:
    """Split a documentation page on its heading structure."""

    source = "docs"

    def __init__(self, config) -> None:
        self.config = config
        self.min_tokens = int(config.get("chunking.docs.min_tokens", 120))
        self.max_tokens = int(config.get("chunking.docs.max_tokens", 800))
        self.overlap_sentences = int(config.get("chunking.docs.split_overlap_sentences", 1))
        self.heading_levels = set(config.get("chunking.docs.heading_levels", [2, 3]))

    # -- public --------------------------------------------------------------

    def chunk(self, doc: RawDocument) -> list[Chunk]:
        sections = self._parse_sections(doc.body)
        sections = self._merge_orphans(sections)

        chunks: list[Chunk] = []
        seen_slugs: set[str] = set()
        h2_chunk_ids: dict[str, str] = {}  # H2 title -> its first chunk id, for parent_id

        for section in sections:
            if not section.body:
                continue
            pieces = split_on_paragraphs(section.body, self.max_tokens, self.overlap_sentences)
            for i, piece in enumerate(pieces):
                slug = slug_for(section.title, fallback="body")
                if i:
                    slug = f"{slug}-p{i + 1}"
                slug = dedupe_slug(slug, seen_slugs)
                chunk_id = f"docs:{doc.doc_id}:{slug}"

                header = build_header(
                    "docs", self.config, metadata=doc.metadata, section_path=section.path
                )
                # parent_id points at the enclosing H2 so a later small-to-big
                # retrieval step can widen a precise hit into its full section.
                parent_key = section.path[0] if section.path else None
                parent_id = h2_chunk_ids.get(parent_key) if parent_key else None

                chunks.append(
                    make_chunk(
                        chunk_id=chunk_id,
                        source="docs",
                        body=piece,
                        context_header=header,
                        metadata={
                            **doc.metadata,
                            "section_title": section.title,
                            "section_path": list(section.path),
                            "heading_level": section.level,
                            "chunker": "heading_tree",
                        },
                        parent_id=parent_id if parent_id != chunk_id else None,
                    )
                )
                if section.level == 2 and parent_key and parent_key not in h2_chunk_ids:
                    h2_chunk_ids[parent_key] = chunk_id

        return chunks

    # -- internals -----------------------------------------------------------

    def _parse_sections(self, body: str) -> list[_Section]:
        """Walk the document once, maintaining a heading stack to build paths.

        Content before the first qualifying heading becomes a preamble section so
        that intro prose (often the clearest summary on the page) is not dropped.
        """
        lines = body.splitlines()
        code_mask = iter_fenced_blocks(lines)

        sections: list[_Section] = []
        stack: list[tuple[int, str]] = []  # (level, title)
        current = _Section(level=1, title="", path=[])

        for line, in_code in zip(lines, code_mask):
            match = None if in_code else _HEADING_RE.match(line)
            if not match:
                current.lines.append(line)
                continue

            hashes, title = match.groups()
            level = len(hashes)

            if level == 1:
                # Page title: starts the preamble, does not create a section path.
                if current.body:
                    sections.append(current)
                current = _Section(level=1, title="", path=[])
                stack = []
                continue

            if current.body or current.title:
                sections.append(current)

            while stack and stack[-1][0] >= level:
                stack.pop()
            stack.append((level, title))
            path = [t for _, t in stack]
            current = _Section(level=level, title=title, path=path)

        if current.body or current.title:
            sections.append(current)
        return [s for s in sections if s.body]

    def _merge_orphans(self, sections: list[_Section]) -> list[_Section]:
        """Fold sections below ``min_tokens`` into a neighbour.

        A lone heading with a sentence under it embeds as noise and retrieves as
        noise. Merging forward into the next sibling keeps the heading text
        (which is usually the most query-like string on the page) attached to
        enough body to be meaningful.
        """
        if not sections:
            return sections

        merged: list[_Section] = []
        for section in sections:
            if (
                merged
                and section.tokens < self.min_tokens
                and merged[-1].tokens + section.tokens <= self.max_tokens
            ):
                prev = merged[-1]
                prev.lines.append("")
                if section.title:
                    prev.lines.append(f"{'#' * section.level} {section.title}")
                prev.lines.extend(section.lines)
                continue
            merged.append(section)

        # A tiny trailing section with nowhere to merge forward attaches backward.
        if len(merged) > 1 and merged[-1].tokens < self.min_tokens:
            tail = merged.pop()
            prev = merged[-1]
            prev.lines.append("")
            if tail.title:
                prev.lines.append(f"{'#' * tail.level} {tail.title}")
            prev.lines.extend(tail.lines)

        return merged
