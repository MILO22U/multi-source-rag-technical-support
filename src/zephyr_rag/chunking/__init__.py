"""Per-source chunking strategies (Requirement 2).

``build_chunkers`` returns the strategy set named by ``chunking.strategy``:
``per_source`` (the real system) or ``uniform`` (the ablation control).
"""

from .base import Chunker
from .blog import BlogChunker
from .docs import DocsChunker
from .forum import ForumChunker
from .uniform import UniformChunker

__all__ = ["Chunker", "DocsChunker", "ForumChunker", "BlogChunker", "UniformChunker", "build_chunkers", "chunk_corpus"]


def build_chunkers(config) -> dict:
    """Map source name -> chunker instance, per ``chunking.strategy``."""
    if config.get("chunking.strategy", "per_source") == "uniform":
        shared = UniformChunker(config)
        return {"docs": shared, "forum": shared, "blog": shared}
    return {
        "docs": DocsChunker(config),
        "forum": ForumChunker(config),
        "blog": BlogChunker(config),
    }


def chunk_corpus(corpus: dict, config) -> dict:
    """Chunk every document, returning source -> list[Chunk]."""
    chunkers = build_chunkers(config)
    out: dict[str, list] = {}
    for source, docs in corpus.items():
        chunker = chunkers[source]
        chunks: list = []
        for doc in docs:
            chunks.extend(chunker.chunk(doc))
        out[source] = chunks
    return out
