"""Forum chunker -- thread-aware question/answer pairing.

A forum thread is a conversation, and conversations are not documents. The two
obvious strategies both fail:

* **one chunk per post** -- a post is frequently meaningless alone ("Did you try
  3.1?"), because the question lives in a different post.
* **one chunk per thread** -- too large, and it fuses correct and incorrect
  answers into a single unit.

The natural answer unit is a **problem-solution pair**: the question plus
*exactly one* answer. The "exactly one" is load-bearing for two reasons:

1. Concatenating competing answers gives them a shared retrieval score, so a
   correct answer can no longer be ranked above a wrong one.
2. It hides disagreement *inside* a chunk, where a pairwise contradiction
   detector structurally cannot see it.

That second point generalises: **chunk boundaries set the resolution limit of
contradiction detection.** Thread ``t_0007`` in this corpus is the test case --
a wrong answer with 47 votes that the OP accepted, and a staff correction below
it. Only separate chunks can surface that conflict.
"""

from __future__ import annotations

import re

from ..ingest.loaders import RawDocument
from ..tokenize import estimate_tokens, sentences
from ..types import Chunk
from .base import make_chunk
from .contextual import build_header

__all__ = ["ForumChunker"]

_QUOTE_RE = re.compile(r"^\s*>.*$", re.MULTILINE)


class ForumChunker:
    """Pair a thread's question with each of its answers."""

    source = "forum"

    def __init__(self, config) -> None:
        self.config = config
        self.min_votes = int(config.get("chunking.forum.min_votes_for_non_accepted", 3))
        self.include_followups = bool(config.get("chunking.forum.include_op_followups", True))
        self.strip_quotes = bool(config.get("chunking.forum.strip_quoted_replies", True))
        self.max_question_tokens = int(config.get("chunking.forum.max_question_tokens", 180))

    def chunk(self, doc: RawDocument) -> list[Chunk]:
        posts = doc.posts
        if not posts:
            return []

        opening = next((p for p in posts if p.get("parent_post_id") is None), posts[0])
        question = self._format_question(doc.title, opening)

        # Direct replies to the opening post are answers. Replies to an answer
        # are follow-ups and are appended to that answer's chunk, because a
        # confirmation ("that fixed it") is evidence about the answer rather
        # than an answer in its own right.
        answers = [p for p in posts if p.get("parent_post_id") == opening.get("post_id")]
        followups: dict[str, list[dict]] = {}
        for post in posts:
            parent = post.get("parent_post_id")
            if parent and parent != opening.get("post_id"):
                followups.setdefault(str(parent), []).append(post)

        chunks: list[Chunk] = []
        for answer in answers:
            if not self._keep(answer):
                continue
            body_parts = [question, "", f"ANSWER ({self._describe(answer)}):", self._clean(answer.get("body", ""))]

            if self.include_followups:
                for follow in followups.get(str(answer.get("post_id")), []):
                    body_parts.extend(
                        ["", f"FOLLOW-UP ({self._describe(follow)}):", self._clean(follow.get("body", ""))]
                    )

            header = build_header("forum", self.config, thread_meta=doc.metadata, post=answer)
            chunks.append(
                make_chunk(
                    chunk_id=f"forum:{doc.doc_id}:{answer.get('post_id')}",
                    source="forum",
                    body="\n".join(body_parts),
                    context_header=header,
                    metadata={
                        **doc.metadata,
                        "post_id": answer.get("post_id"),
                        "author": answer.get("author"),
                        "author_role": answer.get("author_role", "user"),
                        "votes": int(answer.get("votes", 0)),
                        "is_accepted": bool(answer.get("is_accepted", False)),
                        "created_at": answer.get("created_at") or doc.metadata.get("created_at"),
                        "corroborating_followups": len(followups.get(str(answer.get("post_id")), [])),
                        "chunker": "thread_qa_pair",
                    },
                    parent_id=f"forum:{doc.doc_id}",
                )
            )

        # An unanswered thread still describes a real symptom, and matching a
        # user's symptom to a known-unsolved report is a useful outcome -- so the
        # question is indexed alone rather than dropped.
        if not chunks:
            header = build_header("forum", self.config, thread_meta=doc.metadata, post=opening)
            chunks.append(
                make_chunk(
                    chunk_id=f"forum:{doc.doc_id}:{opening.get('post_id', 'p1')}",
                    source="forum",
                    body=question,
                    context_header=header,
                    metadata={
                        **doc.metadata,
                        "post_id": opening.get("post_id"),
                        "author_role": opening.get("author_role", "user"),
                        "votes": int(opening.get("votes", 0)),
                        "is_accepted": False,
                        "created_at": opening.get("created_at"),
                        "unanswered": True,
                        "chunker": "thread_qa_pair",
                    },
                    parent_id=f"forum:{doc.doc_id}",
                )
            )
        return chunks

    # -- helpers -------------------------------------------------------------

    def _keep(self, answer: dict) -> bool:
        """Vote threshold for non-accepted answers.

        Accepted answers and staff posts are always kept regardless of votes:
        ``t_0007``'s staff correction has 12 votes against a 47-vote wrong
        answer, and dropping low-vote staff posts would delete the correction
        that makes the misconception detectable.
        """
        if answer.get("is_accepted") or answer.get("author_role") in {"staff", "mvp"}:
            return True
        return int(answer.get("votes", 0)) >= self.min_votes

    def _describe(self, post: dict) -> str:
        bits = [str(post.get("author_role", "user"))]
        if post.get("is_accepted"):
            bits.append("accepted")
        bits.append(f"{int(post.get('votes', 0))} votes")
        return ", ".join(bits)

    def _clean(self, body: str) -> str:
        """Strip quoted reply blocks, which duplicate text and skew BM25 term
        frequencies toward whatever was being quoted."""
        if self.strip_quotes:
            body = _QUOTE_RE.sub("", body)
        return re.sub(r"\n{3,}", "\n\n", body).strip()

    def _format_question(self, title: str, opening: dict) -> str:
        """Title plus opening post, truncated at a sentence boundary if long.

        Truncated rather than dropped: the question supplies the symptom
        vocabulary a troubleshooting query matches on, so losing it would cost
        recall on exactly the intent forums are best at.
        """
        body = self._clean(opening.get("body", ""))
        if estimate_tokens(body) > self.max_question_tokens:
            kept: list[str] = []
            total = 0
            for sentence in sentences(body):
                t = estimate_tokens(sentence)
                if total + t > self.max_question_tokens:
                    break
                kept.append(sentence)
                total += t
            body = " ".join(kept) or body[:600]
        return f"QUESTION: {title}\n{body}".strip()
