"""Language plugin: Prometheus metrics.

Activates with ``lang use prom``. Adds three declarative forms — one
per metric type — that compile to runtime ``make_*`` calls:

- ``metric counter NAME labels=[...] doc="..."``
- ``metric histogram NAME labels=[...] buckets=[...] doc="..."``
- ``metric gauge NAME labels=[...] doc="..."``

Usage:

.. code-block:: cobra4

    lang use prom

    metric counter requests_total labels=["method", "status"] doc="HTTP requests"
    metric histogram request_ms labels=["route"] buckets=[10, 50, 100, 500] doc="Latency"

    requests_total.labels(method="GET", status="200").inc()
    request_ms.labels(route="/health").observe(7)

    fn metrics_handler(req) = prom.metrics_text()
    serve metrics_handler on :9090

The runtime delegates to ``prometheus_client`` if installed; otherwise
ships an in-process fallback with the same surface, so ``lang use prom``
works on every fresh ``pip install cobra4``.
"""

from __future__ import annotations

import re

from cobra4.plugins.api import LanguagePlugin, register_plugin
from cobra4.runtime.prom import (
    make_counter, make_histogram, make_gauge, metrics_text, reset_registry,
)


# Per metric kind we capture: name, optional labels=[...], optional
# buckets=[...] (histogram only), optional doc="...".
_METRIC_LINE = re.compile(
    r"""(?mx)
    ^[ \t]*
    metric[ \t]+
    (?P<kind>counter|histogram|gauge)[ \t]+
    (?P<name>[A-Za-z_][A-Za-z0-9_]*)
    (?P<rest>[^\n]*)
    $
    """,
)


def _split_kwargs(rest: str) -> list[str]:
    """``rest`` is a whitespace-separated sequence of ``key=value`` pairs.
    Values may be list literals (``[...]``), string literals
    (``"..."``), or bare numbers/identifiers. We split on the boundary
    that comes between two pairs — i.e. the next ``[a-z_]+=`` token,
    skipping over balanced ``[ ]`` and string contents.
    """
    pairs: list[str] = []
    i = 0
    n = len(rest)
    cur_start = 0
    while i < n:
        c = rest[i]
        if c in ('"', "'"):
            quote = c
            i += 1
            while i < n and rest[i] != quote:
                if rest[i] == "\\":
                    i += 2
                else:
                    i += 1
            i += 1
            continue
        if c == "[":
            depth = 1
            i += 1
            while i < n and depth > 0:
                if rest[i] == "[":
                    depth += 1
                elif rest[i] == "]":
                    depth -= 1
                elif rest[i] in ('"', "'"):
                    q = rest[i]
                    i += 1
                    while i < n and rest[i] != q:
                        if rest[i] == "\\":
                            i += 2
                        else:
                            i += 1
                i += 1
            continue
        if c.isspace():
            # If the next non-space prefix looks like `name=`, this is
            # the boundary between two pairs.
            j = i
            while j < n and rest[j].isspace():
                j += 1
            tail = rest[j:]
            ident_match = re.match(r"[A-Za-z_][A-Za-z0-9_]*\s*=", tail)
            if ident_match:
                pairs.append(rest[cur_start:i].strip())
                cur_start = j
                i = j
                continue
        i += 1
    last = rest[cur_start:].strip()
    if last:
        pairs.append(last)
    return pairs


def _transform(source: str) -> str:
    out_lines: list[str] = []
    for line in source.splitlines(keepends=True):
        m = _METRIC_LINE.match(line)
        if not m:
            out_lines.append(line)
            continue
        kind = m.group("kind")
        name = m.group("name")
        rest = m.group("rest").strip()
        # Drop the line's trailing newline for assembly; re-add at end.
        nl = "\n" if line.endswith("\n") else ""
        pairs = _split_kwargs(rest)
        kwargs = (", " + ", ".join(pairs)) if pairs else ""
        out_lines.append(f'{name} = _c4_prom_make_{kind}("{name}"{kwargs}){nl}')
    return "".join(out_lines)


# A namespace object so users can do `prom.metrics_text()` from cobra4.
class _Prom:
    """Convenience surface for cobra4 modules: ``prom.metrics_text()``."""
    metrics_text = staticmethod(metrics_text)
    reset_registry = staticmethod(reset_registry)


prom = _Prom()


# Re-export under codegen-friendly names so the rewritten source can
# call them without prefixes.
_c4_prom_make_counter = make_counter
_c4_prom_make_histogram = make_histogram
_c4_prom_make_gauge = make_gauge


register_plugin(
    LanguagePlugin(
        name="prom",
        transform_source=_transform,
        runtime_module="cobra4.plugins.builtin.prom",
        builtins=(
            "_c4_prom_make_counter",
            "_c4_prom_make_histogram",
            "_c4_prom_make_gauge",
            "prom",
        ),
        description="Prometheus `metric counter|histogram|gauge` declarations.",
    )
)


__all__ = [
    "_c4_prom_make_counter",
    "_c4_prom_make_histogram",
    "_c4_prom_make_gauge",
    "prom",
    "metrics_text",
    "reset_registry",
]
