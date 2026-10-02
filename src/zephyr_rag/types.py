"""Core data types shared by every stage of the pipeline.

This module is deliberately logic-free: it defines the vocabulary that ingest,
chunking, indexing, retrieval, reranking, contradiction handling, generation and
tracing all speak. Keeping it dependency-free (stdlib only, no imports from
sibling modules) means it can never participate in an import cycle.

Design notes worth knowing before you change anything here:

* ``Chunk.chunk_id`` is **content-addressed, not ordinal** -- ``docs:retries:configuring-backoff``
  rather than ``docs:retries:7``. The evaluation gold set in ``eval/gold.json``
  references chunk ids directly, so ordinals would silently invalidate every
  label the moment a chunker changed. Slugs survive re-chunking.
* ``Chunk.text`` and ``Chunk.display_text`` are intentionally different. ``text``
  carries the context-header breadcrumb and is what gets embedded and indexed;
  ``display_text`` is the clean body and is what gets quoted back to the user.
  Collapsing them into one field makes the generator cite breadcrumbs as if they
  were prose.
* ``ScoredChunk`` records ``rank_before``/``rank_after``. Nearly every question
  worth asking about the reranker ("is it doing anything?", "was stage-1 recall
  good enough?") is answerable from that pair, and it cannot be reconstructed
  after the fact.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any, Literal

__all__ = [
    "Source",
    "Intent",
    "Relationship",
    "DocType",
    "AuthorRole",
    "Chunk",
    "ScoredChunk",
    "QueryAnalysis",
    "ConflictFinding",
    "Resolution",
    "Answer",
    "Citation",
]

# --------------------------------------------------------------------------- #
# Enumerations
# --------------------------------------------------------------------------- #

#: The three knowledge sources. A ``Literal`` rather than an enum because it is
#: used as a dict key in weight tables loaded straight from JSON config.
Source = Literal["docs", "forum", "blog"]

SOURCES: tuple[Source, ...] = ("docs", "forum", "blog")


class Intent(StrEnum):
    """What the user is actually trying to do.

    This drives source weighting (see ``retrieval/weights.py``). The whole point
    of classifying intent is that source reliability is *not* constant: docs win
    for reference lookups, forums win for troubleshooting a specific failure,
    blogs win for design rationale.
    """

    TROUBLESHOOTING = "troubleshooting"
    HOW_TO = "how_to"
    CONCEPTUAL = "conceptual"
    API_REFERENCE = "api_reference"
    VERSION_MIGRATION = "version_migration"
    OPINION = "opinion"
    UNKNOWN = "unknown"


class Relationship(StrEnum):
    """How two chunks relate to each other on a shared topic.

    A binary agree/contradict split mislabels the majority of real cases, which
    is why this taxonomy is wider. ``VERSION_DRIFT`` and ``CONDITIONAL`` in
    particular are *not* contradictions -- both claims are true within their own
    scope, and the correct response is to scope the answer rather than pick a
    winner.
    """

    AGREE = "agree"
    CONTRADICT = "contradict"
    COMPLEMENTARY = "complementary"
    REFINES = "refines"
    VERSION_DRIFT = "version_drift"
    CONDITIONAL = "conditional"
    DEPRECATION = "deprecation"
    MISCONCEPTION = "misconception"
    DOCS_DRIFT = "docs_drift"
    DOCS_GAP = "docs_gap"
    EMPIRICAL_OVERRIDE = "empirical_override"
    UNRELATED = "unrelated"

    @property
    def is_conflict(self) -> bool:
        """True when the relationship requires resolution and disclosure.

        ``AGREE``, ``COMPLEMENTARY``, ``REFINES`` and ``UNRELATED`` need neither.
        """
        return self in {
            Relationship.CONTRADICT,
            Relationship.VERSION_DRIFT,
            Relationship.CONDITIONAL,
            Relationship.DEPRECATION,
            Relationship.MISCONCEPTION,
            Relationship.DOCS_DRIFT,
            Relationship.DOCS_GAP,
            Relationship.EMPIRICAL_OVERRIDE,
        }


class DocType(StrEnum):
    """Documentation page kind. Feeds the docs authority prior."""

    REFERENCE = "reference"
    GUIDE = "guide"
    TUTORIAL = "tutorial"
    API = "api"
    CHANGELOG = "changelog"


class AuthorRole(StrEnum):
    """Who wrote it. Feeds the forum/blog authority signal."""

    STAFF = "staff"
    MVP = "mvp"
    USER = "user"
    GUEST = "guest"


# --------------------------------------------------------------------------- #
# Chunks
# --------------------------------------------------------------------------- #


@dataclass(frozen=True, slots=True)
class Chunk:
    """One retrievable unit of text.

    Attributes:
        chunk_id: Stable, content-addressed identifier of the form
            ``{source}:{doc_id}:{local_slug}``. Stable across re-chunking so the
            gold set stays valid.
        source: Which corpus this came from.
        text: Indexed/embedded form -- ``context_header`` followed by the body.
        display_text: Clean body, used for citation and display.
        context_header: Breadcrumb that repairs context severed by chunking,
            e.g. ``"Zephyr Docs v3.2 > Retries > Configuring backoff"``.
        metadata: Source-specific fields (version, dates, votes, author role,
            doc type, tags). Read by the authority/recency scorers and by the
            contradiction precedence policy.
        token_count: Approximate token length, used for budget arithmetic.
        parent_id: Enclosing section/thread, enabling small-to-big retrieval
            (retrieve the precise chunk, generate over its larger parent).
    """

    chunk_id: str
    source: Source
    text: str
    display_text: str
    context_header: str
    metadata: dict[str, Any] = field(default_factory=dict)
    token_count: int = 0
    parent_id: str | None = None

    # -- derived views -------------------------------------------------------

    @property
    def doc_id(self) -> str:
        """Document identifier without the source prefix or local slug."""
        parts = self.chunk_id.split(":")
        return parts[1] if len(parts) > 1 else self.chunk_id

    @property
    def doc_prefix(self) -> str:
        """``source:doc_id`` -- the key used for per-document result caps and
        for document-level gold labels."""
        parts = self.chunk_id.split(":")
        return ":".join(parts[:2]) if len(parts) > 1 else self.chunk_id

    def matches_label(self, label: str) -> bool:
        """Whether this chunk satisfies a gold-set label.

        Labels may be written at any granularity -- ``docs:retries`` (whole
        document) or ``docs:retries:configuring-backoff`` (one section) -- so
        matching is exact-or-prefix. The trailing ``":"`` guard stops
        ``docs:retries`` from matching a different document named
        ``docs:retries-advanced``.
        """
        return self.chunk_id == label or self.chunk_id.startswith(label + ":")

    @property
    def version(self) -> str | None:
        """Product version this chunk describes, if known.

        Central to contradiction handling: most apparent conflicts in this
        corpus are two correct claims about different versions.
        """
        meta = self.metadata
        for key in ("version", "product_version_mentioned", "product_version_at_time"):
            value = meta.get(key)
            if value:
                return str(value)
        return None


# --------------------------------------------------------------------------- #
# Scored results
# --------------------------------------------------------------------------- #


@dataclass(frozen=True, slots=True)
class ScoredChunk:
    """A chunk with the scores that put it where it is.

    Every score is retained rather than collapsed into one number, because the
    trace record needs to explain *why* a chunk ranked where it did, and the
    ablation study needs each component in isolation.

    Attributes:
        chunk: The chunk itself.
        retrieval_score: Post-fusion, post-weighting stage-1 score.
        ce_score: Cross-encoder relevance; ``None`` until reranking runs.
        final_score: Blend of ``ce_score`` and ``retrieval_score``.
        rank_before: 1-indexed position before reranking.
        rank_after: 1-indexed position after reranking; ``-1`` if not yet ranked.
        components: Raw per-signal values (bm25 rank, dense rank, rrf, authority,
            recency, source weight) kept for the trace and for debugging.
    """

    chunk: Chunk
    retrieval_score: float
    final_score: float
    ce_score: float | None = None
    rank_before: int = -1
    rank_after: int = -1
    components: dict[str, float] = field(default_factory=dict)

    @property
    def rank_delta(self) -> int:
        """Positions gained by reranking. Positive means promoted.

        This is the headline statistic for evaluating the reranker: if gold
        chunks show a near-zero mean delta, reranking is not earning its latency.
        """
        if self.rank_before < 0 or self.rank_after < 0:
            return 0
        return self.rank_before - self.rank_after

    def with_ranks(self, *, before: int = -1, after: int = -1) -> "ScoredChunk":
        """Return a copy with rank fields set (the dataclass is frozen)."""
        from dataclasses import replace

        return replace(
            self,
            rank_before=before if before >= 0 else self.rank_before,
            rank_after=after if after >= 0 else self.rank_after,
        )


# --------------------------------------------------------------------------- #
# Query understanding
# --------------------------------------------------------------------------- #


@dataclass(frozen=True, slots=True)
class QueryAnalysis:
    """Structured reading of the user's question.

    Produced before retrieval. Pays off three times: it selects the source
    weight profile, it supplies expansion queries for a second shot at
    vocabulary mismatch, and ``version_mentioned`` becomes a hard metadata
    filter rather than a soft scoring preference.
    """

    query: str
    intent: Intent = Intent.UNKNOWN
    product_area: str | None = None
    version_mentioned: str | None = None
    expanded_queries: list[str] = field(default_factory=list)
    entities: list[str] = field(default_factory=list)
    requires_canonical_answer: bool = False
    classifier: str = "rules"  # "rules" | "llm" -- recorded in the trace

    @property
    def all_queries(self) -> list[str]:
        """Original query first, then expansions, de-duplicated."""
        seen: set[str] = set()
        out: list[str] = []
        for q in [self.query, *self.expanded_queries]:
            key = q.strip().lower()
            if key and key not in seen:
                seen.add(key)
                out.append(q.strip())
        return out


# --------------------------------------------------------------------------- #
# Contradiction handling
# --------------------------------------------------------------------------- #


@dataclass(frozen=True, slots=True)
class ConflictFinding:
    """Output of the detector: how two chunks relate, and on what claims."""

    chunk_a_id: str
    chunk_b_id: str
    relationship: Relationship
    claim_a: str
    claim_b: str
    confidence: float = 0.0
    reconcilable: bool = True
    topic: str | None = None
    detector: str = "rules"  # "rules" | "llm"

    @property
    def pair_key(self) -> tuple[str, str]:
        """Order-independent identity, so A-vs-B and B-vs-A dedupe."""
        return tuple(sorted((self.chunk_a_id, self.chunk_b_id)))  # type: ignore[return-value]

    @property
    def is_conflict(self) -> bool:
        return self.relationship.is_conflict


@dataclass(frozen=True, slots=True)
class Resolution:
    """Output of the precedence policy: what to believe, and why.

    ``rule_fired`` is deliberately part of the record. Requirement 5 is only
    defensible if the resolution is explainable, and naming the rule makes the
    policy auditable from the trace alone.
    """

    finding: ConflictFinding
    winner: str  # a chunk_id, or "both", or "unresolved"
    rule_fired: str
    explanation: str
    must_disclose: bool = True
    loser: str | None = None

    @property
    def is_scoped(self) -> bool:
        """True when both claims stand within their own scope (version or
        platform) rather than one being wrong."""
        return self.winner == "both"


# --------------------------------------------------------------------------- #
# Generation output
# --------------------------------------------------------------------------- #


@dataclass(frozen=True, slots=True)
class Citation:
    """Link from a span of the answer back to the chunk that grounded it."""

    chunk_id: str
    source: Source
    cited_text: str
    sentence_index: int = -1


@dataclass(frozen=True, slots=True)
class Answer:
    """Final response plus everything needed to audit it."""

    query: str
    text: str
    citations: list[Citation] = field(default_factory=list)
    resolutions: list[Resolution] = field(default_factory=list)
    used_chunks: list[ScoredChunk] = field(default_factory=list)
    refused: bool = False
    generator: str = "extractive"  # "extractive" | "llm"

    @property
    def source_distribution(self) -> dict[str, int]:
        """How many final chunks came from each source.

        Logged per query (requirement 6) and asserted on in tests: a
        multi-source system that answers everything from one source has a
        weighting bug, not a preference.
        """
        dist: dict[str, int] = {}
        for sc in self.used_chunks:
            dist[sc.chunk.source] = dist.get(sc.chunk.source, 0) + 1
        return dist

    @property
    def disclosed_conflicts(self) -> list[Resolution]:
        return [r for r in self.resolutions if r.must_disclose]
