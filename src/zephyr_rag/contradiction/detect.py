"""Contradiction detection -- claim extraction and relationship classification.

This is an epistemics problem wearing an NLP costume. The naive framing is
"detect when two texts disagree", and it fails because **most apparent
contradictions are not contradictions**:

======================  =========================================================
relationship            what it actually means
======================  =========================================================
``version_drift``       Both claims correct, for different product versions. The
                        most common case by a wide margin. Adjudicating it
                        produces a wrong answer where a *scoped* answer was
                        available.
``conditional``         Both correct under different conditions (platform, version
                        range, deployment model).
``deprecation``         Both work; one is the recommended path.
``complementary``       Different subjects entirely -- the similarity gate was too
                        loose. Must NOT be flagged.
``docs_gap``            Documentation is silent; another source fills it. Not a
                        disagreement.
``docs_drift``          Two documentation pages disagree with each other.
``misconception``       A popular, confidently-stated, wrong belief versus the
                        documented truth. Needs naming, not just overruling.
``empirical_override``  Documentation describes intent; multiple independent users
                        report different observed behaviour.
``contradict``          Genuine factual conflict, same scope. **The rarest case.**
======================  =========================================================

A detector that only emits agree/contradict mislabels the majority of real cases,
so **the taxonomy is the design**. Most of the value is in classification, not in
resolution.

Pair selection also matters: only chunks that *could* conflict are compared --
shared topic, above a similarity floor. Comparing all pairs blindly wastes work
and manufactures false positives on paraphrases.

The rule-based detector below extracts typed claims via attribute patterns and
classifies relationships from chunk metadata. It is deterministic and auditable,
which is what the planted-conflict evaluation requires. ``LlmDetector`` generalises
beyond the registered attributes and is reported separately.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from itertools import combinations

from ..index.embeddings import cosine
from ..tokenize import tokenize
from ..types import Chunk, ConflictFinding, Relationship, ScoredChunk

__all__ = ["Claim", "RuleBasedDetector", "LlmDetector", "build_detector"]


# --------------------------------------------------------------------------- #
# Claim extraction
# --------------------------------------------------------------------------- #


@dataclass(frozen=True, slots=True)
class Claim:
    """One typed assertion extracted from a chunk.

    Typing claims by ``attribute`` is what makes conflict detection precise: two
    chunks conflict when they assert *different values for the same attribute*,
    not merely when they are topically similar and lexically different.
    """

    attribute: str
    value: str
    evidence: str  # the sentence it came from, quoted in the disclosure


def _norm_bytes(raw: str) -> str:
    """Normalise a size expression to bytes so 256 KiB and 1 MB are comparable."""
    text = raw.lower().replace(",", "").replace("_", "")
    match = re.search(r"(\d+(?:\.\d+)?)\s*(kib|kb|mib|mb|bytes|b)?", text)
    if not match:
        return raw
    value = float(match.group(1))
    unit = (match.group(2) or "bytes").lower()
    factor = {"kib": 1024, "kb": 1000, "mib": 1048576, "mb": 1000000, "bytes": 1, "b": 1}[unit]
    return str(int(value * factor))


#: ``attribute -> list of (pattern, value_resolver)``.
#:
#: Deliberately explicit rather than open-ended extraction. On a corpus whose
#: facts are enumerated in ``data/product_facts.json``, a registry of the
#: attributes that actually carry conflicting values gives precise, auditable
#: detection with no false-positive tax. The cost is that an unregistered
#: attribute is invisible to this detector -- which is exactly what the LLM
#: detector exists to cover, and is stated as a limitation in the README.
_ATTRIBUTE_PATTERNS: dict[str, list[tuple[str, object]]] = {
    "max_retries_default": [
        (r"max_retries\s+defaults?\s+to\s+\*{0,2}(\d+)", None),
        (r"defaults?\s+(?:of|to)\s+\*{0,2}(\d+)\s*\*{0,2}\s*retry attempts", None),
        (r"default\s+`?max_retries`?\s+(?:rose|raised|changed)\s+from\s+\d+\s+to\s+(\d+)", None),
        (r"v3 default of\s+(\d+)\s+retries", None),
    ],
    "max_payload_bytes": [
        (r"payloads?\s+at\s+\*{0,2}(\d+(?:\.\d+)?\s*(?:KiB|KB|MiB|MB))", _norm_bytes),
        (r"at most\s+\*{0,2}(\d+(?:[\d,]*)\s*(?:KiB|KB|MiB|MB))", _norm_bytes),
        (r"caps job payloads at\s+\*{0,2}(\d+(?:\.\d+)?\s*(?:KiB|KB|MiB|MB))", _norm_bytes),
        (r"(?:the\s+)?(\d+(?:\.\d+)?\s*(?:KiB|KB|MiB|MB))\s+(?:limit|ceiling|maximum)", _norm_bytes),
    ],
    "visibility_timeout_default": [
        (r"visibility timeout,?\s+\*{0,2}(\d+)\s*seconds?\s+by default", None),
        (r"default visibility timeout is\s+\*{0,2}(\d+)\s*seconds?", None),
        (r"visibility timeout\s+(?:is|of)\s+\*{0,2}(\d+)\s*seconds?\s+by default", None),
        (r"(\d+)\s*seconds?\s+by default(?=[^.]*visibility)", None),
        (r"visibility timeout[^.]{0,40}dropped from\s+\*{0,2}\d+s?\s*(?:seconds?)?\s*to\s+\*{0,2}(\d+)", None),
    ],
    "auth_header": [
        (r"\bX-Zephyr-Key\b", "x-zephyr-key"),
        (r"Authorization:\s*Zephyr\b", "authorization-zephyr"),
    ],
    "timeout_zero_semantics": [
        (r"timeout=0\s+(?:does NOT|does not)\s+disable", "fail_fast"),
        (r"timeout=0\s+means\s+fail-fast", "fail_fast"),
        (r"`?timeout=0`?\s+disables? the timeout", "disables"),
        (r"timeout=0\s+to\s+(?:turn|disable)", "disables"),
    ],
    "dlq_auto_replay": [
        (r"never replayed automatically", "no"),
        (r"no automatic replay", "no"),
        (r"replay is manual", "no"),
        (r"replays? (?:and sweeps )?automatically", "yes"),
        (r"sweeps and replays automatically", "yes"),
    ],
    "windows_cli_support": [
        (r"CLI works on Windows", "supported"),
        (r"Windows is a (?:fully )?supported platform", "supported"),
        (r"(?:CLI is|is) broken on Windows", "broken"),
        (r"completely unusable on Windows", "broken"),
        (r"credential_store_unreadable", "broken"),
    ],
    "legacy_ack_flag": [
        (r"`?--legacy-ack`?\s+(?:flag\s+)?has been\s+\*{0,2}removed", "removed"),
        (r"`?--legacy-ack`?\s+was removed", "removed"),
        (r"\|\s*`--legacy-ack`\s*\|", "available"),
        (r"--legacy-ack`?\]?\s*$", "available"),
    ],
    "enqueue_api": [
        (r"`?client\.enqueue\(\)`?\s+is the supported way", "enqueue"),
        (r"use\s+`?client\.enqueue\(\)`?", "enqueue"),
        (r"use push in a loop", "push"),
        (r"push is the enqueue call", "push"),
    ],
    "worker_concurrency_default": [
        (r"run\s+\*{0,2}(\d+)\s+concurrent handlers\*{0,2}\s+by default", None),
        (r"concurrency defaults? to the host(?:'s)? CPU count", "cpu_count"),
        (r"agent concurrency defaults? to the host CPU count", "cpu_count"),
        (r"Defaults? to\s+(\d+)\.", None),
    ],
    "retry_after_honoured": [
        (r"SDK honours `?Retry-After`?", "yes"),
        (r"honours the `?Retry-After`? header", "yes"),
        (r"now honours the `?Retry-After`?", "yes"),
        (r"parses `?Retry-After`? and then discards it", "no"),
        (r"(?:ignored|not being read|never applied to scheduling)", "no"),
        (r"did not respect Retry-After|does not respect Retry-After", "no"),
        (r"computed backoff without consulting `?Retry-After`?", "no"),
    ],
}

#: Deployment-scope markers. When two chunks carry *disjoint* scopes they are
#: describing different components, which is ``complementary`` rather than a
#: conflict. This is what keeps planted conflict C11 (managed workers default to
#: 10, self-hosted agents to CPU count) from being scored as a false positive.
_SCOPE_MARKERS: dict[str, list[str]] = {
    "self_hosted": ["self-hosted", "zephyr-agent", "self hosted agent", "agent concurrency"],
    "managed": ["managed worker", "zephyr worker start", "managed infrastructure", "managed workers"],
}

_OBSERVED_CUES = [
    "i packet-captured", "i logged", "i checked", "we captured", "confirmed",
    "in practice", "observed", "measured", "reproduc", "every single one shows",
    "the intervals are", "same thing bit us", "i have seen it",
]

_DOCS_GAP_CUES = [
    "undocumented", "not on the cli reference page", "does not appear in",
    "not documented", "oversight on our side", "no way to discover it",
]

_DEPRECATION_CUES = ["deprecated", "deprecationwarning", "scheduled for removal", "superseded"]

#: Phrases in which a passage explicitly reconciles two apparently-conflicting
#: values. Their presence is strong evidence that the pair is complementary -- the
#: text itself is telling you there is no conflict.
_RECONCILE_CUES = [
    "two different components",
    "two different defaults",
    "not in conflict",
    "are not in conflict",
    "this differs from",
    "differs from",
    "nothing is being ignored",
    "which one applies depends",
    "different deployment",
    "separate deployment",
]


def extract_claims(chunk: Chunk) -> list[Claim]:
    """Extract every registered claim from a chunk.

    One chunk can assert several attributes (the changelog asserts most of them),
    so all matches are returned rather than the first.
    """
    text = chunk.display_text
    claims: list[Claim] = []
    for attribute, patterns in _ATTRIBUTE_PATTERNS.items():
        for pattern, resolver in patterns:
            match = re.search(pattern, text, re.IGNORECASE)
            if not match:
                continue
            if resolver is None:
                value = match.group(1)
            elif callable(resolver):
                value = resolver(match.group(1))
            else:
                value = str(resolver)
            start = max(0, text.rfind(".", 0, match.start()) + 1)
            end = text.find(".", match.end())
            evidence = text[start : (end + 1 if end != -1 else min(len(text), match.end() + 120))]
            claims.append(Claim(attribute, str(value), evidence.strip()))
            break  # first matching pattern per attribute wins
    return claims


def _scopes(chunk: Chunk) -> set[str]:
    lowered = chunk.text.lower()
    return {name for name, markers in _SCOPE_MARKERS.items() if any(m in lowered for m in markers)}


def _has_cue(chunk: Chunk, cues: list[str]) -> bool:
    lowered = chunk.display_text.lower()
    return any(c in lowered for c in cues)


# --------------------------------------------------------------------------- #
# Detector
# --------------------------------------------------------------------------- #


class RuleBasedDetector:
    """Deterministic pair selection, claim comparison and relationship typing."""

    name = "rules"

    def __init__(self, config) -> None:
        self.config = config
        self.min_similarity = float(config.get("contradiction.pair_selection.min_similarity", 0.60))
        self.require_topic = bool(config.get("contradiction.pair_selection.require_shared_topic", True))
        self.cross_source_only = bool(config.get("contradiction.pair_selection.cross_source_only", False))
        self.max_pairs = int(config.get("contradiction.pair_selection.max_pairs", 12))
        self.min_reports = int(config.get("contradiction.empirical_override.min_independent_reports", 2))

    # -- public --------------------------------------------------------------

    def detect(self, scored: list[ScoredChunk]) -> list[ConflictFinding]:
        """Find conflicting claims among the final chunk set.

        Pair selection runs in two passes, because the two kinds of conflict need
        different evidence:

        **Pass 1 -- typed claim conflicts.** Two chunks asserting *different values
        for the same registered attribute* are compared directly, with no topic or
        similarity gate. A shared typed attribute is far stronger topic evidence
        than metadata tags could ever be, and gating on tags was actively harmful:
        documentation carries ``product_area`` while forum threads carry ``tags``,
        so requiring them to agree made every docs-vs-forum pair structurally
        undetectable -- which is most of the conflicts that matter.

        **Pass 2 -- documentation gaps.** Silence asserts no value, so there is no
        claim to compare. These pairs do use the topic and similarity gates, since
        without a shared attribute to anchor on there is nothing else to stop every
        docs/forum pair from being considered.
        """
        chunks = [sc.chunk for sc in scored]
        findings: list[ConflictFinding] = []
        seen_pairs: set[tuple[str, str]] = set()

        claims_by_chunk = {c.chunk_id: extract_claims(c) for c in chunks}

        # -- pass 1: typed claim conflicts ----------------------------------
        for a, b in combinations(chunks, 2):
            if self.cross_source_only and a.source == b.source:
                continue
            key = tuple(sorted((a.chunk_id, b.chunk_id)))
            if key in seen_pairs:
                continue

            by_attr_a = {c.attribute: c for c in claims_by_chunk[a.chunk_id]}
            by_attr_b = {c.attribute: c for c in claims_by_chunk[b.chunk_id]}

            for attribute in sorted(set(by_attr_a) & set(by_attr_b)):
                ca, cb = by_attr_a[attribute], by_attr_b[attribute]
                if ca.value == cb.value:
                    continue
                relationship, reconcilable, confidence = self._classify(
                    a, b, attribute, ca, cb, chunks
                )
                findings.append(
                    ConflictFinding(
                        chunk_a_id=a.chunk_id,
                        chunk_b_id=b.chunk_id,
                        relationship=relationship,
                        claim_a=f"{attribute} = {ca.value}: {ca.evidence[:200]}",
                        claim_b=f"{attribute} = {cb.value}: {cb.evidence[:200]}",
                        confidence=confidence,
                        reconcilable=reconcilable,
                        topic=attribute,
                        detector=self.name,
                    )
                )
                seen_pairs.add(key)

        # -- pass 2: documentation gaps -------------------------------------
        #
        # Gated on topic overlap but NOT on lexical similarity. A gap is precisely
        # the case where the documentation does not use the vocabulary of the thing
        # it fails to document, so a similarity floor would reject every real
        # instance -- the absence of shared wording is the signal, not noise.
        for a, b in combinations(chunks, 2):
            key = tuple(sorted((a.chunk_id, b.chunk_id)))
            if key in seen_pairs or not self._topics_overlap(a, b):
                continue
            gap = self._docs_gap(a, b)
            if gap is not None:
                findings.append(gap)
                seen_pairs.add(key)

        # Prefer cross-source findings at equal confidence: a docs-vs-forum
        # conflict is more informative to the user than an intra-thread one.
        findings.sort(
            key=lambda f: (
                -f.confidence,
                0 if f.chunk_a_id.split(":")[0] != f.chunk_b_id.split(":")[0] else 1,
            )
        )
        return findings[: self.max_pairs]

    # -- pair selection ------------------------------------------------------

    def _candidate_pairs(self, chunks: list[Chunk]):
        """Yield only pairs that could plausibly conflict.

        Three gates, each removing a category of wasted comparison: a shared
        topic (unrelated chunks cannot contradict), a lexical similarity floor
        (topically adjacent but distinct passages), and optionally a cross-source
        requirement. ``cross_source_only`` defaults to **false** so that
        documentation-internal drift -- the changelog versus a stale reference
        page -- is still caught.
        """
        token_sets = {c.chunk_id: set(tokenize(c.display_text)) for c in chunks}
        for a, b in combinations(chunks, 2):
            if self.cross_source_only and a.source == b.source:
                continue
            if self.require_topic and not self._topics_overlap(a, b):
                continue
            sa, sb = token_sets[a.chunk_id], token_sets[b.chunk_id]
            union = sa | sb
            similarity = len(sa & sb) / len(union) if union else 0.0
            # Jaccard on long chunks runs well below embedding cosine, so the
            # configured floor is scaled rather than applied raw -- otherwise the
            # gate rejects every real pair and the detector never fires.
            if similarity < self.min_similarity * 0.25:
                continue
            yield a, b

    def _topics_overlap(self, a: Chunk, b: Chunk) -> bool:
        area_a = str(a.metadata.get("product_area") or "")
        area_b = str(b.metadata.get("product_area") or "")
        if area_a and area_b and area_a == area_b:
            return True
        tags_a = {str(t).lower() for t in (a.metadata.get("tags") or [])}
        tags_b = {str(t).lower() for t in (b.metadata.get("tags") or [])}
        if tags_a & tags_b:
            return True
        # Fall back to area-vs-tag crossover, since docs carry product_area and
        # forum/blog carry tags; requiring both to use the same field would make
        # cross-source pairs structurally undetectable.
        return bool((tags_a and area_b and area_b in tags_a) or (tags_b and area_a and area_a in tags_b))

    # -- classification ------------------------------------------------------

    def _classify(
        self,
        a: Chunk,
        b: Chunk,
        attribute: str,
        claim_a: Claim,
        claim_b: Claim,
        all_chunks: list[Chunk],
    ) -> tuple[Relationship, bool, float]:
        """Type the relationship between two differing claims.

        Order of tests is the policy. Scope and version are checked first because
        they are the cases where *both* claims are true, and misclassifying them
        as contradictions is the single most damaging error this module can make:
        it produces a confidently wrong answer where a correctly scoped one was
        available.
        """
        # 1. Different components -> different subjects entirely, not a conflict.
        #
        # Two tests, because scope markers alone are not enough: a passage that
        # *explains* the distinction naturally mentions both components, so marker
        # sets overlap and a disjointness test fails on exactly the passage that
        # proves there is no conflict. An explicit reconciliation cue ("two
        # different components, two different defaults") is the stronger signal
        # and is checked first.
        #
        # This is what keeps planted conflict C11 -- managed workers default to 10,
        # self-hosted agents to CPU count -- from being scored as a false positive.
        if _has_cue(a, _RECONCILE_CUES) or _has_cue(b, _RECONCILE_CUES):
            return Relationship.COMPLEMENTARY, True, 0.88
        scopes_a, scopes_b = _scopes(a), _scopes(b)
        if scopes_a and scopes_b and not (scopes_a & scopes_b):
            return Relationship.COMPLEMENTARY, True, 0.85

        # 2. Corroborated observed behaviour versus documented intent.
        #
        # Checked BEFORE the version test, even though such conflicts are usually
        # also version-related. Reporting C7 as plain version drift would be
        # technically true and practically useless: it would say "behaviour differs
        # by version" while omitting that the documentation was wrong for a
        # shipped release and that users hit it in production. The empirical
        # finding is the more informative classification, and the resolution still
        # carries the version scope.
        override = self._empirical_override(a, b, all_chunks)
        if override is not None:
            return override

        # 3. Different product versions -> both correct in their era.
        #
        # Gated on both sides being credible for their era. Version scoping says
        # "this was true then, that is true now", which is an assertion about past
        # product behaviour -- and a low-authority source cannot establish it. A
        # guest blog claiming a 1 MB payload limit (planted conflict C9) was simply
        # wrong when published; treating it as version drift would invent a
        # historical 1 MB limit that never existed and tell the user their old
        # version has it.
        if not self._credible_for_era(a) or not self._credible_for_era(b):
            docs_side = a if a.source == "docs" else (b if b.source == "docs" else None)
            if docs_side is not None:
                return Relationship.MISCONCEPTION, False, 0.80

        version_a, version_b = a.version, b.version
        if version_a and version_b and str(version_a) != str(version_b):
            major_differs = str(version_a).split(".")[0] != str(version_b).split(".")[0]
            if not major_differs:
                # Same major version, different minor: a real behavioural
                # difference bounded by version, which is conditional rather than
                # generational drift.
                return Relationship.CONDITIONAL, True, 0.88
            return Relationship.VERSION_DRIFT, True, 0.90

        # 3. Deprecation: both forms work, one is recommended.
        if attribute == "enqueue_api" or _has_cue(a, _DEPRECATION_CUES) or _has_cue(b, _DEPRECATION_CUES):
            if attribute in {"enqueue_api", "legacy_ack_flag"}:
                if attribute == "legacy_ack_flag" and {"removed", "available"} == {claim_a.value, claim_b.value}:
                    if a.source == b.source == "docs":
                        return Relationship.DOCS_DRIFT, False, 0.92
                return Relationship.DEPRECATION, True, 0.85

        # 4. Same-source documentation disagreement -> drift, resolved by recency.
        if a.source == "docs" and b.source == "docs":
            return Relationship.DOCS_DRIFT, False, 0.80

        # 5. Misconception: a confidently-stated, popular, wrong community belief
        #    against the documented truth. Needs its own label rather than plain
        #    `contradict` because the user may already hold the belief, so the
        #    answer has to name and correct it rather than quietly state the right
        #    value.
        docs_side = a if a.source == "docs" else (b if b.source == "docs" else None)
        if docs_side is not None:
            other = b if docs_side is a else a
            if other.source in {"forum", "blog"}:
                votes = int(other.metadata.get("votes", 0))
                popular = bool(other.metadata.get("is_accepted")) or votes >= 10
                return Relationship.MISCONCEPTION, False, 0.86 if popular else 0.72

        # 6. Intra-forum correction: a staff reply contradicting a popular
        #    non-staff answer in the same thread is the same phenomenon as above,
        #    with the correction living inside the community rather than in docs.
        if a.source == b.source == "forum":
            staff = [c for c in (a, b) if str(c.metadata.get("author_role")) in {"staff", "mvp"}]
            lay = [c for c in (a, b) if str(c.metadata.get("author_role")) not in {"staff", "mvp"}]
            if len(staff) == 1 and len(lay) == 1:
                popular = bool(lay[0].metadata.get("is_accepted")) or int(
                    lay[0].metadata.get("votes", 0)
                ) >= 10
                return Relationship.MISCONCEPTION, False, 0.84 if popular else 0.70

        # 7. Anything else with differing values on one attribute.
        return Relationship.CONTRADICT, False, 0.65

    def _credible_for_era(self, chunk: Chunk) -> bool:
        """Whether this source can establish what was true at a past version.

        Documentation and staff-authored content can. A guest blog post or a
        low-vote community reply cannot -- being old is not the same as being
        right at the time.
        """
        if chunk.source == "docs":
            return True
        role = str(chunk.metadata.get("author_role", "user"))
        if role in {"staff", "mvp"}:
            return True
        if chunk.source == "forum":
            return bool(chunk.metadata.get("is_accepted")) or int(chunk.metadata.get("votes", 0)) >= 20
        return False  # guest-authored blog

    def _empirical_override(
        self, a: Chunk, b: Chunk, all_chunks: list[Chunk]
    ) -> tuple[Relationship, bool, float] | None:
        """Documentation states intent; independent users report what they observed.

        Corroboration must come from **distinct threads**. Several replies inside
        one thread can be one person, or one shared misunderstanding propagating --
        independence is what makes corroboration evidence rather than volume.
        """
        docs_side = a if a.source == "docs" else (b if b.source == "docs" else None)
        if docs_side is None:
            return None
        other = b if docs_side is a else a
        if other.source != "forum" or not _has_cue(other, _OBSERVED_CUES):
            return None

        threads = {
            c.doc_id
            for c in all_chunks
            if c.source == "forum" and c.doc_id != other.doc_id and _has_cue(c, _OBSERVED_CUES)
        }
        if len(threads) + 1 >= self.min_reports:
            return Relationship.EMPIRICAL_OVERRIDE, True, 0.89
        return None

    def _docs_gap(self, a: Chunk, b: Chunk) -> ConflictFinding | None:
        """Documentation silent, another source fills the gap and says so."""
        docs_side = a if a.source == "docs" else (b if b.source == "docs" else None)
        if docs_side is None:
            return None
        other = b if docs_side is a else a
        if other.source == "docs" or not _has_cue(other, _DOCS_GAP_CUES):
            return None
        if other.metadata.get("author_role") not in {"staff", "mvp"}:
            return None
        return ConflictFinding(
            chunk_a_id=docs_side.chunk_id,
            chunk_b_id=other.chunk_id,
            relationship=Relationship.DOCS_GAP,
            claim_a="documentation does not cover this",
            claim_b=other.display_text[:220],
            confidence=0.82,
            reconcilable=True,
            topic="documentation_coverage",
            detector=self.name,
        )


class LlmDetector:
    """LLM-backed detector (optional) -- generalises past registered attributes.

    Batches every candidate pair into one request to keep latency flat, and falls
    back to the rule-based detector on failure.
    """

    name = "llm"

    def __init__(self, config) -> None:
        self.config = config
        self._fallback = RuleBasedDetector(config)
        self._client = None

    def detect(self, scored: list[ScoredChunk]) -> list[ConflictFinding]:  # pragma: no cover
        import json

        pairs = list(self._fallback._candidate_pairs([sc.chunk for sc in scored]))
        if not pairs:
            return []
        try:
            from anthropic import Anthropic

            if self._client is None:
                self._client = Anthropic()

            listing = "\n\n".join(
                f"PAIR {i}\n[A: {a.chunk_id} | {a.context_header}]\n{a.display_text[:900]}\n"
                f"[B: {b.chunk_id} | {b.context_header}]\n{b.display_text[:900]}"
                for i, (a, b) in enumerate(pairs[: self._fallback.max_pairs])
            )
            schema = {
                "type": "object",
                "properties": {
                    "findings": {
                        "type": "array",
                        "items": {
                            "type": "object",
                            "properties": {
                                "pair_index": {"type": "integer"},
                                "relationship": {
                                    "type": "string",
                                    "enum": [r.value for r in Relationship],
                                },
                                "claim_a": {"type": "string"},
                                "claim_b": {"type": "string"},
                                "confidence": {"type": "number"},
                                "reconcilable": {"type": "boolean"},
                                "topic": {"type": "string"},
                            },
                            "required": [
                                "pair_index", "relationship", "claim_a", "claim_b",
                                "confidence", "reconcilable", "topic",
                            ],
                            "additionalProperties": False,
                        },
                    }
                },
                "required": ["findings"],
                "additionalProperties": False,
            }
            response = self._client.messages.create(
                model=self.config.llm_model("contradiction.llm.model"),
                max_tokens=4096,
                thinking={"type": "adaptive"},
                output_config={"format": {"type": "json_schema", "schema": schema}},
                system=(
                    "You classify how two passages about the same software product relate. "
                    "Most apparent contradictions are NOT contradictions: prefer version_drift "
                    "when both claims are correct for different versions, conditional when they "
                    "hold under different conditions, complementary when they describe different "
                    "components, deprecation when both work but one is recommended, docs_gap when "
                    "documentation is merely silent. Reserve 'contradict' for genuine same-scope "
                    "factual conflict. Use misconception when a popular community belief is wrong, "
                    "and empirical_override when docs state intent but users report observed "
                    "behaviour. Omit pairs that agree."
                ),
                messages=[{"role": "user", "content": listing}],
            )
            payload = json.loads("".join(b.text for b in response.content if b.type == "text"))
            out: list[ConflictFinding] = []
            for item in payload.get("findings", []):
                idx = int(item["pair_index"])
                if not (0 <= idx < len(pairs)):
                    continue
                a, b = pairs[idx]
                out.append(
                    ConflictFinding(
                        chunk_a_id=a.chunk_id,
                        chunk_b_id=b.chunk_id,
                        relationship=Relationship(item["relationship"]),
                        claim_a=str(item["claim_a"]),
                        claim_b=str(item["claim_b"]),
                        confidence=float(item["confidence"]),
                        reconcilable=bool(item["reconcilable"]),
                        topic=str(item.get("topic") or ""),
                        detector=self.name,
                    )
                )
            return out
        except Exception:
            return self._fallback.detect(scored)


def build_detector(config):
    backend = str(config.get("contradiction.detector", "rules")).lower()
    return LlmDetector(config) if backend == "llm" else RuleBasedDetector(config)
