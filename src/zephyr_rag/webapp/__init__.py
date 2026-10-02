"""Local chat application (stdlib HTTP server) for the RAG pipeline."""

from .server import create_app_state, serve

__all__ = ["serve", "create_app_state"]
