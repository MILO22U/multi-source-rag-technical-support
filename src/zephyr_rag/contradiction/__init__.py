"""Contradiction detection, resolution and disclosure (Requirement 5).

Four phases, kept separate because each is independently testable:

``detect``    -> which pairs relate, and how (the taxonomy)
``resolve``   -> which side to believe, and under which rule (the cascade)
``disclose``  -> what to tell the user (never a silent average)
``report``    -> what to log (which rule fired, on which pair)
"""

from __future__ import annotations

from ..types import ConflictFinding, QueryAnalysis, Relationship, Resolution, ScoredChunk
from .detect import Claim, LlmDetector, RuleBasedDetector, build_detector, extract_claims
from .policy import PrecedencePolicy

__all__ = [
    "Claim",
    "extract_claims",
    "RuleBasedDetector",
    "LlmDetector",
    "build_detector",
    "PrecedencePolicy",
    "ContradictionEngine",
]


class ContradictionEngine:
    """Detect, then resolve, returning resolutions ready for disclosure."""

    def __init__(self, config) -> None:
        self.config = config
        self.enabled = bool(config.get("contradiction.enabled", True))
        self.detector = build_detector(config) if self.enabled else None
        self.policy = PrecedencePolicy(config)

    #: Maps a claim attribute to the ``product_area`` it belongs to, so a conflict
    #: can be tested for relevance to the question actually asked.
    ATTRIBUTE_AREA: dict[str, set[str]] = {
        "max_retries_default": {"retries"},
        "backoff_strategy": {"retries"},
        "max_payload_bytes": {"payloads"},
        "visibility_timeout_default": {"concurrency", "timeouts", "idempotency"},
        "auth_header": {"auth", "migration"},
        "timeout_zero_semantics": {"timeouts"},
        "dlq_auto_replay": {"dlq"},
        "windows_cli_support": {"cli"},
        "legacy_ack_flag": {"cli", "migration"},
        "enqueue_api": {"sdk", "batch", "migration"},
        "worker_concurrency_default": {"concurrency"},
        "retry_after_honoured": {"rate_limits", "retries", "dlq"},
        "documentation_coverage": set(),  # judged by chunk rank instead
    }

    def analyse(
        self,
        scored: list[ScoredChunk],
        analysis: QueryAnalysis | None = None,
    ) -> tuple[list[Resolution], list[ConflictFinding]]:
        """Return ``(resolutions, all_findings)``.

        ``all_findings`` includes non-conflicts (``agree``, ``complementary``,
        ``refines``) so the false-positive rate is measurable: a detector that
        flags every paraphrase scores well on recall and is useless in practice,
        so both sides have to be visible in the evaluation.

        Findings are gated on relevance to the question before becoming
        resolutions. The final chunk set contains eight passages that are all
        topically adjacent, so the detector legitimately finds conflicts the user
        did not ask about -- a question about payload size would otherwise come
        back with a disclosure about the ``--legacy-ack`` CLI flag. **An
        irrelevant disclosure is noise, not honesty**: it dilutes the one warning
        that mattered and trains the reader to skip the section.
        """
        if not self.enabled or self.detector is None or len(scored) < 2:
            return [], []

        by_id = {sc.chunk.chunk_id: sc.chunk for sc in scored}
        rank = {sc.chunk.chunk_id: sc.rank_after for sc in scored}
        findings = self.detector.detect(scored)

        resolutions: list[Resolution] = []
        seen: set[tuple[tuple[str, str], str]] = set()

        for finding in findings:
            if not finding.relationship.is_conflict:
                continue
            # Collapse duplicates: the same pair can surface through several
            # attribute patterns, and repeating one disclosure three times reads
            # as a bug.
            key = (finding.pair_key, str(finding.topic))
            if key in seen:
                continue
            a = by_id.get(finding.chunk_a_id)
            b = by_id.get(finding.chunk_b_id)
            if a is None or b is None:
                continue
            if not self._relevant_to_query(finding, analysis, rank):
                continue
            seen.add(key)
            resolutions.append(self.policy.resolve(finding, a, b))

        return resolutions, findings

    def _relevant_to_query(
        self,
        finding: ConflictFinding,
        analysis: QueryAnalysis | None,
        rank: dict[str, int],
    ) -> bool:
        """Whether this conflict bears on the question that was asked.

        Three independent ways to qualify, because the signals are imperfect
        individually: the attribute's product area matches the query's, the
        attribute name shares vocabulary with the query, or both conflicting
        chunks rank in the top three (in which case the conflict is central to
        whatever was retrieved, whatever the topic labels say).
        """
        if analysis is None:
            return True

        topic = str(finding.topic or "")
        areas = self.ATTRIBUTE_AREA.get(topic)

        if areas and analysis.product_area and analysis.product_area in areas:
            return True

        from ..tokenize import tokenize

        query_tokens = set(tokenize(analysis.query))
        topic_tokens = set(tokenize(topic.replace("_", " ")))
        if topic_tokens & query_tokens:
            return True

        ra = rank.get(finding.chunk_a_id, 99)
        rb = rank.get(finding.chunk_b_id, 99)

        # A documentation gap has no shared attribute to match on -- the whole
        # point is that one side is silent -- so it qualifies when the side that
        # *does* carry the information ranks highly. Requiring both chunks in the
        # top three would reject the gap precisely when the gap-filling answer
        # won the query, which is the case worth disclosing.
        if topic == "documentation_coverage":
            return min(ra, rb) <= 3

        return ra <= 3 and rb <= 3
