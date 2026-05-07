"""Plugin contract — what a language plugin must provide."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable, Optional


@dataclass
class LanguagePlugin:
    """Contract for a cobra4 language plugin.

    Required:
      - ``name``: unique key matching ``lang use NAME`` in source files.
      - ``transform_source``: pre-processing function. Takes the raw source
        string with all ``lang use`` directives stripped, returns
        transformed cobra4 source ready for the main parser.
        It MAY look for plugin-specific syntax (e.g. ``sql { ... }``) and
        rewrite them to core cobra4 calls.

    Optional:
      - ``runtime_module``: dotted Python path. The codegen adds an
        import for it so the transformed source can call into runtime
        helpers.
      - ``builtins``: extra names to whitelist in the resolver. Tooling
        (``c4 check``, LSP) uses this so transformed-source identifiers
        introduced by the plugin (``sql_run``, ``re_compile``, …) don't
        get reported as undefined.
      - ``preserve_for_format``: optional callable. When ``c4 fmt`` runs,
        it asks each plugin to extract its source-level constructs (e.g.
        ``sql { ... }`` blocks) and substitute placeholders, so the body
        parses with the bare cobra4 grammar. After formatting, the
        callable's reverse hook restores the originals. The signature is
        ``preserve_for_format(src) -> (src_with_sentinels, restore_fn)``.
        If absent, ``transform_source`` runs as usual and the formatter
        emits the post-transform code (acceptable but lossy).
      - ``description``: free-text. Surfaced by ``c4 plugin list``.
    """

    name: str
    transform_source: Callable[[str], str]
    runtime_module: Optional[str] = None
    builtins: tuple[str, ...] = ()
    description: str = ""
    preserve_for_format: Optional[Callable[[str], "tuple"]] = None


_registry: dict[str, LanguagePlugin] = {}


def register_plugin(plugin: LanguagePlugin) -> None:
    """Register a plugin so it can be referenced via ``lang use NAME``."""
    _registry[plugin.name] = plugin


def get_plugin(name: str) -> Optional[LanguagePlugin]:
    return _registry.get(name)


def list_plugins() -> list[LanguagePlugin]:
    return list(_registry.values())
