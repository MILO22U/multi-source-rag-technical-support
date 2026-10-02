"""Answer synthesis with mandatory conflict disclosure.

The failure mode this module is built to prevent
------------------------------------------------
**Silent averaging.** Given one source saying "3" and another saying "5", a
generator asked to synthesise will happily produce "around 3 to 5", or pick one
with full confidence and no indication that its sources disagreed. The user gets
a confident wrong answer and no way to detect it. That is strictly worse than
being told the sources disagree and why.

So conflicts reach the generator as **structured input** -- this pair conflicts,
this is the type, this rule resolved it, these are the two claims -- never as two
contradictory chunks dropped into context with a hope that it works out.

The governing principle: an honest system distinguishes *"I know"* from *"my
sources disagree"*. Collapsing that distinction to sound confident leaves the
user's trust miscalibrated in a way they cannot detect.

Two backends
------------
``ExtractiveSynthesizer`` (default) selects and quotes the most query-relevant
sentences, so every statement is traceable to a chunk by construction and the
evaluation has no generation variance. ``LlmSynthesizer`` uses a hosted LLM with
document blocks and ``citations`` enabled, which yields span-level attribution
reported by the model rather than parsed out of prose.
"""

from __future__ import annotations

import math
from collections import Counter

from ..tokenize import sentences, tokenize
from ..types import Answer, Citation, Resolution, ScoredChunk

__all__ = ["ExtractiveSynthesizer", "LlmSynthesizer", "build_synthesizer"]


_RELATIONSHIP_PREFACE = {
    "version_drift": "This changed between versions",
    "conditional": "This depends on your version",
    "deprecation": "One form is deprecated",
    "misconception": "A common community answer is incorrect here",
    "docs_drift": "Two documentation pages disagree",
    "docs_gap": "This is not covered in the official documentation",
    "empirical_override": "Documented behaviour and reported behaviour differ",
    "contradict": "Sources conflict",
}


class ExtractiveSynthesizer:
    """Compose an answer by quoting the most query-relevant sentences."""

    name = "extractive"

    def __init__(self, config) -> None:
        self.config = config
        self.max_chunks = int(config.get("generation.max_context_chunks", 8))
        self.refuse_below = float(config.get("generation.refuse_below_score", 0.18))
        self.refuse_below_coverage = float(config.get("generation.refuse_below_coverage", 0.34))
        self.max_unknown_ratio = float(config.get("generation.refuse_above_unknown_ratio", 0.40))
        self.max_sentences_per_chunk = 3
        self.max_sentence_chars = 400
        #: Every token present anywhere in the corpus. Injected by the pipeline.
        #: Knowing what the corpus does *not* contain is a far stronger
        #: out-of-scope signal than any score computed over what it does.
        self.corpus_vocab: set[str] = set()
        #: token -> number of corpus chunks containing it, and the chunk total.
        #: Used to exclude corpus-ubiquitous terms from scope judgements.
        self.corpus_df: dict[str, int] = {}
        self.corpus_chunks: int = 0

    def synthesize(
        self,
        query: str,
        chunks: list[ScoredChunk],
        resolutions: list[Resolution],
    ) -> Answer:
        if not chunks:
            return self._refuse(query, chunks)

        # Refusal uses two independent signals, because neither alone is reliable.
        #
        # ``final_score`` is unusable for this: it is min-max normalised within the
        # candidate set, so the top chunk always scores exactly 1.0 regardless of
        # whether anything relevant was found.
        #
        # The reranker's absolute score helps but is not decisive -- an off-topic
        # query still matches generic vocabulary ("Zephyr", "job") and lands in a
        # middling band that overlaps with genuinely weak-but-valid queries.
        #
        # **Term coverage is the discriminating signal.** An out-of-scope query
        # carries distinctive terms that appear nowhere in the corpus
        # ("kubernetes", "cronjob"), so measuring how many of the query's
        # distinctive terms actually occur in the retrieved text separates "we
        # found little" from "this topic does not exist here".
        # The decisive test is the third one: terms that appear *nowhere in the
        # corpus*. "Terraform provider" defeats pure coverage scoring because
        # "provider" does occur (in "payment provider"), giving 50% coverage on a
        # question the corpus cannot answer at all. "terraform" being absent from
        # the entire vocabulary is unambiguous.
        best_ce = max((sc.ce_score or 0.0) for sc in chunks)
        coverage = self._distinctive_coverage(query, chunks)
        unknown = self._unknown_ratio(query)
        if (
            best_ce < self.refuse_below
            or coverage < self.refuse_below_coverage
            or unknown >= self.max_unknown_ratio
        ):
            return self._refuse(
                query, chunks, coverage=coverage, best_ce=best_ce, unknown=unknown
            )

        used = chunks[: self.max_chunks]
        idf = self._idf(used)

        paragraphs: list[str] = []
        citations: list[Citation] = []
        sentence_index = 0

        for sc in used[:4]:
            picked = self._best_sentences(query, sc, idf)
            if not picked:
                continue
            label = self._label(sc)
            body = " ".join(picked)
            paragraphs.append(f"**{label}:** {body}")
            citations.append(
                Citation(
                    chunk_id=sc.chunk.chunk_id,
                    source=sc.chunk.source,
                    cited_text=body[:300],
                    sentence_index=sentence_index,
                )
            )
            sentence_index += 1

        text = "\n\n".join(paragraphs) if paragraphs else "No directly relevant passage was found."

        disclosure = self._disclose(resolutions)
        if disclosure:
            text = f"{text}\n\n{disclosure}"

        return Answer(
            query=query,
            text=text,
            citations=citations,
            resolutions=resolutions,
            used_chunks=used,
            refused=False,
            generator=self.name,
        )

    # -- helpers -------------------------------------------------------------

    def _idf(self, chunks: list[ScoredChunk]) -> dict[str, float]:
        df: Counter[str] = Counter()
        for sc in chunks:
            df.update(set(tokenize(sc.chunk.display_text)))
        n = max(1, len(chunks))
        return {t: math.log(1.0 + (n + 1.0) / (c + 0.5)) for t, c in df.items()}

    def _distinctive_coverage(self, query: str, chunks: list[ScoredChunk]) -> float:
        """Fraction of the query's distinctive terms present in the retrieved text.

        "Distinctive" excludes terms that appear in most retrieved chunks, since a
        term occurring everywhere carries no topical information and would inflate
        coverage for any query mentioning the product name.
        """
        q_terms = [t for t in dict.fromkeys(tokenize(query)) if len(t) > 2]
        if not q_terms:
            return 1.0

        per_chunk = [set(tokenize(sc.chunk.display_text)) for sc in chunks[: self.max_chunks]]
        if not per_chunk:
            return 0.0
        union = set().union(*per_chunk)
        ubiquity_cut = max(1, int(len(per_chunk) * 0.6))

        distinctive = [
            t for t in q_terms if sum(1 for s in per_chunk if t in s) <= ubiquity_cut
        ]
        if not distinctive:
            return 1.0
        return sum(1 for t in distinctive if t in union) / len(distinctive)

    def _unknown_ratio(self, query: str) -> float:
        """Fraction of the query's *discriminating* terms absent from the corpus.

        Corpus-ubiquitous terms are excluded from the denominator. The product
        name is the clear case: "zephyr" occurs in every chunk, so it carries no
        information about whether a question is answerable, yet counting it
        inflates the denominator and dilutes the signal. "Does Zephyr have a
        Terraform provider?" scores 1/3 unknown with it included and 1/2 without
        -- and only the second reflects that the corpus has nothing on Terraform.

        Returns 0.0 when no corpus statistics have been injected, so the signal
        goes quiet rather than refusing everything.
        """
        if not self.corpus_vocab:
            return 0.0
        terms = [t for t in dict.fromkeys(tokenize(query)) if len(t) > 3]
        if self.corpus_chunks:
            ceiling = self.corpus_chunks * 0.6
            terms = [t for t in terms if self.corpus_df.get(t, 0) <= ceiling]
        if not terms:
            return 0.0
        return sum(1 for t in terms if t not in self.corpus_vocab) / len(terms)

    def _best_sentences(self, query: str, sc: ScoredChunk, idf: dict[str, float]) -> list[str]:
        """Pick the sentences carrying the most query signal, in document order.

        Markdown tables and fenced code blocks contain no sentence terminators, so
        the splitter returns them as single enormous "sentences". Quoting one
        verbatim buries the answer, so over-long candidates are rejected and code
        fences are stripped before scoring -- a code sample is excellent
        documentation and poor prose.
        """
        q_terms = set(tokenize(query))
        if not q_terms:
            return []

        scored: list[tuple[float, int, str]] = []
        for i, sentence in enumerate(sentences(sc.chunk.display_text)):
            cleaned = self._strip_blocks(sentence)
            if not (25 <= len(cleaned) <= self.max_sentence_chars):
                continue
            tokens = set(tokenize(cleaned))
            overlap = q_terms & tokens
            if not overlap:
                continue
            score = sum(idf.get(t, 1.0) for t in overlap)
            # Prefer statements over restatements of the question.
            if cleaned.rstrip().endswith("?") or cleaned.startswith("QUESTION:"):
                score *= 0.25
            # Prefer prose over table rows, which match terms without explaining.
            if cleaned.count("|") >= 3:
                score *= 0.4
            # Boost sentences carrying a concrete value. A reference question
            # ("what is the maximum payload size") is answered by the sentence
            # containing the number, which often shares fewer query terms than
            # the surrounding prose and otherwise loses the selection.
            if any(ch.isdigit() for ch in cleaned):
                score *= 1.45
            if "--" in sentence or "`" in sentence:
                score *= 1.25
            scored.append((score, i, cleaned))

        scored.sort(key=lambda t: -t[0])
        chosen = sorted(scored[: self.max_sentences_per_chunk], key=lambda t: t[1])
        return [s for _, _, s in chosen]

    @staticmethod
    def _strip_blocks(text: str) -> str:
        """Remove fenced code and collapse whitespace for quotable prose."""
        import re

        text = re.sub(r"```.*?```", " ", text, flags=re.DOTALL)
        text = re.sub(r"`{1,2}([^`]*)`{1,2}", r"\1", text)
        text = re.sub(r"^\s*#+\s*", "", text, flags=re.MULTILINE)
        return re.sub(r"\s+", " ", text).strip()

    def _label(self, sc: ScoredChunk) -> str:
        """Human-readable provenance label -- attribution is part of the answer.

        "Retries default to 5" and "the v3.2 reference page states retries default
        to 5" are different answers with different usefulness, and the difference
        is entirely attribution.
        """
        meta = sc.chunk.metadata
        if sc.chunk.source == "docs":
            title = meta.get("title") or sc.chunk.doc_id
            return f"Documentation — {title} (v{meta.get('version', '?')})"
        if sc.chunk.source == "forum":
            role = meta.get("author_role", "user")
            tag = "accepted answer" if meta.get("is_accepted") else "answer"
            return f"Forum — {tag} by {role}, {meta.get('votes', 0)} votes"
        published = str(meta.get("published_at", ""))[:7]
        return f"Blog — {meta.get('author_role', 'guest')}, {published} (v{meta.get('product_version_at_time', '?')} era)"

    def _disclose(self, resolutions: list[Resolution]) -> str:
        """Render the conflict section. Never summarised away, never averaged."""
        shown = [r for r in resolutions if r.must_disclose]
        if not shown:
            return ""
        lines = ["### Note on conflicting information"]
        for r in shown[:4]:
            preface = _RELATIONSHIP_PREFACE.get(
                str(r.finding.relationship), "Sources differ"
            )
            lines.append(f"- **{preface}.** {r.explanation}")
            lines.append(f"  - {r.finding.claim_a[:180]}")
            lines.append(f"  - {r.finding.claim_b[:180]}")
            lines.append(f"  - *Resolved by rule:* `{r.rule_fired}` → `{r.winner}`")
        return "\n".join(lines)

    def _refuse(
        self,
        query: str,
        chunks: list[ScoredChunk],
        *,
        coverage: float = 0.0,
        best_ce: float = 0.0,
        unknown: float = 0.0,
    ) -> Answer:
        """Decline rather than improvise.

        A graceful refusal is a feature, not a gap: the alternative on an
        out-of-scope query is a fluent answer assembled from loosely-related
        passages, which is the most damaging output a support system can produce --
        it is confidently wrong and gives the user no signal that it is.
        """
        return Answer(
            query=query,
            text=(
                "I could not find information about this in the available sources "
                "(product documentation, community forum, or engineering blog). "
                f"Only {coverage:.0%} of the distinctive terms in your question appear "
                "anywhere in the indexed corpus, so rather than assembling a plausible "
                "answer from loosely-related passages, I am flagging this as out of scope.\n\n"
                "If this topic should be covered, it is missing from the knowledge base "
                "rather than from the retrieval."
            ),
            citations=[],
            resolutions=[],
            used_chunks=chunks[:3],
            refused=True,
            generator=self.name,
        )


class LlmSynthesizer:
    """LLM-backed synthesis with span-level citations (optional)."""

    name = "llm"

    _SYSTEM = """You answer technical-support questions about Zephyr, a managed \
distributed task queue, using only the supplied documents.

Rules, in priority order:

1. Lead with the resolved answer. Be specific and concrete.
2. Attribute claims to their source type (documentation, forum, blog) inline.
3. When a CONFLICT block is supplied, you MUST surface it. Never average or \
split the difference between conflicting values -- if one source says 3 and \
another says 5, never write "3 to 5". State the resolved value, then explain the \
disagreement and its resolution under a short "Note on conflicting information" \
heading.
4. For version-scoped conflicts, give the current behaviour and name the version \
in which it changed.
5. For a misconception, name the incorrect belief explicitly and correct it -- the \
user may already hold it.
6. If the documents do not answer the question, say so plainly. Do not improvise.
7. Never invent configuration keys, flags, limits or version numbers."""

    def __init__(self, config) -> None:
        self.config = config
        self.model = config.llm_model("generation.llm.model")
        self.max_tokens = int(config.get("generation.llm.max_tokens", 2048))
        self.effort = config.get("generation.llm.effort", "high")
        self.use_citations = bool(config.get("generation.llm.use_citations", True))
        self._fallback = ExtractiveSynthesizer(config)
        self._client = None

    def synthesize(
        self, query: str, chunks: list[ScoredChunk], resolutions: list[Resolution]
    ) -> Answer:  # pragma: no cover - optional path
        try:
            from anthropic import Anthropic

            if self._client is None:
                self._client = Anthropic()

            documents = [
                {
                    "type": "document",
                    "source": {"type": "text", "media_type": "text/plain", "data": sc.chunk.display_text},
                    "title": f"{sc.chunk.chunk_id} | {sc.chunk.context_header}",
                    "citations": {"enabled": self.use_citations},
                }
                for sc in chunks
            ]

            conflict_block = ""
            if resolutions:
                parts = ["<conflicts>"]
                for r in resolutions:
                    parts.append(
                        f"<conflict type=\"{r.finding.relationship}\" rule=\"{r.rule_fired}\" "
                        f"winner=\"{r.winner}\">\n"
                        f"  <claim_a>{r.finding.claim_a}</claim_a>\n"
                        f"  <claim_b>{r.finding.claim_b}</claim_b>\n"
                        f"  <resolution>{r.explanation}</resolution>\n"
                        f"</conflict>"
                    )
                parts.append("</conflicts>")
                conflict_block = "\n".join(parts)

            content = [*documents, {"type": "text", "text": f"{conflict_block}\n\nQuestion: {query}"}]

            with self._client.messages.stream(
                model=self.model,
                max_tokens=self.max_tokens,
                thinking={"type": "adaptive"},
                output_config={"effort": self.effort},
                system=self._SYSTEM,
                messages=[{"role": "user", "content": content}],
            ) as stream:
                response = stream.get_final_message()

            if getattr(response, "stop_reason", None) == "refusal":
                return self._fallback.synthesize(query, chunks, resolutions)

            text_parts: list[str] = []
            citations: list[Citation] = []
            for i, block in enumerate(response.content):
                if block.type != "text":
                    continue
                text_parts.append(block.text)
                for cit in getattr(block, "citations", None) or []:
                    title = getattr(cit, "document_title", "") or ""
                    chunk_id = title.split(" | ")[0]
                    match = next((sc for sc in chunks if sc.chunk.chunk_id == chunk_id), None)
                    citations.append(
                        Citation(
                            chunk_id=chunk_id,
                            source=match.chunk.source if match else "docs",  # type: ignore[arg-type]
                            cited_text=getattr(cit, "cited_text", "")[:300],
                            sentence_index=i,
                        )
                    )

            return Answer(
                query=query,
                text="".join(text_parts).strip(),
                citations=citations,
                resolutions=resolutions,
                used_chunks=chunks,
                refused=False,
                generator=self.name,
            )
        except Exception:
            return self._fallback.synthesize(query, chunks, resolutions)


def build_synthesizer(config):
    backend = str(config.get("generation.backend", "extractive")).lower()
    return LlmSynthesizer(config) if backend == "llm" else ExtractiveSynthesizer(config)
