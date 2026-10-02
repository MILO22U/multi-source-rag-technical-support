"""Tests for core data types.

Most of these pin behaviour that later stages depend on. The gold-label matching
tests matter most: ``eval/gold.json`` references chunk ids, so a change in
matching semantics silently changes every retrieval metric in the report.
"""

import pytest

from zephyr_rag.types import (
    Answer,
    Chunk,
    ConflictFinding,
    Intent,
    Relationship,
    Resolution,
    ScoredChunk,
)


def make_chunk(chunk_id: str = "docs:retries-and-backoff:configuring-backoff", **meta) -> Chunk:
    return Chunk(
        chunk_id=chunk_id,
        source=chunk_id.split(":")[0],  # type: ignore[arg-type]
        text="header\n\nbody text",
        display_text="body text",
        context_header="header",
        metadata=meta or {"version": "3.2", "doc_type": "reference"},
        token_count=12,
    )


class TestChunkIdentity:
    def test_doc_id_strips_source_and_slug(self):
        assert make_chunk().doc_id == "retries-and-backoff"

    def test_doc_prefix_is_source_plus_doc(self):
        assert make_chunk().doc_prefix == "docs:retries-and-backoff"

    def test_version_read_from_any_metadata_key(self):
        assert make_chunk(version="3.2").version == "3.2"
        assert make_chunk(product_version_at_time="2.4").version == "2.4"
        assert make_chunk(tags=["x"]).version is None


class TestGoldLabelMatching:
    """Exact-or-prefix matching lets gold labels be written at document or
    section granularity."""

    def test_exact_match(self):
        c = make_chunk()
        assert c.matches_label("docs:retries-and-backoff:configuring-backoff")

    def test_document_level_label_matches_any_section(self):
        c = make_chunk()
        assert c.matches_label("docs:retries-and-backoff")

    def test_partial_segment_does_not_match(self):
        """Without the ':' guard, 'docs:retries-and' would match a different
        document -- the bug this test exists to prevent."""
        c = make_chunk()
        assert not c.matches_label("docs:retries-and")

    def test_sibling_document_does_not_match(self):
        c = make_chunk("docs:retries-advanced:intro")
        assert not c.matches_label("docs:retries")


class TestScoredChunk:
    def test_rank_delta_positive_when_promoted(self):
        sc = ScoredChunk(
            chunk=make_chunk(), retrieval_score=0.3, final_score=0.8,
            ce_score=0.9, rank_before=14, rank_after=2,
        )
        assert sc.rank_delta == 12

    def test_rank_delta_zero_when_unranked(self):
        sc = ScoredChunk(chunk=make_chunk(), retrieval_score=0.3, final_score=0.3)
        assert sc.rank_delta == 0

    def test_with_ranks_returns_copy(self):
        sc = ScoredChunk(chunk=make_chunk(), retrieval_score=0.3, final_score=0.3)
        updated = sc.with_ranks(before=5, after=1)
        assert updated.rank_delta == 4
        assert sc.rank_before == -1, "original must stay unmodified (frozen)"

    def test_frozen(self):
        sc = ScoredChunk(chunk=make_chunk(), retrieval_score=0.3, final_score=0.3)
        with pytest.raises(Exception):
            sc.final_score = 0.9  # type: ignore[misc]


class TestRelationshipTaxonomy:
    @pytest.mark.parametrize(
        "rel",
        [
            Relationship.CONTRADICT,
            Relationship.VERSION_DRIFT,
            Relationship.MISCONCEPTION,
            Relationship.DOCS_DRIFT,
            Relationship.DOCS_GAP,
            Relationship.EMPIRICAL_OVERRIDE,
            Relationship.CONDITIONAL,
            Relationship.DEPRECATION,
        ],
    )
    def test_conflicts_require_resolution(self, rel):
        assert rel.is_conflict

    @pytest.mark.parametrize(
        "rel",
        [
            Relationship.AGREE,
            Relationship.COMPLEMENTARY,
            Relationship.REFINES,
            Relationship.UNRELATED,
        ],
    )
    def test_non_conflicts_need_no_resolution(self, rel):
        """C11 in the planted ledger is a complementary pair; flagging it would
        be a false positive."""
        assert not rel.is_conflict

    def test_str_enum_compares_to_plain_string(self):
        assert Intent.TROUBLESHOOTING == "troubleshooting"
        assert Relationship.AGREE == "agree"


class TestConflictFinding:
    def test_pair_key_is_order_independent(self):
        a = ConflictFinding("x", "y", Relationship.CONTRADICT, "ca", "cb")
        b = ConflictFinding("y", "x", Relationship.CONTRADICT, "cb", "ca")
        assert a.pair_key == b.pair_key, "A-vs-B and B-vs-A must dedupe"


class TestAnswer:
    def test_source_distribution_counts_per_source(self):
        chunks = [
            ScoredChunk(chunk=make_chunk("docs:a:s"), retrieval_score=1, final_score=1),
            ScoredChunk(chunk=make_chunk("docs:b:s"), retrieval_score=1, final_score=1),
            ScoredChunk(chunk=make_chunk("forum:t_1:p1"), retrieval_score=1, final_score=1),
        ]
        ans = Answer(query="q", text="a", used_chunks=chunks)
        assert ans.source_distribution == {"docs": 2, "forum": 1}

    def test_disclosed_conflicts_filters(self):
        f = ConflictFinding("a", "b", Relationship.VERSION_DRIFT, "x", "y")
        shown = Resolution(f, "a", "version_scoping", "because", must_disclose=True)
        hidden = Resolution(f, "both", "complementary", "no conflict", must_disclose=False)
        ans = Answer(query="q", text="a", resolutions=[shown, hidden])
        assert ans.disclosed_conflicts == [shown]
