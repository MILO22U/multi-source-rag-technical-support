"""Shared text normalisation for every component that reads text.

BM25 (``index/bm25.py``) and the LSA embedding backend (``index/embeddings.py``)
both call :func:`tokenize`. That is a deliberate constraint: if the lexical and
dense sides of hybrid retrieval disagreed about what a token is, their rank
lists would be incomparable for reasons that have nothing to do with relevance,
and fusion would quietly degrade.

The hard requirement driving the design
---------------------------------------
This is a *technical support* corpus. Its highest-value query terms are
identifiers, not prose: ``429``, ``max_retries``, ``v3.2``, ``X-Zephyr-Key``,
``--legacy-ack``, ``client.enqueue``. A conventional tokenizer that strips
digits and punctuation destroys exactly the vocabulary BM25 is good at, which
would remove the entire reason for running a lexical index alongside embeddings.

So :func:`tokenize` keeps compound identifiers intact *and* additionally emits
their parts. ``X-Zephyr-Key`` yields ``x-zephyr-key``, ``zephyr`` and ``key``,
so it matches both an exact-identifier query and a natural-language one
("what's the zephyr key header"). This is the same trade-off Elasticsearch's
``word_delimiter`` filter makes with ``preserve_original``: a modest amount of
index inflation buys recall on both query styles.
"""

from __future__ import annotations

import re
import unicodedata

__all__ = [
    "STOPWORDS",
    "tokenize",
    "normalize_token",
    "slugify",
    "estimate_tokens",
    "sentences",
]

# --------------------------------------------------------------------------- #
# Stopwords
# --------------------------------------------------------------------------- #

#: Intentionally short. An aggressive stoplist is risky on a technical corpus --
#: "no", "not", "all" and "off" carry real meaning in configuration contexts
#: ("retries off", "not acknowledged"), and dropping them costs precision.
STOPWORDS: frozenset[str] = frozenset(
    {
        "a", "an", "and", "are", "as", "at", "be", "been", "but", "by",
        "can", "did", "do", "does", "for", "from", "had", "has", "have",
        "i", "if", "in", "into", "is", "it", "its", "just", "me", "my",
        "of", "on", "or", "our", "should", "so", "some", "than", "that",
        "the", "their", "them", "then", "there", "these", "they", "this",
        "to", "was", "we", "were", "what", "when", "where", "which", "who",
        "why", "will", "with", "would", "you", "your",
    }
)

# --------------------------------------------------------------------------- #
# Patterns
# --------------------------------------------------------------------------- #

#: A token is an alphanumeric run, optionally joined to further runs by one of
#: ``. _ - /``. That single rule covers versions (``3.2``), snake_case
#: (``max_retries``), kebab-case (``dead-letter-queue``), dotted attribute paths
#: (``client.dlq.replay``) and CLI flags (leading dashes are stripped by the
#: character class, so ``--legacy-ack`` arrives as ``legacy-ack``).
_TOKEN_RE = re.compile(r"[a-z0-9]+(?:[._\-/][a-z0-9]+)*")

#: Version-like tokens must never be split: splitting ``3.2`` into ``3`` and
#: ``2`` produces two meaningless high-frequency tokens and lets a v2 document
#: match a v3 query.
_VERSION_RE = re.compile(r"^v?\d+(?:\.\d+)*$")

#: Separators used to derive sub-tokens from a compound identifier.
_SPLIT_RE = re.compile(r"[._\-/]")

_SLUG_STRIP_RE = re.compile(r"[^a-z0-9]+")

#: Sentence boundary: terminal punctuation followed by whitespace and a capital
#: or digit. Avoids splitting on ``v3.2`` or ``e.g.`` in the common cases.
_SENTENCE_RE = re.compile(r"(?<=[.!?])\s+(?=[A-Z0-9])")


# --------------------------------------------------------------------------- #
# Normalisation
# --------------------------------------------------------------------------- #


def normalize_token(tok: str) -> str:
    """Apply crude suffix stripping to a single token.

    Deliberately simpler than a real stemmer (Porter et al.). Two reasons: this
    package has no third-party dependencies, and over-aggressive stemming is
    actively harmful on identifier-heavy text -- conflating ``ack`` with
    ``acks`` is useful, conflating ``backoff`` with ``back`` is not.

    Tokens containing digits or separators are returned untouched: ``v3.2``,
    ``429`` and ``max_retries`` must survive byte-identical for exact matching
    to work.

    >>> normalize_token("retries")
    'retry'
    >>> normalize_token("v3.2")
    'v3.2'
    >>> normalize_token("address")
    'address'
    """
    if len(tok) <= 3 or any(ch.isdigit() for ch in tok) or _SPLIT_RE.search(tok):
        return tok

    if tok.endswith("ies") and len(tok) > 4:
        return tok[:-3] + "y"  # retries -> retry
    if tok.endswith("sses"):
        return tok[:-2]  # classes -> class
    if tok.endswith("ss") or tok.endswith("us") or tok.endswith("is"):
        return tok  # address, status, analysis
    if tok.endswith("s") and len(tok) > 3:
        return tok[:-1]  # jobs -> job
    if tok.endswith("ing") and len(tok) > 6:
        return tok[:-3]  # configuring -> configur
    if tok.endswith("ed") and len(tok) > 5:
        return tok[:-2]  # acknowledged -> acknowledg
    return tok


def tokenize(text: str, *, split_compounds: bool = True) -> list[str]:
    """Normalise text into the token stream used by every index.

    Lowercases, strips accents, extracts alphanumeric-and-separator runs, drops
    stopwords, and applies :func:`normalize_token`. Compound identifiers are
    emitted whole *and* split into parts, so both exact and natural-language
    queries can match them.

    Args:
        text: Raw input.
        split_compounds: When False, emit compounds whole only. Used by
            diagnostics that need a one-to-one token mapping.

    Returns:
        Token list in document order. Duplicates are preserved -- BM25 needs
        term frequencies.

    >>> tokenize("Why do my jobs retry forever on a 429 from max_retries in v3.2?")
    ['job', 'retry', 'forever', '429', 'max_retries', 'max', 'retry', 'v3.2']
    """
    if not text:
        return []

    folded = unicodedata.normalize("NFKD", text).encode("ascii", "ignore").decode("ascii")
    out: list[str] = []

    for raw in _TOKEN_RE.findall(folded.lower()):
        is_version = bool(_VERSION_RE.match(raw))
        has_parts = bool(_SPLIT_RE.search(raw))

        if raw not in STOPWORDS:
            out.append(raw if (is_version or has_parts) else normalize_token(raw))

        # Emit sub-tokens for compounds, but never for versions.
        if split_compounds and has_parts and not is_version:
            for part in _SPLIT_RE.split(raw):
                if len(part) >= 2 and part not in STOPWORDS and not _VERSION_RE.match(part):
                    out.append(normalize_token(part))

    return out


def slugify(text: str, *, max_len: int = 60) -> str:
    """Convert a heading into the stable id fragment used in ``chunk_id``.

    Chunk ids are content-addressed precisely so that gold-set labels survive
    re-chunking, which makes this function part of the evaluation contract:
    changing its output invalidates ``eval/gold.json``. Treat it as frozen once
    labels exist.

    >>> slugify("Configuring Backoff")
    'configuring-backoff'
    >>> slugify("What does timeout=0 do?")
    'what-does-timeout-0-do'
    """
    folded = unicodedata.normalize("NFKD", text).encode("ascii", "ignore").decode("ascii")
    slug = _SLUG_STRIP_RE.sub("-", folded.lower()).strip("-")
    if len(slug) > max_len:
        slug = slug[:max_len].rsplit("-", 1)[0] or slug[:max_len]
    return slug


def estimate_tokens(text: str) -> int:
    """Approximate the LLM token count of ``text``.

    A local proxy, not ground truth. The real figure comes from the provider's
    ``messages.count_tokens`` endpoint, which this function stands in for so
    that chunking stays runnable with no network and no API key; swap it behind
    the same signature when an API key is available.

    Blends two estimators and takes the larger, which keeps the result
    conservative for both prose (word-count dominated) and code or JSON
    (character-count dominated, since punctuation tokenises densely).

    Chunk budgets are computed in tokens rather than characters on purpose:
    character budgets drift roughly 30% between prose and code, which is enough
    to overflow a context window you believed was half full.
    """
    if not text:
        return 0
    char_estimate = len(text) / 4.0
    word_estimate = len(text.split()) * 1.3
    return max(1, int(round(max(char_estimate, word_estimate))))


def sentences(text: str) -> list[str]:
    """Split prose into sentences.

    Used by the blog chunker (to cut windows on sentence boundaries) and by the
    extractive generator (to attribute citations per sentence). Good enough for
    this corpus; a real deployment would use a proper segmenter.
    """
    parts = [p.strip() for p in _SENTENCE_RE.split(text.strip()) if p.strip()]
    return parts or ([text.strip()] if text.strip() else [])
