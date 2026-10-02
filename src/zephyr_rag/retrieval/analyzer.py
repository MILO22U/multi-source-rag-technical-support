"""Query understanding -- classify intent before retrieving anything.

This stage exists because of the central insight behind requirement 3: **source
reliability is not constant, it depends on what is being asked.**

* "What is the maximum payload size?" -- one correct answer, defined by the
  documentation. A forum guess is noise.
* "Why do my jobs retry forever on 429s?" -- lived experience of a specific
  broken configuration. The docs describe intended behaviour and often do not
  describe the failure mode actually hit; someone on the forum hit it at 2am and
  posted the fix. **Forums can outrank documentation here.**
* "Why exponential backoff instead of fixed delays?" -- design rationale. Docs
  say *what*, rarely *why*. Blogs exist to explain why.

So intent selects a weight profile (``retrieval.weights`` in config). One cheap
classification reshapes the entire downstream ranking, which is a lot of leverage
for very little work.

Two further products of this stage:

* **expansions** -- paraphrases give hybrid retrieval extra shots at vocabulary
  mismatch. They are weighted *below* the original query during fusion; see
  ``retrieval/hybrid.py`` for why unweighted expansion destroys precision.
* **version_mentioned** -- becomes a *hard metadata filter*, not a soft scoring
  preference. "I'm on v2.4" should exclude v3-only content rather than fight it
  on score. Hard constraints deserve hard tools.

The rule-based classifier is the default so results reproduce with no API key.
``retrieval.query_analysis.backend: llm`` swaps in a hosted LLM with structured
outputs; the interface is identical.
"""

from __future__ import annotations

import re

from ..types import Intent, QueryAnalysis

__all__ = ["QueryAnalyzer", "RuleBasedAnalyzer", "LlmAnalyzer", "build_analyzer"]


# Order is significant: the first matching group wins, so patterns are arranged
# most-specific first.
_INTENT_PATTERNS: list[tuple[Intent, list[str]]] = [
    (
        Intent.VERSION_MIGRATION,
        [
            r"\bmigrat",
            r"\bupgrad",
            r"\bv?2\.\d.*\bv?3\.\d",
            r"\bbreaking change",
            r"\bmoving (?:from|to)\b",
            r"\bafter upgrading\b",
            r"\bwhat breaks\b",
        ],
    ),
    # Conceptual is tested BEFORE troubleshooting deliberately. Both begin
    # "why does ...", so whichever is tested first wins. Design-rationale cues
    # ("instead of", "rationale", "trade-off") are far more specific than the
    # bare interrogative, so they take priority; troubleshooting then catches the
    # remaining "why" questions, which carry symptom cues instead.
    (
        Intent.CONCEPTUAL,
        [
            r"\binstead of\b",
            r"\brather than\b",
            r"\brationale\b",
            r"\btrade-?off",
            r"\bwhy (?:does|do|did) (?:zephyr|the|it|you)\b",
            r"\bhow does .{0,30}work\b",
            r"\bwhat is the point\b",
            r"\bexplain\b",
            r"\bunder the hood\b",
            r"\bdesign decision\b",
            r"\bwhy not\b",
        ],
    ),
    (
        Intent.OPINION,
        [
            r"\bbetter than\b",
            r"\bvs\.?\b",
            r"\bversus\b",
            r"\bshould i (?:use|pick|choose)\b",
            r"\bworth it\b",
            r"\brecommend\b",
            r"\bcompare",
        ],
    ),
    (
        Intent.TROUBLESHOOTING,
        [
            r"\bwhy (?:do|does|is|are|am|did)\b",
            r"\berror\b",
            r"\bfail",
            r"\bbroken\b",
            r"\bnot work",
            r"\bstuck\b",
            r"\bhang",
            r"\bforever\b",
            r"\btwice\b",
            r"\bduplicate",
            r"\bcrash",
            r"\b4\d{2}\b",
            r"\b5\d{2}\b",
            r"\bdisappear",
            r"\bunexpected",
            r"\bissue\b",
            r"\bproblem\b",
            r"\bkeeps?\b",
            r"\bgone\b",
            r"\bmissing\b",
            r"\bignor",
            r"\bwon'?t\b",
        ],
    ),
    (
        Intent.API_REFERENCE,
        [
            r"\bwhat(?:'s| is) the (?:default|maximum|max|minimum|min|limit)\b",
            r"\bhow many\b",
            r"\bsignature\b",
            r"\bparameter",
            r"\bwhich header\b",
            r"\bdefault value\b",
            r"\bmax(?:imum)? (?:size|number|payload|batch)\b",
            r"\bexit code",
            r"\bdefault (?:retry|retries|concurrency|timeout)\b",
            r"\bdeprecat",
        ],
    ),
    (
        Intent.HOW_TO,
        [
            r"\bhow (?:do|can|should) i\b",
            r"\bhow to\b",
            r"\bsteps? to\b",
            r"\bconfigur",
            r"\bset up\b",
            r"\benable\b",
            r"\binspect\b",
            r"\bcheck\b",
            r"\bcan i\b",
        ],
    ),
]

_VERSION_RE = re.compile(r"\bv?([23])\.(\d+)(?:\.\d+)?\b", re.IGNORECASE)

#: Maps corpus vocabulary to the ``product_area`` recorded in chunk metadata.
#: Used to gate contradiction pair selection to plausibly-related chunks.
_AREA_KEYWORDS: dict[str, list[str]] = {
    "retries": ["retry", "retries", "backoff", "max_retries", "attempt", "jitter"],
    "rate_limits": ["429", "rate limit", "retry-after", "quota", "throttle", "too many requests"],
    "dlq": ["dlq", "dead-letter", "dead letter", "replay", "dead-lettered"],
    "auth": ["auth", "401", "api key", "header", "x-zephyr-key", "authorization", "credential"],
    "payloads": ["payload", "413", "size limit", "kib", "base64", "serialis", "serializ"],
    "concurrency": ["concurrency", "worker", "visibility timeout", "cpu", "parallel", "lease"],
    "idempotency": ["idempoten", "duplicate", "exactly once", "at-least-once", "dedup"],
    "timeouts": ["timeout", "fail-fast", "heartbeat"],
    "cli": ["cli", "command line", "zephyr dlq", "flag", "windows", "terminal"],
    "sdk": ["sdk", "client.", "enqueue", "push", "handler"],
    "migration": ["migrate", "migration", "upgrade", "breaking"],
    "batch": ["batch", "enqueue_many", "bulk"],
}

_STOP_FOR_ENTITIES = {
    "what", "which", "where", "when", "does", "about", "from", "with", "this",
    "that", "they", "there", "then", "zephyr", "jobs", "job",
}


class RuleBasedAnalyzer:
    """Deterministic intent classifier -- no network, no API key, reproducible.

    Pattern matching rather than a model because intent here is a six-way
    classification over a narrow technical domain with highly characteristic
    surface cues, and a deterministic classifier keeps the ablation table free of
    LLM sampling variance. The LLM variant exists for comparison.
    """

    name = "rules"

    def __init__(self, config) -> None:
        self.config = config
        self.expand = bool(config.get("retrieval.query_analysis.expand_queries", True))
        self.max_expansions = int(config.get("retrieval.query_analysis.max_expansions", 3))

    def analyze(self, query: str) -> QueryAnalysis:
        lowered = query.lower()

        intent = Intent.UNKNOWN
        for candidate, patterns in _INTENT_PATTERNS:
            if any(re.search(p, lowered) for p in patterns):
                intent = candidate
                break

        version = None
        match = _VERSION_RE.search(query)
        if match:
            version = f"{match.group(1)}.{match.group(2)}"

        area = self._product_area(lowered)

        # A reference lookup or migration question wants the canonical answer,
        # which raises the bar for letting a forum post win on authority alone.
        requires_canonical = intent in {
            Intent.API_REFERENCE,
            Intent.VERSION_MIGRATION,
        } or bool(re.search(r"\b(default|maximum|max|limit|exactly)\b", lowered))

        return QueryAnalysis(
            query=query,
            intent=intent,
            product_area=area,
            version_mentioned=version,
            expanded_queries=self._expansions(query, area) if self.expand else [],
            entities=self._entities(query),
            requires_canonical_answer=requires_canonical,
            classifier=self.name,
        )

    # -- helpers -------------------------------------------------------------

    def _product_area(self, lowered: str) -> str | None:
        """Highest-scoring area by keyword hits, or None when nothing matches.

        ``None`` is a meaningful outcome, not a failure: an out-of-scope query
        ("Kubernetes CronJobs") matches no area, and that absence is one of the
        signals the refusal path uses.
        """
        best, best_score = None, 0
        for area, keywords in _AREA_KEYWORDS.items():
            score = sum(1 for kw in keywords if kw in lowered)
            if score > best_score:
                best, best_score = area, score
        return best

    def _entities(self, query: str) -> list[str]:
        """Identifier-like tokens: status codes, versions, snake_case, dotted paths.

        These are the terms BM25 is uniquely good at, so surfacing them lets the
        trace explain why a lexical hit fired.
        """
        found: list[str] = []
        for token in re.findall(r"[A-Za-z0-9][A-Za-z0-9._\-]*", query):
            if token.lower() in _STOP_FOR_ENTITIES:
                continue
            looks_identifier = (
                token.isdigit()
                or bool(re.match(r"^v?\d+\.\d", token))
                or "_" in token
                or "." in token
                or "-" in token
                or any(c.isupper() for c in token[1:])
            )
            if looks_identifier:
                found.append(token)
        seen: set[str] = set()
        return [t for t in found if not (t.lower() in seen or seen.add(t.lower()))][:8]

    def _expansions(self, query: str, area: str | None) -> list[str]:
        """Generate paraphrases to widen lexical coverage.

        Deliberately conservative -- domain synonym substitution plus an
        area-keyword appendix, never free-form rewriting. A noisy expansion
        injects spurious matches into fusion, and on a corpus this size one bad
        expansion can displace the correct hit entirely. The fusion stage also
        down-weights expansion lists relative to the original query.
        """
        out: list[str] = []
        lowered = query.lower()

        synonyms = [
            ("retry forever", "retries indefinitely loop endlessly backoff"),
            ("retries", "retry attempt backoff max_retries"),
            ("retry", "retries attempt backoff max_retries"),
            ("429", "rate limit too many requests Retry-After throttled"),
            ("timeout", "time limit deadline fail-fast visibility"),
            ("dlq", "dead-letter queue failed jobs replay"),
            ("dead letter", "dlq replay failed job"),
            ("disable", "turn off unset none"),
            ("twice", "duplicate redelivery double execution visibility timeout"),
            ("broken", "fails error not working bug"),
            ("windows", "windows credential store powershell"),
            ("payload", "job body size limit bytes"),
            ("concurrency", "workers parallel handlers"),
            ("automatically", "automatic replay manual"),
        ]
        for needle, replacement in synonyms:
            if needle in lowered:
                out.append(f"{query} {replacement}")
                break

        if area:
            out.append(f"{query} {area.replace('_', ' ')}")

        deduped: list[str] = []
        seen = {query.strip().lower()}
        for item in out:
            key = item.strip().lower()
            if key not in seen:
                seen.add(key)
                deduped.append(item.strip())
        return deduped[: self.max_expansions]


class LlmAnalyzer:
    """LLM-backed analyzer using structured outputs (optional).

    Falls back to the rule-based analyzer on any failure rather than raising: a
    transient API error should degrade ranking quality, not abort the query.
    """

    name = "llm"

    _SCHEMA = {
        "type": "object",
        "properties": {
            "intent": {
                "type": "string",
                "enum": [
                    "troubleshooting",
                    "how_to",
                    "conceptual",
                    "api_reference",
                    "version_migration",
                    "opinion",
                ],
            },
            "product_area": {"type": "string"},
            "version_mentioned": {"type": ["string", "null"]},
            "expanded_queries": {"type": "array", "items": {"type": "string"}, "maxItems": 3},
            "entities": {"type": "array", "items": {"type": "string"}},
            "requires_canonical_answer": {"type": "boolean"},
        },
        "required": [
            "intent",
            "product_area",
            "version_mentioned",
            "expanded_queries",
            "entities",
            "requires_canonical_answer",
        ],
        "additionalProperties": False,
    }

    def __init__(self, config) -> None:
        self.config = config
        self._fallback = RuleBasedAnalyzer(config)
        self._client = None

    def _ensure_client(self):  # pragma: no cover - optional path
        if self._client is None:
            from anthropic import Anthropic

            self._client = Anthropic()
        return self._client

    def analyze(self, query: str) -> QueryAnalysis:  # pragma: no cover - optional path
        import json

        try:
            client = self._ensure_client()
            response = client.messages.create(
                model=self.config.llm_model("retrieval.query_analysis.model"),
                max_tokens=1024,
                output_config={
                    "format": {"type": "json_schema", "schema": self._SCHEMA},
                    "effort": "low",
                },
                system=(
                    "Classify technical-support queries about Zephyr, a managed distributed "
                    "task queue. Return only the JSON object. product_area must be one of: "
                    + ", ".join(_AREA_KEYWORDS)
                ),
                messages=[{"role": "user", "content": query}],
            )
            payload = json.loads("".join(b.text for b in response.content if b.type == "text"))
            return QueryAnalysis(
                query=query,
                intent=Intent(payload["intent"]),
                product_area=payload.get("product_area") or None,
                version_mentioned=payload.get("version_mentioned") or None,
                expanded_queries=list(payload.get("expanded_queries") or [])[:3],
                entities=list(payload.get("entities") or []),
                requires_canonical_answer=bool(payload.get("requires_canonical_answer")),
                classifier=self.name,
            )
        except Exception:
            return self._fallback.analyze(query)


QueryAnalyzer = RuleBasedAnalyzer  # default alias


def build_analyzer(config):
    backend = str(config.get("retrieval.query_analysis.backend", "rules")).lower()
    return LlmAnalyzer(config) if backend == "llm" else RuleBasedAnalyzer(config)
