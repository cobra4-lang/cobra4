"""cobra4 language plugins.

Plugins extend the *language*, not just the runtime. They can register
new keyword blocks, new literal forms, or rewrite syntax to core cobra4.

A plugin is a Python module exposing the contract defined in
:mod:`cobra4.plugins.api`. Users opt in per-source-file with::

    lang use sql

at the top. The loader scans for such ``lang use`` directives during
parsing (before the main grammar consumes them) and activates the named
plugins.

M5 implementation pre-processes ``lang use`` directives via a regex pass
on the source — far simpler than threading plugin grammar extensions
through lark, while still giving users the full plugin programming model
(parse helpers, lowering, runtime). For full grammar extension, M6 will
introduce a dynamic Earley fallback.
"""

from cobra4.plugins.api import LanguagePlugin, register_plugin, get_plugin, list_plugins
from cobra4.plugins.loader import preprocess, parse_with_plugins

__all__ = [
    "LanguagePlugin",
    "register_plugin",
    "get_plugin",
    "list_plugins",
    "preprocess",
    "parse_with_plugins",
]
