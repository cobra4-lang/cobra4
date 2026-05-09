"""LLM agent runtime for cobra4.

Provides the ``_c4_llm_run(...)`` helper that the ``llm`` language
plugin compiles ``agent`` blocks into. An agent is an async function
that drives a tool-calling loop against an LLM provider until the
model emits a final answer.

Provider abstraction: pluggable via :func:`set_provider`. Default
provider tries to import ``anthropic`` and falls back to
:class:`MockProvider` for tests / offline runs.

Design notes
------------
- Tool functions can be sync or async. The runtime awaits coroutines.
- Tool schemas are inferred from Python signatures + type hints.
- Loop is bounded by ``max_iters`` to prevent runaway tool spirals.
- The whole loop is wrapped to surface errors as :class:`AgentError`
  with the partial conversation transcript attached for debugging.
"""

from __future__ import annotations

import asyncio
import inspect
import json
import os
from dataclasses import dataclass, field
from typing import Any, Callable, Optional


# ---------- public types ----------


class AgentError(Exception):
    """Raised when an agent loop fails. ``transcript`` carries the
    partial messages so the caller can inspect what happened."""

    def __init__(self, message: str, transcript: Optional[list] = None) -> None:
        super().__init__(message)
        self.transcript = transcript or []


@dataclass
class ToolCall:
    name: str
    arguments: dict[str, Any]
    tool_use_id: str = ""


@dataclass
class Response:
    """Provider-agnostic agent response. Either:
    - ``stop`` with final ``text``, or
    - ``tool_use`` with ``tool_calls`` to execute and continue the loop.
    """
    kind: str  # "stop" | "tool_use"
    text: str = ""
    tool_calls: list[ToolCall] = field(default_factory=list)


# ---------- providers ----------


class LLMProvider:
    """Interface every provider implements."""

    name: str = ""

    async def turn(
        self,
        *,
        model: str,
        system: Optional[str],
        messages: list[dict],
        tools: list[dict],
    ) -> Response:
        raise NotImplementedError


class MockProvider(LLMProvider):
    """Scripted provider for tests. Yields the next ``Response`` from
    ``scripted`` each time :meth:`turn` is invoked."""

    name = "mock"

    def __init__(self, scripted: list[Response]) -> None:
        self._scripted = list(scripted)
        self._call_log: list[dict] = []

    @property
    def calls(self) -> list[dict]:
        return list(self._call_log)

    async def turn(self, *, model, system, messages, tools) -> Response:
        self._call_log.append({
            "model": model, "system": system,
            "messages": list(messages), "tools": list(tools),
        })
        if not self._scripted:
            raise AgentError(
                "MockProvider exhausted — script ran out of responses",
                transcript=messages,
            )
        return self._scripted.pop(0)


class AnthropicProvider(LLMProvider):
    """Real Anthropic provider — needs ``pip install anthropic``."""

    name = "anthropic"

    def __init__(self, *, api_key: Optional[str] = None) -> None:
        try:
            import anthropic  # type: ignore
        except ImportError as e:
            raise AgentError(
                "anthropic provider requires `pip install anthropic`. "
                "Or call cobra4.runtime.llm.set_provider(MockProvider(...)) "
                "for tests."
            ) from e
        self._anthropic = anthropic
        self._client = anthropic.AsyncAnthropic(
            api_key=api_key or os.environ.get("ANTHROPIC_API_KEY"),
        )

    async def turn(self, *, model, system, messages, tools) -> Response:
        kwargs = {
            "model": model,
            "max_tokens": 4096,
            "messages": messages,
        }
        if system:
            kwargs["system"] = system
        if tools:
            kwargs["tools"] = tools
        resp = await self._client.messages.create(**kwargs)

        if resp.stop_reason == "tool_use":
            calls: list[ToolCall] = []
            for block in resp.content:
                if getattr(block, "type", None) == "tool_use":
                    calls.append(ToolCall(
                        name=block.name,
                        arguments=block.input or {},
                        tool_use_id=block.id,
                    ))
            return Response(kind="tool_use", tool_calls=calls)

        text = ""
        for block in resp.content:
            if getattr(block, "type", None) == "text":
                text += block.text
        return Response(kind="stop", text=text)


# ---------- registry ----------


_PROVIDER: LLMProvider = MockProvider(scripted=[])


def set_provider(provider: LLMProvider) -> None:
    """Override the active provider. Tests should pass a MockProvider;
    production code calls AnthropicProvider() at module load."""
    global _PROVIDER
    _PROVIDER = provider


def get_provider() -> LLMProvider:
    return _PROVIDER


# ---------- tool schema introspection ----------


def _tool_schema(fn: Callable) -> dict:
    """Build an Anthropic-compatible tool schema from a Python callable
    by inspecting its signature + type hints + docstring."""
    sig = inspect.signature(fn)
    properties: dict[str, dict] = {}
    required: list[str] = []
    for pname, p in sig.parameters.items():
        if pname in ("self", "cls"):
            continue
        if p.kind in (inspect.Parameter.VAR_POSITIONAL, inspect.Parameter.VAR_KEYWORD):
            continue
        json_type = _python_type_to_json_schema(p.annotation)
        properties[pname] = {"type": json_type}
        if p.default is inspect.Parameter.empty:
            required.append(pname)
    return {
        "name": fn.__name__,
        "description": (fn.__doc__ or "").strip().split("\n")[0] or fn.__name__,
        "input_schema": {
            "type": "object",
            "properties": properties,
            "required": required,
        },
    }


def _python_type_to_json_schema(t: Any) -> str:
    # Annotations may be lazy strings (e.g. under `from __future__ import
    # annotations`) — handle both forms.
    if isinstance(t, str):
        bare = t.split("[")[0].strip().lower()
        return {
            "int": "integer", "integer": "integer",
            "float": "number", "number": "number",
            "bool": "boolean", "boolean": "boolean",
            "list": "array", "tuple": "array",
            "dict": "object", "object": "object",
            "str": "string", "string": "string",
        }.get(bare, "string")
    if t in (int,):
        return "integer"
    if t in (float,):
        return "number"
    if t in (bool,):
        return "boolean"
    if t in (list,) or getattr(t, "__origin__", None) is list:
        return "array"
    if t in (dict,) or getattr(t, "__origin__", None) is dict:
        return "object"
    return "string"


# ---------- the agent runtime ----------


async def _c4_llm_run(
    *,
    agent_name: str,
    prompt: str,
    tools: list[Callable],
    model: str,
    system: Optional[str] = None,
    max_iters: int = 10,
) -> str:
    """Run an agent's tool-calling loop. Returns the final text response.

    This is the single entry point the ``llm`` language plugin compiles
    ``agent NAME(...) { ... }`` blocks into.

    ``prompt`` is the fully-rendered initial user message — cobra4's
    string interpolation handles parameter substitution at call time
    (``"{query}"`` in the source is replaced by the agent's ``query``
    parameter automatically), so the runtime never needs to do its own
    templating.
    """
    provider = _PROVIDER
    messages: list[dict] = [{"role": "user", "content": prompt}]

    tool_schemas = [_tool_schema(t) for t in tools]
    tool_by_name = {t.__name__: t for t in tools}

    for iteration in range(max_iters):
        try:
            resp = await provider.turn(
                model=model, system=system,
                messages=messages, tools=tool_schemas,
            )
        except AgentError:
            raise
        except Exception as e:
            raise AgentError(
                f"agent {agent_name!r} provider error on iteration {iteration}: {e}",
                transcript=messages,
            ) from e

        if resp.kind == "stop":
            return resp.text

        # tool_use: append assistant response, execute each tool,
        # append the tool_result, and continue.
        messages.append({
            "role": "assistant",
            "content": [
                {"type": "tool_use", "id": tc.tool_use_id, "name": tc.name, "input": tc.arguments}
                for tc in resp.tool_calls
            ],
        })
        results: list[dict] = []
        for tc in resp.tool_calls:
            fn = tool_by_name.get(tc.name)
            if fn is None:
                results.append({
                    "type": "tool_result",
                    "tool_use_id": tc.tool_use_id,
                    "is_error": True,
                    "content": f"unknown tool: {tc.name}",
                })
                continue
            try:
                out = fn(**tc.arguments)
                if inspect.isawaitable(out):
                    out = await out
                results.append({
                    "type": "tool_result",
                    "tool_use_id": tc.tool_use_id,
                    "content": json.dumps(out) if not isinstance(out, str) else out,
                })
            except Exception as tool_exc:  # noqa: BLE001
                results.append({
                    "type": "tool_result",
                    "tool_use_id": tc.tool_use_id,
                    "is_error": True,
                    "content": f"{type(tool_exc).__name__}: {tool_exc}",
                })
        messages.append({"role": "user", "content": results})

    raise AgentError(
        f"agent {agent_name!r} exceeded max_iters={max_iters} without stop",
        transcript=messages,
    )


__all__ = [
    "AgentError",
    "Response",
    "ToolCall",
    "LLMProvider",
    "MockProvider",
    "AnthropicProvider",
    "set_provider",
    "get_provider",
    "_c4_llm_run",
]
