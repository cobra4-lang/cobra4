'''YAML literal plugin.

Activated by ``lang use yaml``.

Rewrites ``yaml"""..."""`` triple-string blocks (or ``yaml"..."`` for
short single-line YAML) into ``yaml_load(...)`` runtime calls that parse
the string at *load time* (i.e., when the generated Python module
imports). The result is a normal Python value (dict / list / str / int /
...), so user code can manipulate it freely.

Requires PyYAML at runtime: ``pip install pyyaml`` (or ``cobra4[yaml]``).

Example::

    config = yaml"""
        server:
          host: localhost
          port: 8080
    """
'''

from __future__ import annotations

import re

from cobra4.plugins.api import LanguagePlugin, register_plugin


# Triple-quoted block first (preferred for multi-line YAML).
_YAML_TRIPLE = re.compile(r'yaml"""(?P<body>.*?)"""', re.DOTALL)
# Single-quoted single-line YAML.
_YAML_SINGLE = re.compile(r'yaml"((?:[^"\\]|\\.)*)"')


def _transform(source: str) -> str:
    def _triple(m: re.Match) -> str:
        body = m.group("body")
        body_escaped = body.replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n")
        return f'yaml_load("{body_escaped}")'

    out = _YAML_TRIPLE.sub(_triple, source)

    def _single(m: re.Match) -> str:
        body = m.group(1)
        return f'yaml_load("{body}")'

    out = _YAML_SINGLE.sub(_single, out)
    return out


def yaml_load(text: str):
    """Runtime helper — parse a YAML string and return the Python value."""
    try:
        import yaml  # type: ignore
    except ImportError as e:  # pragma: no cover
        raise RuntimeError(
            "yaml plugin runtime requires `pip install pyyaml`."
        ) from e
    return yaml.safe_load(text)


def _preserve_for_format(source: str) -> tuple[str, list[tuple[str, str]]]:
    restorers: list[tuple[str, str]] = []
    counter = [0]

    def _swap_triple(m: re.Match) -> str:
        idx = counter[0]
        counter[0] += 1
        sentinel = f"_C4_YAML_PLACEHOLDER_{idx}"
        restorers.append((sentinel, m.group(0)))
        return sentinel

    body = _YAML_TRIPLE.sub(_swap_triple, source)
    body = _YAML_SINGLE.sub(_swap_triple, body)
    return body, restorers


register_plugin(
    LanguagePlugin(
        name="yaml",
        transform_source=_transform,
        runtime_module="cobra4.plugins.builtin.yaml",
        builtins=("yaml_load",),
        description='Inline YAML literals: `config = yaml"""...""",`',
        preserve_for_format=_preserve_for_format,
    )
)
