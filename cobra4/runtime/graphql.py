"""GraphQL runtime for cobra4.

The `graphql` language plugin (`lang use graphql`) compiles inline
``graphql\"\"\"...\"\"\"`` literals into calls to :func:`parse_document`
defined here. The result is a `GraphQLDocument` that wraps either:

- a real ``graphql.language.DocumentNode`` from ``graphql-core`` if
  the package is installed, or
- a lightweight ``RawDocument`` placeholder that stores the SDL text
  so production deployments can still execute against a real GraphQL
  server while tests / offline runs don't require the heavy dep.

Server-side integration is intentionally out of scope for the MVP —
this gives users compile-time validation of GraphQL syntax + an entry
point to plug into whatever GraphQL library they choose.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Optional


@dataclass
class GraphQLDocument:
    """Parsed GraphQL document wrapper.

    ``ast`` is a ``graphql.language.DocumentNode`` when graphql-core is
    installed; otherwise it's ``None`` and ``text`` carries the SDL.
    """

    text: str
    ast: Optional[Any] = None

    def is_parsed(self) -> bool:
        return self.ast is not None

    def __str__(self) -> str:
        return self.text


def _try_import_graphql_core():
    try:
        from graphql import parse as gql_parse  # type: ignore

        return gql_parse
    except ImportError:
        return None


_PARSER = _try_import_graphql_core()


def parse_document(sdl: str) -> GraphQLDocument:
    """Parse a GraphQL SDL string. Returns a :class:`GraphQLDocument`.

    When graphql-core is available we validate the syntax up-front; a
    malformed string raises :class:`GraphQLSyntaxError` from the
    underlying library. Without graphql-core, we accept any string —
    runtime users plug in their own schema layer."""
    if _PARSER is None:
        return GraphQLDocument(text=sdl)
    ast = _PARSER(sdl)
    return GraphQLDocument(text=sdl, ast=ast)


__all__ = ["GraphQLDocument", "parse_document"]
