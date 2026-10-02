"""Context-header construction -- repairing the context that chunking severs.

Chunking a document necessarily cuts passages away from the information that
makes them interpretable. A chunk reading "Set this to false to disable it" is
perfectly retrievable and completely useless.

The fix is cheap and deterministic: prepend a breadcrumb to the text *before*
indexing, so the chunk carries its own context into both the lexical and the
dense index.

    Zephyr Docs v3.2 > Retries and backoff > Configuring backoff
    Zephyr Forum > solved > "Jobs retry forever on 429" > accepted answer by staff
    Zephyr Blog > 2025-08-12 (v2.4 era) > "Scaling to a million jobs a day"

Three fields are load-bearing rather than decorative:

* **version** -- most apparent contradictions in this corpus are the same claim
  scoped to different versions; without it the resolver cannot scope.
* **date** (blog) -- a post is a snapshot of a moment. Strip the date and a
  v2-era claim becomes a timeless one.
* **authority** (forum) -- "accepted answer by staff" versus "answer by user"
  is exactly the signal that decides a misconception conflict.

An optional LLM variant (``chunking.contextual.llm_blurb``) adds a generated
situating sentence per chunk, which is the higher-quality form of the same idea;
the deterministic breadcrumb is the default so results reproduce offline.
"""

from __future__ import annotations

from typing import Any

__all__ = ["build_header", "docs_header", "forum_header", "blog_header"]

_SEP = " > "
_BRAND = "Zephyr"


def docs_header(metadata: dict[str, Any], section_path: list[str], *, include_version: bool = True) -> str:
    """``Zephyr Docs v3.2 > Retries and backoff > Configuring backoff``."""
    parts = [f"{_BRAND} Docs"]
    version = metadata.get("version")
    if include_version and version:
        parts[0] = f"{_BRAND} Docs v{version}"
    doc_type = metadata.get("doc_type")
    if doc_type and doc_type != "reference":
        parts.append(str(doc_type))
    title = metadata.get("title")
    if title:
        parts.append(str(title))
    parts.extend(p for p in section_path if p)
    return _SEP.join(parts)


def forum_header(
    thread_meta: dict[str, Any],
    post: dict[str, Any],
    *,
    include_authority: bool = True,
    include_version: bool = True,
) -> str:
    """``Zephyr Forum > solved > "title" > accepted answer by staff``.

    The authority clause is included in the *indexed* text deliberately: the
    cross-encoder sees it, which lets reranking distinguish a staff-authored
    accepted answer from a speculative reply on textual evidence alone.
    """
    parts = [f"{_BRAND} Forum"]
    status = thread_meta.get("status")
    if status:
        parts.append(str(status))
    version = thread_meta.get("product_version_mentioned")
    if include_version and version:
        parts.append(f"v{version}")
    title = thread_meta.get("title")
    if title:
        parts.append(f'"{title}"')

    if include_authority:
        role = str(post.get("author_role", "user"))
        if post.get("parent_post_id") is None:
            descriptor = "original question"
        elif post.get("is_accepted"):
            descriptor = f"accepted answer by {role}"
        else:
            votes = post.get("votes", 0)
            descriptor = f"answer by {role} ({votes} votes)"
        parts.append(descriptor)
    return _SEP.join(parts)


def blog_header(
    metadata: dict[str, Any],
    *,
    include_date: bool = True,
    include_version: bool = True,
    include_authority: bool = True,
) -> str:
    """``Zephyr Blog > 2025-08-12 (v2.4 era) > staff > "Scaling to 1M jobs"``.

    Date and era-version are the whole point. A blog chunk without them is a
    claim with no scope, and the contradiction resolver has no grounds to prefer
    current documentation over a three-year-old post.
    """
    parts = [f"{_BRAND} Blog"]
    published = metadata.get("published_at")
    era = metadata.get("product_version_at_time")
    if include_date and published:
        parts.append(f"{published} (v{era} era)" if (include_version and era) else str(published))
    elif include_version and era:
        parts.append(f"v{era} era")
    if include_authority:
        role = metadata.get("author_role")
        if role:
            parts.append(str(role))
    title = metadata.get("title")
    if title:
        parts.append(f'"{title}"')
    return _SEP.join(parts)


def build_header(source: str, config, **kwargs: Any) -> str:
    """Dispatch to the per-source header builder, honouring config flags."""
    if not config.get("chunking.contextual.enabled", True):
        return ""
    include_version = bool(config.get("chunking.contextual.include_version", True))
    include_date = bool(config.get("chunking.contextual.include_date", True))
    include_authority = bool(config.get("chunking.contextual.include_authority", True))

    if source == "docs":
        return docs_header(
            kwargs["metadata"], kwargs.get("section_path", []), include_version=include_version
        )
    if source == "forum":
        return forum_header(
            kwargs["thread_meta"],
            kwargs["post"],
            include_authority=include_authority,
            include_version=include_version,
        )
    if source == "blog":
        return blog_header(
            kwargs["metadata"],
            include_date=include_date,
            include_version=include_version,
            include_authority=include_authority,
        )
    return ""
