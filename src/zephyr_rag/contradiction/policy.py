"""Precedence policy -- deciding what to believe, and recording why.

The policy is a declarative cascade loaded from ``contradiction.precedence`` in
config rather than logic scattered through code, for three reasons: requirement 5
is only defensible if the resolution is *explainable*; a reviewer must be able to
read the policy without reading the implementation; and ``rule_fired`` in the
trace makes every decision auditable after the fact.

Cascade order, and why each rung sits where it does
---------------------------------------------------
1. ``version_scoping`` -- **first**, because it resolves the most common case at
   zero cost. If both claims are correct for different versions there is nothing
   to adjudicate; there is something to *explain*. Adjudicating here would
   produce a wrong answer where a scoped answer was available.
2. ``explicit_deprecation`` -- recommend the current path, acknowledge the old one
   still works. Not a factual conflict.
3. ``empirical_override`` -- documentation describes *intended* behaviour; users
   report *observed* behaviour. Several independent reports of observed behaviour
   outweigh silent deference to the docs. This rung is the difference between a
   thoughtful system and ``if source == "docs": win``.
4. ``source_authority`` -- for canonical facts (defaults, limits, signatures),
   where exactly one answer is right.
5. ``recency_within_tier`` -- a changelog beats a stale reference page.
6. ``unresolved`` -- present both, attributed, with no false confidence.

Rule 3 deserves the emphasis. If five independent users report that a flag does
not behave as documented, the probable explanation is a bug or documentation
drift -- not that five people independently hallucinated the same failure. The
correct response is not to silently flip the answer either: it is to **surface
the discrepancy**.
"""

from __future__ import annotations

from ..types import Chunk, ConflictFinding, Relationship, Resolution

__all__ = ["PrecedencePolicy"]


class PrecedencePolicy:
    """Evaluates the configured cascade against a finding."""

    def __init__(self, config) -> None:
        self.config = config
        self.cascade: list[str] = list(
            config.get("contradiction.precedence", [])
            or [
                "version_scoping",
                "explicit_deprecation",
                "empirical_override",
                "source_authority",
                "recency_within_tier",
                "unresolved",
            ]
        )
        self.authority_order: list[str] = list(
            config.get("contradiction.source_authority_order", []) or []
        )
        self.always_disclose_unresolved = bool(
            config.get("contradiction.disclosure.always_disclose_unresolved", True)
        )

    # -- public --------------------------------------------------------------

    def resolve(self, finding: ConflictFinding, a: Chunk, b: Chunk) -> Resolution:
        """Apply the cascade, returning the first rule that fires."""
        for rule in self.cascade:
            handler = getattr(self, f"_rule_{rule}", None)
            if handler is None:
                continue
            resolution = handler(finding, a, b)
            if resolution is not None:
                return resolution
        return self._rule_unresolved(finding, a, b)  # type: ignore[return-value]

    # -- rules ---------------------------------------------------------------

    def _rule_version_scoping(self, finding: ConflictFinding, a: Chunk, b: Chunk) -> Resolution | None:
        """Both claims correct within their own version or condition."""
        if finding.relationship not in {
            Relationship.VERSION_DRIFT,
            Relationship.CONDITIONAL,
            Relationship.COMPLEMENTARY,
        }:
            return None

        if finding.relationship == Relationship.COMPLEMENTARY:
            return Resolution(
                finding=finding,
                winner="both",
                rule_fired="complementary_scope",
                explanation=(
                    "These passages describe different components rather than disagreeing. "
                    "Both are correct within their own scope, so the answer states which "
                    "applies to which."
                ),
                must_disclose=False,
            )

        version_a, version_b = a.version, b.version
        newer = self._newer(a, b)
        older = b if newer is a else a
        kind = "version" if finding.relationship == Relationship.VERSION_DRIFT else "condition"
        return Resolution(
            finding=finding,
            winner="both",
            rule_fired="version_scoping",
            explanation=(
                f"Both claims are correct for their own {kind}: "
                f"v{version_a or 'unknown'} and v{version_b or 'unknown'} differ here. "
                f"The current behaviour is the one described for v{newer.version or 'current'}; "
                f"the other applies to v{older.version or 'earlier'}."
            ),
            must_disclose=True,
            loser=None,
        )

    def _rule_explicit_deprecation(
        self, finding: ConflictFinding, a: Chunk, b: Chunk
    ) -> Resolution | None:
        """One form is deprecated: recommend the current one, keep the old visible."""
        if finding.relationship is not Relationship.DEPRECATION:
            return None
        # Documentation defines what is current; if neither side is docs, prefer staff.
        winner = a if a.source == "docs" else (b if b.source == "docs" else self._more_authoritative(a, b))
        loser = b if winner is a else a
        return Resolution(
            finding=finding,
            winner=winner.chunk_id,
            rule_fired="explicit_deprecation",
            explanation=(
                "Not a factual conflict: both forms work, but one is deprecated. The answer "
                "recommends the current API and notes that the older form still functions, "
                "since it appears throughout older threads and posts."
            ),
            must_disclose=True,
            loser=loser.chunk_id,
        )

    def _rule_empirical_override(
        self, finding: ConflictFinding, a: Chunk, b: Chunk
    ) -> Resolution | None:
        """Corroborated observed behaviour versus documented intent."""
        if finding.relationship is not Relationship.EMPIRICAL_OVERRIDE:
            return None
        docs_side = a if a.source == "docs" else b
        reports = b if docs_side is a else a
        return Resolution(
            finding=finding,
            winner="disclose_both",
            rule_fired="empirical_override",
            explanation=(
                "The documentation describes intended behaviour; multiple independent user "
                "reports describe different observed behaviour. Independent corroboration of a "
                "discrepancy is evidence of a bug or documentation drift, not of user error, so "
                "the answer leads with the documented behaviour and explicitly surfaces the "
                "reported discrepancy as a known issue rather than hiding it."
            ),
            must_disclose=True,
            loser=None,
        )

    def _rule_source_authority(
        self, finding: ConflictFinding, a: Chunk, b: Chunk
    ) -> Resolution | None:
        """Canonical-fact conflicts resolve by source authority."""
        if finding.relationship not in {
            Relationship.CONTRADICT,
            Relationship.MISCONCEPTION,
            Relationship.DOCS_GAP,
        }:
            return None

        if finding.relationship is Relationship.DOCS_GAP:
            # Documentation is silent, so authority runs the other way: the source
            # that actually contains the information wins.
            filler = a if a.source != "docs" else b
            return Resolution(
                finding=finding,
                winner=filler.chunk_id,
                rule_fired="docs_gap_staff_authority",
                explanation=(
                    "The documentation does not cover this; a staff-authored community post "
                    "does. The answer uses it and flags that the behaviour is undocumented, so "
                    "the user knows it may change without a changelog entry."
                ),
                must_disclose=True,
                loser=None,
            )

        winner = self._more_authoritative(a, b)
        loser = b if winner is a else a
        misconception = finding.relationship is Relationship.MISCONCEPTION
        return Resolution(
            finding=finding,
            winner=winner.chunk_id,
            rule_fired="authority_canonical",
            explanation=(
                (
                    "A widely-held community answer conflicts with the documentation on a fact "
                    "that has one correct value. The documented behaviour wins, and the answer "
                    "names and corrects the misconception explicitly because the user may "
                    "already believe it."
                )
                if misconception
                else (
                    "Conflicting claims on a canonical fact. Resolved by source authority: "
                    f"{self._tier(winner)} outranks {self._tier(loser)} for defaults, limits "
                    "and signatures."
                )
            ),
            must_disclose=True,
            loser=loser.chunk_id,
        )

    def _rule_recency_within_tier(
        self, finding: ConflictFinding, a: Chunk, b: Chunk
    ) -> Resolution | None:
        """Same-tier disagreement (typically docs vs docs) resolves by recency."""
        if finding.relationship is not Relationship.DOCS_DRIFT:
            return None
        winner = self._newer(a, b)
        loser = b if winner is a else a
        changelog_wins = str(winner.metadata.get("doc_type")) == "changelog"
        return Resolution(
            finding=finding,
            winner=winner.chunk_id,
            rule_fired="recency_within_tier",
            explanation=(
                (
                    "Two documentation pages disagree. The changelog is authoritative on what "
                    "changed and when, so the reference page is stale here."
                )
                if changelog_wins
                else (
                    "Two pages from the same source tier disagree; the more recently updated "
                    "one wins."
                )
            ),
            must_disclose=True,
            loser=loser.chunk_id,
        )

    def _rule_unresolved(self, finding: ConflictFinding, a: Chunk, b: Chunk) -> Resolution:
        """No rule applies: present both without manufacturing confidence."""
        return Resolution(
            finding=finding,
            winner="unresolved",
            rule_fired="unresolved",
            explanation=(
                "No precedence rule resolves this conflict. Both claims are presented with "
                "their sources attributed, because asserting one would express a confidence "
                "the evidence does not support."
            ),
            must_disclose=self.always_disclose_unresolved,
        )

    # -- helpers -------------------------------------------------------------

    def _tier(self, chunk: Chunk) -> str:
        """Authority tier label, as used in ``contradiction.source_authority_order``."""
        if chunk.source == "docs":
            return "docs:changelog" if chunk.metadata.get("doc_type") == "changelog" else "docs"
        role = str(chunk.metadata.get("author_role", "user"))
        if chunk.source == "forum":
            if role in {"staff", "mvp"}:
                return "forum:staff"
            if chunk.metadata.get("is_accepted"):
                return "forum:accepted"
            return "forum:high_votes" if int(chunk.metadata.get("votes", 0)) >= 20 else "forum:low_votes"
        return "blog:staff" if role == "staff" else "blog:guest"

    def _rank(self, chunk: Chunk) -> int:
        tier = self._tier(chunk)
        try:
            return self.authority_order.index(tier)
        except ValueError:
            return len(self.authority_order)

    def _more_authoritative(self, a: Chunk, b: Chunk) -> Chunk:
        ra, rb = self._rank(a), self._rank(b)
        if ra != rb:
            return a if ra < rb else b
        return self._newer(a, b)

    def _newer(self, a: Chunk, b: Chunk) -> Chunk:
        """Later-dated chunk, falling back to higher version, then to ``a``."""
        from datetime import datetime

        def stamp(chunk: Chunk) -> tuple[int, tuple[int, ...]]:
            meta = chunk.metadata
            raw = meta.get("last_updated") or meta.get("published_at") or meta.get("created_at")
            epoch = 0
            if raw:
                try:
                    epoch = int(datetime.strptime(str(raw)[:10], "%Y-%m-%d").timestamp())
                except ValueError:
                    epoch = 0
            version = tuple(int(p) for p in str(chunk.version or "0").split(".") if p.isdigit())
            return epoch, version

        sa, sb = stamp(a), stamp(b)
        if sa[1] != sb[1]:
            return a if sa[1] > sb[1] else b
        if sa[0] != sb[0]:
            return a if sa[0] > sb[0] else b
        return a
