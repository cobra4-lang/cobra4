"""Language plugin: LLM agents.

Activates with ``lang use llm``. Adds ``agent NAME(args) -> ret`` blocks
that compile to async functions running a tool-calling loop against an
LLM provider. Inside each block the user declares: ``tools: [fn, ...]``,
``model: "..."``, ``max_iters: N``, ``system: "..."``, and a ``prompt``
template (triple-quoted string).

The plugin rewrites each ``agent`` block to an ``async fn`` whose body
calls ``_c4_llm_run(...)``. The fn signature is preserved verbatim, so
type-checking and effect annotations on agent declarations work the
same way they do on hand-written ``async fn``.

Provider selection is runtime-side (see :mod:`cobra4.runtime.llm`):
default mock provider for tests; configure
:class:`cobra4.runtime.llm.AnthropicProvider` at the start of your
program for production.
"""

from __future__ import annotations

import re

from cobra4.plugins.api import LanguagePlugin, register_plugin
from cobra4.runtime.llm import _c4_llm_run, AgentError, MockProvider, set_provider


# Header line, e.g.  `agent customer_support(query: str) -> str {`
_AGENT_HEADER = re.compile(
    r"agent\s+(?P<name>[A-Za-z_][A-Za-z0-9_]*)\s*"
    r"\((?P<params>[^)]*)\)\s*"
    r"(?:->\s*(?P<ret>[A-Za-z_][\w\[\],\s\?]*))?\s*\{",
)


def _find_agent_blocks(source: str):
    """Yield ``(start, end, header_match, body)`` for every top-level
    ``agent NAME(...) { ... }`` block. Brace counting is string-aware
    (skips contents of "" and ''' triple quoted strings)."""
    i = 0
    while i < len(source):
        m = _AGENT_HEADER.search(source, i)
        if not m:
            return
        body_start = m.end()  # position right after the opening `{`
        depth = 1
        j = body_start
        while j < len(source) and depth > 0:
            c = source[j]
            if c == "\\":
                j += 2
                continue
            if source[j:j + 3] in ('"""', "'''"):
                quote = source[j:j + 3]
                j += 3
                while j < len(source) and source[j:j + 3] != quote:
                    if source[j] == "\\":
                        j += 2
                    else:
                        j += 1
                j += 3
                continue
            if c in ('"', "'"):
                quote = c
                j += 1
                while j < len(source) and source[j] != quote:
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
                    yield m.start(), j + 1, m, source[body_start:j]
                    i = j + 1
                    break
            j += 1
        else:
            return  # unmatched brace — let the parser report it cleanly


# Field parsers inside the body.
_FIELD_TOOLS = re.compile(r"(?ms)^\s*tools\s*:\s*(\[[^\]]*\])")
_FIELD_MODEL = re.compile(r"""(?ms)^\s*model\s*:\s*("[^"]*"|'[^']*')""")
_FIELD_SYSTEM = re.compile(
    r'''(?ms)^\s*system\s*:\s*(?P<q>"""|'''  + r"'''" + r'''|"|')(?P<body>.*?)(?P=q)''',
)
_FIELD_PROMPT = re.compile(
    r'''(?ms)^\s*prompt\s*(?P<q>"""|'''  + r"'''" + r'''|"|')(?P<body>.*?)(?P=q)''',
)
_FIELD_MAX_ITERS = re.compile(r"(?m)^\s*max_iters\s*:\s*(\d+)")


def _extract_fields(body: str) -> dict:
    out: dict = {"system": None, "max_iters": "10"}

    if (m := _FIELD_TOOLS.search(body)):
        out["tools"] = m.group(1).strip()
    else:
        out["tools"] = "[]"

    if (m := _FIELD_MODEL.search(body)):
        out["model"] = m.group(1).strip()
    else:
        out["model"] = '"claude-sonnet-4-6"'

    if (m := _FIELD_SYSTEM.search(body)):
        out["system"] = m.group("body")

    if (m := _FIELD_PROMPT.search(body)):
        out["prompt"] = m.group("body")
    else:
        out["prompt"] = ""

    if (m := _FIELD_MAX_ITERS.search(body)):
        out["max_iters"] = m.group(1)

    return out


def _quote_for_cobra4(text: str) -> str:
    """Render ``text`` as a plain triple-quoted cobra4 string.

    Cobra4 always interpolates ``{name}`` placeholders against the
    enclosing scope, which is exactly what we want for prompts: the
    user writes ``{query}`` and cobra4 substitutes the agent's
    ``query`` parameter at call time.

    We escape backslashes (so newlines stay literal) and any embedded
    triple-double-quotes (rare, but they would terminate the string
    literal early)."""
    escaped = text.replace("\\", "\\\\").replace('"""', '\\"""')
    return '"""' + escaped + '"""'


def _transform(source: str) -> str:
    """Rewrite every ``agent NAME(...) { ... }`` block."""
    out = []
    pos = 0
    for start, end, m, body in _find_agent_blocks(source):
        out.append(source[pos:start])
        name = m.group("name")
        params = m.group("params") or ""
        ret = m.group("ret")
        ret_clause = f" -> {ret.strip()}" if ret else ""

        fields = _extract_fields(body)

        prompt_lit = _quote_for_cobra4(fields["prompt"])
        system_lit = (
            _quote_for_cobra4(fields["system"]) if fields["system"] is not None
            else "None"
        )

        # Note: the prompt literal contains `{name}` placeholders which
        # cobra4's regular string interpolation will resolve against the
        # async fn's parameters at call time — so we don't pass `args`
        # to the runtime; the prompt arrives fully rendered.
        rewritten = (
            f"async fn {name}({params}){ret_clause} {{\n"
            f"    return await _c4_llm_run(\n"
            f'        agent_name="{name}",\n'
            f"        prompt={prompt_lit},\n"
            f"        tools={fields['tools']},\n"
            f"        model={fields['model']},\n"
            f"        system={system_lit},\n"
            f"        max_iters={fields['max_iters']},\n"
            f"    )\n"
            f"}}\n"
        )
        out.append(rewritten)
        pos = end
    out.append(source[pos:])
    return "".join(out)


# Register at import time.
register_plugin(
    LanguagePlugin(
        name="llm",
        transform_source=_transform,
        runtime_module="cobra4.plugins.builtin.llm",
        builtins=("_c4_llm_run", "AgentError"),
        description="LLM agent declarations: `agent NAME(...) { tools, model, prompt }`",
    )
)


# Re-export the helpers so cobra4 modules can import via `lang use llm`.
__all__ = ["_c4_llm_run", "AgentError", "MockProvider", "set_provider"]
