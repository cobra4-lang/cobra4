"""Regex literal plugin.

Activated by ``lang use regex``.

Rewrites ``re"PATTERN"`` and ``re"PATTERN"FLAGS`` to ``re_compile(...)``
runtime calls. Flags are a subset of single-letter regex flags:
``i`` (ignore-case), ``m`` (multiline), ``s`` (dotall), ``x`` (verbose),
``a`` (ascii), ``u`` (unicode), ``l`` (locale).

Examples::

    p = re"[a-z]+"i
    if p.match("Hello") { ... }

    for m in re"\\d+".finditer(text) { print(m.group()) }
"""

from __future__ import annotations

import re

from cobra4.plugins.api import LanguagePlugin, register_plugin

_FLAG_MAP = {
    "i": "re.IGNORECASE",
    "m": "re.MULTILINE",
    "s": "re.DOTALL",
    "x": "re.VERBOSE",
    "a": "re.ASCII",
    "u": "re.UNICODE",
    "l": "re.LOCALE",
}


# Match `re"..."FLAGS` where flags is an optional run of letters from FLAG_MAP keys.
_RE_LITERAL = re.compile(r're"((?:[^"\\]|\\.)*)"([imsxaul]*)')


def _transform(source: str) -> str:
    def _repl(m: re.Match) -> str:
        pattern, flags = m.group(1), m.group(2)
        # Pattern may contain escapes already valid for regex; pass through.
        flag_expr = " | ".join(_FLAG_MAP[c] for c in flags) if flags else "0"
        # Escape the pattern as a Python string literal — simpler to wrap in r"..." if no quotes inside.
        if '"' not in pattern and "\\" in pattern:
            pat_lit = f'r"{pattern}"'
        else:
            pat_lit = '"' + pattern.replace("\\", "\\\\").replace('"', '\\"') + '"'
        return f"re_compile({pat_lit}, {flag_expr})"

    return _RE_LITERAL.sub(_repl, source)


def re_compile(pattern: str, flags: int = 0):
    """Runtime helper — alias for ``re.compile`` so plugin code stays minimal."""
    return re.compile(pattern, flags)


# Re-export `re` for runtime references like `re.IGNORECASE`.
import re as re  # noqa: E402,F401  (intentional re-export)


def _preserve_for_format(source: str) -> tuple[str, list[tuple[str, str]]]:
    restorers: list[tuple[str, str]] = []
    counter = [0]

    def _swap(m: re.Match) -> str:
        idx = counter[0]
        counter[0] += 1
        sentinel = f"_C4_REGEX_PLACEHOLDER_{idx}"
        restorers.append((sentinel, m.group(0)))
        return sentinel

    body = _RE_LITERAL.sub(_swap, source)
    return body, restorers


register_plugin(
    LanguagePlugin(
        name="regex",
        transform_source=_transform,
        runtime_module="cobra4.plugins.builtin.regex",
        builtins=("re_compile", "re"),
        description='Embed regex literals: `p = re"[a-z]+"i`',
        preserve_for_format=_preserve_for_format,
    )
)
