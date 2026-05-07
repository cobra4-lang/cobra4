"""Lowering: cobra4 surface AST → cobra4 *core* AST.

In M1 the only structural rewrite is conceptual — most sugar is handled
inline by the codegen because the surface→core distance is small. We
keep this module as the canonical place for future rewrites (M2+).

Right now ``lower(module)`` returns the module unchanged. The transforms
that fall under "lowering" but are implemented in the codegen for M1:

- ``each x in xs in parallel {body}`` → call to ``_c4_parallel_for``
- ``a?.b`` → ``_c4_safe_attr(a, "b")``
- ``a ?? b`` → ``_c4_default(a, b)``
- string interpolation ``"hello {x}"`` → Python f-string ``f"hello {x}"``

Keeping these in the codegen avoids shuttling intermediate node
representations and makes the M1 generated Python easier to read.
"""

from __future__ import annotations

from cobra4 import ast_nodes as N


def lower(module: N.Module) -> N.Module:
    """Return ``module``, optionally rewritten. M1: identity."""
    return module
