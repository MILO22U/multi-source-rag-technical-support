"""Tests for the shared tokenizer.

The first test class is the important one: if technical identifiers do not
survive tokenisation, BM25 loses the advantage that justifies running a lexical
index at all, and hybrid retrieval silently collapses to dense-only.
"""

from zephyr_rag.tokenize import (
    estimate_tokens,
    normalize_token,
    sentences,
    slugify,
    tokenize,
)


class TestTechnicalIdentifiersSurvive:
    """Identifier preservation -- the load-bearing property of this module."""

    def test_bare_status_code_survives(self):
        assert "429" in tokenize("I keep getting a 429 from the API")

    def test_version_is_not_split(self):
        toks = tokenize("upgrading to v3.2 broke it")
        assert "v3.2" in toks
        # Splitting a version into '3' and '2' would let v2 docs match v3 queries.
        assert "3" not in toks and "2" not in toks

    def test_bare_dotted_version_is_not_split(self):
        assert tokenize("version 3.2") == ["version", "3.2"]

    def test_snake_case_kept_whole_and_split(self):
        toks = tokenize("set max_retries higher")
        assert "max_retries" in toks  # exact-identifier queries
        assert "max" in toks and "retry" in toks  # natural-language queries

    def test_header_name_kept_whole_and_split(self):
        toks = tokenize("pass the X-Zephyr-Key header")
        assert "x-zephyr-key" in toks
        assert "zephyr" in toks and "key" in toks

    def test_cli_flag_loses_only_leading_dashes(self):
        assert "legacy-ack" in tokenize("the --legacy-ack flag")

    def test_dotted_attribute_path(self):
        toks = tokenize("call client.dlq.replay() to retry")
        assert "client.dlq.replay" in toks
        assert "client" in toks and "dlq" in toks and "replay" in toks


class TestNormalisation:
    def test_stopwords_removed(self):
        toks = tokenize("why do my jobs fail")
        for dropped in ("why", "do", "my"):
            assert dropped not in toks

    def test_plural_folding(self):
        assert normalize_token("jobs") == "job"
        assert normalize_token("retries") == "retry"

    def test_double_s_not_stripped(self):
        assert normalize_token("address") == "address"
        assert normalize_token("status") == "status"

    def test_short_tokens_untouched(self):
        assert normalize_token("ack") == "ack"

    def test_identifiers_never_stemmed(self):
        assert normalize_token("max_retries") == "max_retries"
        assert normalize_token("v3.2") == "v3.2"

    def test_query_and_document_tokenise_compatibly(self):
        """Query/document vocabulary must align or fusion is meaningless."""
        q = set(tokenize("jobs retrying forever"))
        d = set(tokenize("The job retried forever because backoff never capped."))
        assert q & d, "no shared tokens between paraphrases of the same thing"

    def test_empty_input(self):
        assert tokenize("") == []
        assert tokenize("   ") == []

    def test_accents_folded(self):
        assert "naive" in tokenize("naïve retry logic")


class TestSlugify:
    def test_basic(self):
        assert slugify("Configuring Backoff") == "configuring-backoff"

    def test_punctuation_and_equals(self):
        assert slugify("What does timeout=0 do?") == "what-does-timeout-0-do"

    def test_stable_for_chunk_ids(self):
        """slugify output is part of the eval contract -- pin known values."""
        assert slugify("Dead-letter queue") == "dead-letter-queue"
        assert slugify("Rate limits & 429s") == "rate-limits-429s"

    def test_truncates_on_word_boundary(self):
        out = slugify("a very long heading " * 10, max_len=30)
        assert len(out) <= 30
        assert not out.endswith("-")


class TestTokenEstimation:
    def test_scales_with_length(self):
        assert estimate_tokens("word " * 100) > estimate_tokens("word " * 10)

    def test_empty_is_zero(self):
        assert estimate_tokens("") == 0

    def test_dense_punctuation_estimated_higher_than_word_count(self):
        """JSON/code tokenises more densely than prose -- the char estimator
        must dominate there, or chunk budgets overflow."""
        code = '{"a":1,"b":2,"c":3,"d":4,"e":5,"f":6,"g":7,"h":8}'
        assert estimate_tokens(code) > len(code.split()) * 1.3


class TestSentences:
    def test_splits_on_terminal_punctuation(self):
        assert len(sentences("One thing. Two things happened. Three.")) == 3

    def test_does_not_split_version_numbers(self):
        assert len(sentences("We shipped v3.2 last week.")) == 1

    def test_single_sentence_without_terminator(self):
        assert sentences("no terminator here") == ["no terminator here"]
