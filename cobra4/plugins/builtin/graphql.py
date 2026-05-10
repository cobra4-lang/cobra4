"""Language plugin: GraphQL schema / query literals.

Activates with ``lang use graphql``. Adds an inline literal form:

.. code-block:: cobra4

    lang use graphql

    schema_doc = graphql\"\"\"
        type User {
            id: ID!
            email: String!
        }
        type Query {
            user(id: ID!): User
        }
    \"\"\"

    log("schema parsed", text=schema_doc.text)

The plugin rewrites each ``graphql\"\"\"...\"\"\"`` block to a
``_c4_graphql_parse(\"...\")`` call. The runtime delegates to
``graphql-core`` when installed (real syntax validation); otherwise it
stores the SDL text for later use by a server-side library.
"""

from __future__ import annotations

import re

from cobra4.plugins.api import LanguagePlugin, register_plugin
from cobra4.runtime.graphql import parse_document as _c4_graphql_parse


# `graphql"""..."""` — non-greedy match across newlines.
_GRAPHQL_TRIPLE = re.compile(r'graphql"""(?P<body>.*?)"""', re.DOTALL)


def _transform(source: str) -> str:
    def _repl(m: re.Match) -> str:
        body = m.group("body")
        # Cobra4 always interpolates `{name}` in `"""..."""` literals,
        # but SDL is full of literal `{` `}`. Double them so they
        # survive cobra4's interpolation pass and re-emerge as single
        # braces. Also escape backslashes and any embedded triple-double
        # quote.
        body = body.replace("\\", "\\\\")
        body = body.replace("{", "{{").replace("}", "}}")
        body = body.replace('"""', '\\"""')
        return f'_c4_graphql_parse("""{body}""")'
    return _GRAPHQL_TRIPLE.sub(_repl, source)


register_plugin(
    LanguagePlugin(
        name="graphql",
        transform_source=_transform,
        runtime_module="cobra4.plugins.builtin.graphql",
        builtins=("_c4_graphql_parse",),
        description='GraphQL SDL literal: `graphql"""type Foo { ... }"""`.',
    )
)


__all__ = ["_c4_graphql_parse"]
