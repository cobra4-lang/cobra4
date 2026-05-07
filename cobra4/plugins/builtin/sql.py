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


# Match `sql { ... }` blocks. The body may contain newlines; we use a
# permissive non-greedy regex that bails out on a balanced closing `}`.
# True nested SQL with `{` inside is rare; we accept the limitation in M5.
_SQL_BLOCK = re.compile(r"sql\s*\{(?P<body>[^{}]*?)\}", re.DOTALL)


def _transform(source: str) -> str:
    def _repl(m: re.Match) -> str:
        body = m.group("body").strip()
        # Escape embedded triple quotes; we wrap in r"""..."""-equivalent.
        body_escaped = body.replace("\\", "\\\\").replace('"', '\\"')
        return f'sql_run("{body_escaped}")'

    return _SQL_BLOCK.sub(_repl, source)


def _preserve_for_format(source: str) -> tuple[str, list[tuple[str, str]]]:
    """For ``c4 fmt``: replace ``sql { ... }`` blocks with placeholder
    identifier calls so the body parses, and emit restorers so the
    formatter output can put them back verbatim.
    """
    restorers: list[tuple[str, str]] = []
    counter = [0]

    def _swap(m: re.Match) -> str:
        idx = counter[0]
        counter[0] += 1
        sentinel = f"_C4_SQL_PLACEHOLDER_{idx}()"
        restorers.append((sentinel, m.group(0)))
        return sentinel

    body = _SQL_BLOCK.sub(_swap, source)
    return body, restorers


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


def sql_run(query: str, *, params: dict | None = None, conn: object | None = None) -> list[dict]:
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
            log("sql.run", query=query, conn="<unconfigured>", note="call sql.configure() to execute")
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
