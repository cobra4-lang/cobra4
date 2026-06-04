"""Plugin loader: scans `lang use NAME` directives and applies plugins.

The flow:

1. Scan the source for ``lang use NAME`` lines (only at the top of file,
   before any non-blank/non-comment statement). Strip them from the source.
2. For each name, look up the plugin in the registry. Auto-import a
   conventional Python package: try ``cobra4.plugins.builtin.<name>``
   first, then ``cobra4_lang_<name>`` from PyPI.
3. Apply each plugin's ``transform_source`` in declaration order.
4. Return the transformed source, the active plugins, and the source map
   (line offsets so error reporting remains accurate).

The result feeds into the regular ``cobra4.parser.parse``.
"""

from __future__ import annotations

import importlib
import re
from dataclasses import dataclass

from cobra4.plugins.api import LanguagePlugin, get_plugin

_LANG_USE_RE = re.compile(r"^\s*lang\s+use\s+([A-Za-z_][A-Za-z0-9_]*)\s*$")


@dataclass
class PreprocessResult:
    source: str
    plugins: list[LanguagePlugin]
    line_offset: int  # how many leading lines were stripped
    extra_builtins: tuple[str, ...] = ()  # collected from plugin.builtins


def _autoload(name: str) -> LanguagePlugin | None:
    plugin = get_plugin(name)
    if plugin is not None:
        return plugin
    # Try built-in path.
    try:
        importlib.import_module(f"cobra4.plugins.builtin.{name}")
    except ModuleNotFoundError:
        pass
    plugin = get_plugin(name)
    if plugin is not None:
        return plugin
    # Try external PyPI package convention.
    try:
        importlib.import_module(f"cobra4_lang_{name}")
    except ModuleNotFoundError:
        pass
    return get_plugin(name)


def preprocess(source: str) -> PreprocessResult:
    lines = source.splitlines(keepends=True)
    plugin_names: list[str] = []
    body_start = 0
    for i, line in enumerate(lines):
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or stripped.startswith("//"):
            continue
        m = _LANG_USE_RE.match(line.rstrip("\n"))
        if m:
            plugin_names.append(m.group(1))
            continue
        body_start = i
        break
    else:
        body_start = len(lines)

    plugins: list[LanguagePlugin] = []
    for name in plugin_names:
        p = _autoload(name)
        if p is None:
            raise ValueError(
                f"unknown language plugin '{name}' — install cobra4_lang_{name} "
                f"or register it via cobra4.plugins.register_plugin(...)."
            )
        plugins.append(p)

    # Replace `lang use ...` lines with blank lines to preserve line numbers.
    cleaned = []
    for line in lines:
        if _LANG_USE_RE.match(line.rstrip("\n")):
            cleaned.append("\n")
        else:
            cleaned.append(line)
    transformed = "".join(cleaned)

    for plugin in plugins:
        transformed = plugin.transform_source(transformed)

    extra_builtins: list[str] = []
    for p in plugins:
        extra_builtins.extend(p.builtins or ())

    return PreprocessResult(
        source=transformed,
        plugins=plugins,
        line_offset=0,
        extra_builtins=tuple(extra_builtins),
    )


def preserve_plugin_constructs(source: str) -> tuple[str, list, list[LanguagePlugin]]:
    """Strip plugin directives + extract preservable plugin constructs.

    Used by ``c4 fmt``: lets the formatter parse the bare cobra4 body
    (without ``lang use`` lines or plugin-specific syntax like
    ``sql { ... }``), then re-stitches the output afterwards.

    Returns ``(body_with_sentinels, restorers, plugins)`` where:

    - ``body_with_sentinels`` is parseable cobra4 source.
    - ``restorers`` is a list of ``(sentinel, original)`` to substitute
      back into the formatted output.
    - ``plugins`` is the active plugin list (for ``lang use`` re-prepend).
    """
    lines = source.splitlines(keepends=True)
    plugin_names: list[str] = []
    body_idx = len(lines)
    for i, line in enumerate(lines):
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or stripped.startswith("//"):
            continue
        m = _LANG_USE_RE.match(line.rstrip("\n"))
        if m:
            plugin_names.append(m.group(1))
            continue
        body_idx = i
        break

    plugins: list[LanguagePlugin] = []
    for name in plugin_names:
        p = _autoload(name)
        if p is None:
            raise ValueError(f"unknown language plugin '{name}'")
        plugins.append(p)

    body_lines = lines[body_idx:]
    body = "".join(body_lines)
    restorers: list[tuple[str, str]] = []
    sentinel_idx = 0
    for plugin in plugins:
        if plugin.preserve_for_format is None:
            # No preserver: fall back to running transform_source so the body
            # at least parses. The plugin-specific syntax won't survive
            # formatting in this case.
            body = plugin.transform_source(body)
            continue
        body, more_restorers = plugin.preserve_for_format(body)
        for s, o in more_restorers:
            restorers.append((s, o))
            sentinel_idx += 1
    return body, restorers, plugins


def parse_with_plugins(source: str, source_path: str | None = None):
    """Convenience: preprocess then parse, returning (Module, [plugins])."""
    from cobra4.parser import parse  # avoid cycle

    res = preprocess(source)
    module = parse(res.source, source_path=source_path)
    return module, res.plugins
