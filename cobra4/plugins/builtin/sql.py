"""Reference language plugin: SQL block embedding.

Registers ``sql`` as a usable plugin (``lang use sql``). Transforms::

    rows = sql {
        SELECT * FROM users WHERE age > 18
    }

into core cobra4::

    rows = sql_run("SELECT * FROM users WHERE age > 18")

The plugin pre-processes the source string before it reaches the main
parser. Only top-level ``sql { ... }`` (not nested) is rewritten — for
M5 this is enough, and the limitation matches what plugin authors will
encounter when targeting real grammars.
"""

from __future__ import annotations

import re

from cobra4.plugins.api import LanguagePlugin, register_plugin

# Match the opening of `sql { ... }`. Body matching is done with a
# brace-aware scanner (below) so SQL strings containing `{` (JSON
# predicates, format placeholders) don't terminate the block early.
_SQL_HEADER = re.compile(r"sql\s*\{")


def _find_sql_blocks(source: str):
    """Yield ``(start, end, body)`` for every ``sql { ... }`` block.

    The scanner tracks string state so a literal `{` or `}` inside an
    SQL string doesn't throw off brace balance. Used instead of a plain
    regex because the regex form
    (``sql\\s*\\{(?P<body>[^{}]*?)\\}``) silently mis-parses anything
    with braces in string literals."""
    i = 0
    while True:
        m = _SQL_HEADER.search(source, i)
        if not m:
            return
        body_start = m.end()
        j = body_start
        depth = 1
        while j < len(source) and depth > 0:
            c = source[j]
            if c == "\\":
                j += 2
                continue
            if c in ('"', "'"):
                q = c
                j += 1
                while j < len(source) and source[j] != q:
                    if source[j] == "\\":
                        j += 2
                    else:
                        j += 1
                j += 1
                continue
            if c == "{":
                depth += 1
            elif c == "}":
                depth -= 1
                if depth == 0:
                    yield m.start(), j + 1, source[body_start:j]
                    i = j + 1
                    break
            j += 1
        else:
            return  # unmatched brace — leave it for the main parser


def _transform(source: str) -> str:
    out: list[str] = []
    pos = 0
    for start, end, body in _find_sql_blocks(source):
        out.append(source[pos:start])
        body_clean = body.strip()
        body_escaped = body_clean.replace("\\", "\\\\").replace('"', '\\"')
        out.append(f'sql_run("{body_escaped}")')
        pos = end
    out.append(source[pos:])
    return "".join(out)


def _preserve_for_format(source: str) -> tuple[str, list[tuple[str, str]]]:
    """For ``c4 fmt``: replace ``sql { ... }`` blocks with placeholder
    identifier calls so the body parses, and emit restorers so the
    formatter output can put them back verbatim. Uses the same
    brace-aware scanner as ``_transform`` so SQL strings containing
    braces don't truncate the placeholder."""
    out: list[str] = []
    restorers: list[tuple[str, str]] = []
    pos = 0
    for idx, (start, end, _body) in enumerate(_find_sql_blocks(source)):
        sentinel = f"_C4_SQL_PLACEHOLDER_{idx}()"
        out.append(source[pos:start])
        out.append(sentinel)
        restorers.append((sentinel, source[start:end]))
        pos = end
    out.append(source[pos:])
    return "".join(out), restorers


_default_engine = None


def configure(url: str | None = None, **kwargs) -> object:
    """Configure the default SQLAlchemy engine used by ``sql_run``.

    ``url`` is a SQLAlchemy connection string. If omitted, reads
    ``COBRA4_SQL_URL`` env var. Kwargs are forwarded to
    ``sqlalchemy.create_engine``.

    Returns the engine object. Subsequent calls replace it.
    """
    import os

    global _default_engine
    try:
        import sqlalchemy as sa  # type: ignore
    except ImportError as e:  # pragma: no cover
        raise RuntimeError("sql plugin requires `pip install sqlalchemy`") from e
    if url is None:
        url = os.environ.get("COBRA4_SQL_URL")
    if not url:
        raise RuntimeError(
            "sql.configure() requires a connection URL (or COBRA4_SQL_URL env var). "
            "Examples: 'sqlite:///./app.db', "
            "'postgresql+psycopg://user:pass@host/db'"
        )
    _default_engine = sa.create_engine(url, **kwargs)
    return _default_engine


def sql_run(
    query: str, *, params: dict | None = None, conn: object | None = None
) -> list[dict]:
    """Execute a SQL query against the default engine (or ``conn``) and
    return rows as ``list[dict]``.

    The ``query`` may include named parameters in SQLAlchemy ``:name``
    style; pass values via ``params``. If neither :func:`configure` was
    called nor ``COBRA4_SQL_URL`` is set, falls back to logging the
    query (preserving the previous "demo" behavior).

    Returns an empty list for non-SELECT statements.
    """
    from cobra4.runtime.observe import log

    if conn is None and _default_engine is None:
        # Auto-configure from env if available, else log-only fallback.
        import os

        if os.environ.get("COBRA4_SQL_URL"):
            configure()
        else:
            log(
                "sql.run",
                query=query,
                conn="<unconfigured>",
                note="call sql.configure() to execute",
            )
            return []

    try:
        import sqlalchemy as sa  # type: ignore
    except ImportError as e:  # pragma: no cover
        raise RuntimeError("sql plugin requires `pip install sqlalchemy`") from e

    engine = conn or _default_engine
    log("sql.run", query=query[:200], params=params or {})
    with engine.connect() as cx:
        result = cx.execute(sa.text(query), params or {})
        if not result.returns_rows:
            cx.commit()
            return []
        return [dict(r._mapping) for r in result]


def query(sql: str, **params) -> list[dict]:
    """Convenience: ``query("SELECT * FROM users WHERE id = :id", id=42)``."""
    return sql_run(sql, params=params)


# Register at import time.
register_plugin(
    LanguagePlugin(
        name="sql",
        transform_source=_transform,
        runtime_module="cobra4.plugins.builtin.sql",
        builtins=("sql_run",),
        description="Embed raw SQL inside cobra4: `sql { SELECT ... }`",
        preserve_for_format=_preserve_for_format,
    )
)
