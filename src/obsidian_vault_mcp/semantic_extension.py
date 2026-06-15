"""Semantic + hybrid vault search, packaged as a server Extension.

Adds two tools -- vault_semantic_search and vault_hybrid_search -- and owns the
SemanticIndex lifecycle, WITHOUT editing server.py. It plugs into the extension
seam (extensions.Extension / server.serve(extensions=...)) added upstream in #57.

Run it with the dedicated console entry point:

    vault-mcp-semantic            # = main() below -> serve([SemanticSearchExtension()])

The stock `vault-mcp` entry point is unaffected and runs with no extensions.

Gated on config.SEMANTIC_ENABLED: when 0/false the tools are not registered and
no index is built, so the server behaves exactly like the stock build. The DNS-
rebinding allowed hosts come from VAULT_MCP_ALLOWED_HOSTS (config, #34) -- nothing
is hardcoded here.
"""
from __future__ import annotations

import logging

from . import config
from .extensions import Extension

logger = logging.getLogger(__name__)


class SemanticSearchExtension(Extension):
    """Wires vault_semantic_search / vault_hybrid_search + the vector index."""

    def __init__(self) -> None:
        self.index = None

    def register_tools(self, mcp) -> None:
        if not config.SEMANTIC_ENABLED:
            logger.info("SEMANTIC_ENABLED is off; semantic/hybrid tools not registered")
            return
        from .tools.search import vault_semantic_search as _sem, vault_hybrid_search as _hyb

        @mcp.tool(
            name="vault_semantic_search",
            description=(
                "Search the vault by MEANING (vector/semantic search). Finds relevant notes even when "
                "they use different words or another language than the query (Bulgarian/English). Use "
                "when keyword search misses synonyms or paraphrases."
            ),
            annotations={"readOnlyHint": True, "destructiveHint": False, "idempotentHint": True, "openWorldHint": False},
        )
        def vault_semantic_search(query: str, path_prefix: str | None = None, max_results: int = 10) -> str:
            """Semantic search over vault content."""
            return _sem(query, path_prefix, max_results)

        @mcp.tool(
            name="vault_hybrid_search",
            description=(
                "Best general search: combines keyword (exact) and semantic (meaning) search with "
                "Reciprocal Rank Fusion. Prefer this over vault_search for most lookups. Falls back to "
                "keyword search if the semantic index is still building."
            ),
            annotations={"readOnlyHint": True, "destructiveHint": False, "idempotentHint": True, "openWorldHint": False},
        )
        def vault_hybrid_search(query: str, path_prefix: str | None = None, max_results: int = 10) -> str:
            """Hybrid keyword + semantic search."""
            return _hyb(query, path_prefix, max_results)

    def before_indexes_start(self, frontmatter_index) -> None:
        """Create the index instance and wire it into search.py before serving."""
        if not config.SEMANTIC_ENABLED:
            return
        from .semantic_index import SemanticIndex
        from .tools import search

        self.index = SemanticIndex()
        search.set_semantic_index(self.index)

    def after_indexes_start(self, frontmatter_index) -> None:
        """Build the vector index in a background thread (never blocks startup)."""
        if not config.SEMANTIC_ENABLED or self.index is None:
            return
        try:
            self.index.start()
            logger.info("Semantic index starting in background...")
        except Exception as e:  # never take the server down for a search add-on
            logger.warning("Semantic index could not start: %s", e)

    def shutdown(self) -> None:
        if self.index is not None:
            try:
                self.index.stop()
            except Exception:
                pass


def main() -> None:
    """Console entry point: run the server with the semantic extension loaded."""
    from .server import serve

    serve([SemanticSearchExtension()])


if __name__ == "__main__":
    main()
