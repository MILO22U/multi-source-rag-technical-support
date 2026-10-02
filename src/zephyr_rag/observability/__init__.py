"""Per-query tracing and source attribution (Requirement 6)."""

from .trace import QueryTrace, TraceWriter

__all__ = ["QueryTrace", "TraceWriter"]
