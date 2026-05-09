"""Tests for the LLM agent plugin and runtime.

The runtime is exercised with :class:`MockProvider` (no API calls)
so tests are deterministic, fast, and offline.
"""

from __future__ import annotations

import asyncio
import inspect
import subprocess
import sys
from pathlib import Path

import pytest

from cobra4.plugins.builtin.llm import _transform, _find_agent_blocks, _extract_fields
from cobra4.runtime.llm import (
    AgentError, MockProvider, Response, ToolCall, _c4_llm_run,
    _tool_schema, set_provider,
)


# ---------- plugin source transform ----------


def test_transform_replaces_agent_block_with_async_fn() -> None:
    src = (
        'lang use llm\n'
        'agent greet(name: str) -> str {\n'
        '    tools: []\n'
        '    model: "claude-sonnet-4-6"\n'
        '    prompt "Hi {name}"\n'
        '}\n'
    )
    out = _transform(src)
    assert "async fn greet(name: str) -> str" in out
    assert "_c4_llm_run" in out
    assert 'agent_name="greet"' in out
    assert "model=" in out
    assert "tools=[]" in out


def test_transform_handles_multiple_agents() -> None:
    src = (
        'agent a() { prompt "x" }\n'
        'agent b() { prompt "y" }\n'
    )
    out = _transform(src)
    assert "async fn a()" in out
    assert "async fn b()" in out


def test_transform_preserves_non_agent_code_verbatim() -> None:
    src = "fn helper() = 42\n\nagent x() { prompt \"hi\" }\n\nfn other() = 7\n"
    out = _transform(src)
    assert "fn helper() = 42" in out
    assert "fn other() = 7" in out


def test_transform_handles_braces_inside_strings() -> None:
    """A `{` inside a quoted string in the agent body must not throw off
    the brace-balance scanner that finds the closing `}`."""
    src = (
        'agent x() {\n'
        '    model: "x { y }"\n'
        '    prompt "hi"\n'
        '}\n'
        'after = 1\n'
    )
    out = _transform(src)
    # The `after = 1` must still be there, after the rewritten async fn.
    assert "after = 1" in out


def test_extract_fields_parses_default_max_iters() -> None:
    """Without `max_iters: ...`, the default is 10."""
    body = '    tools: []\n    prompt "x"\n'
    f = _extract_fields(body)
    assert f["max_iters"] == "10"


def test_extract_fields_parses_explicit_max_iters() -> None:
    body = '    max_iters: 7\n    prompt "x"\n'
    f = _extract_fields(body)
    assert f["max_iters"] == "7"


def test_find_agent_blocks_returns_empty_for_non_agent_code() -> None:
    blocks = list(_find_agent_blocks("fn x() = 1\n"))
    assert blocks == []


# ---------- runtime: MockProvider direct ----------


def test_mock_provider_returns_scripted_response() -> None:
    p = MockProvider(scripted=[Response(kind="stop", text="hi")])
    out = asyncio.run(p.turn(model="m", system=None, messages=[], tools=[]))
    assert out.kind == "stop"
    assert out.text == "hi"


def test_mock_provider_records_call_log() -> None:
    p = MockProvider(scripted=[Response(kind="stop", text="x")])
    asyncio.run(p.turn(model="m", system="s", messages=[{"role": "user", "content": "q"}], tools=[]))
    assert len(p.calls) == 1
    assert p.calls[0]["model"] == "m"
    assert p.calls[0]["system"] == "s"


def test_mock_provider_raises_when_exhausted() -> None:
    p = MockProvider(scripted=[])
    with pytest.raises(AgentError, match="exhausted"):
        asyncio.run(p.turn(model="m", system=None, messages=[], tools=[]))


# ---------- _c4_llm_run agent loop ----------


def test_agent_loop_returns_stop_text() -> None:
    set_provider(MockProvider(scripted=[Response(kind="stop", text="answer")]))
    out = asyncio.run(_c4_llm_run(
        agent_name="t", prompt="?", tools=[],
        model="m",
    ))
    assert out == "answer"


def test_agent_loop_executes_tool_then_stops() -> None:
    def echo(message: str) -> str:
        return f"echoed: {message}"
    set_provider(MockProvider(scripted=[
        Response(kind="tool_use", tool_calls=[
            ToolCall(name="echo", arguments={"message": "hi"}, tool_use_id="t1"),
        ]),
        Response(kind="stop", text="done"),
    ]))
    out = asyncio.run(_c4_llm_run(
        agent_name="t", prompt="?", tools=[echo], model="m",
    ))
    assert out == "done"


def test_agent_loop_appends_tool_result_to_messages() -> None:
    """After a tool runs, the next provider call must see the tool_result
    in the messages so the LLM has context."""
    captured_messages: list = []

    class CapturingProvider(MockProvider):
        async def turn(self, *, model, system, messages, tools):
            captured_messages.append([dict(m) for m in messages])
            return await super().turn(model=model, system=system, messages=messages, tools=tools)

    def echo(x: str) -> str:
        return "OK"

    set_provider(CapturingProvider(scripted=[
        Response(kind="tool_use", tool_calls=[
            ToolCall(name="echo", arguments={"x": "y"}, tool_use_id="t1"),
        ]),
        Response(kind="stop", text="done"),
    ]))
    asyncio.run(_c4_llm_run(
        agent_name="t", prompt="?", tools=[echo], model="m",
    ))
    # First turn: 1 message (the prompt)
    # Second turn: 1 prompt + 1 assistant tool_use + 1 tool_result = 3
    assert len(captured_messages[0]) == 1
    assert len(captured_messages[1]) == 3


def test_agent_loop_handles_async_tool() -> None:
    async def slow_lookup(key: str) -> str:
        await asyncio.sleep(0)
        return f"value-of-{key}"
    set_provider(MockProvider(scripted=[
        Response(kind="tool_use", tool_calls=[
            ToolCall(name="slow_lookup", arguments={"key": "K"}, tool_use_id="t1"),
        ]),
        Response(kind="stop", text="resolved"),
    ]))
    out = asyncio.run(_c4_llm_run(
        agent_name="t", prompt="?", tools=[slow_lookup], model="m",
    ))
    assert out == "resolved"


def test_agent_loop_handles_unknown_tool_gracefully() -> None:
    """If the LLM hallucinates a tool name we don't have, return an
    error result to the LLM so it can recover (the loop keeps going
    rather than crashing)."""
    set_provider(MockProvider(scripted=[
        Response(kind="tool_use", tool_calls=[
            ToolCall(name="not_a_real_tool", arguments={}, tool_use_id="t1"),
        ]),
        Response(kind="stop", text="oh well"),
    ]))
    out = asyncio.run(_c4_llm_run(
        agent_name="t", prompt="?", tools=[], model="m",
    ))
    assert out == "oh well"


def test_agent_loop_surfaces_tool_exceptions_to_llm_not_caller() -> None:
    """A tool that raises shouldn't kill the loop — the LLM gets the
    error and decides what to do next."""
    def boom(x: int) -> int:
        raise ValueError("nope")

    set_provider(MockProvider(scripted=[
        Response(kind="tool_use", tool_calls=[
            ToolCall(name="boom", arguments={"x": 1}, tool_use_id="t1"),
        ]),
        Response(kind="stop", text="recovered"),
    ]))
    out = asyncio.run(_c4_llm_run(
        agent_name="t", prompt="?", tools=[boom], model="m",
    ))
    assert out == "recovered"


def test_agent_loop_max_iters_raises() -> None:
    """If the LLM keeps emitting tool_use forever, abort cleanly."""
    def echo(x: str) -> str:
        return "x"
    set_provider(MockProvider(scripted=[
        Response(kind="tool_use", tool_calls=[
            ToolCall(name="echo", arguments={"x": "y"}, tool_use_id=f"t{i}"),
        ])
        for i in range(20)
    ]))
    with pytest.raises(AgentError, match="exceeded max_iters"):
        asyncio.run(_c4_llm_run(
            agent_name="t", prompt="?", tools=[echo], model="m", max_iters=3,
        ))


# ---------- tool schema introspection ----------


def test_tool_schema_extracts_name_and_required_params() -> None:
    def lookup(key: str, optional: int = 0) -> str:
        """Look up a key."""
        return ""
    schema = _tool_schema(lookup)
    assert schema["name"] == "lookup"
    assert schema["description"] == "Look up a key."
    assert "key" in schema["input_schema"]["properties"]
    assert schema["input_schema"]["properties"]["key"]["type"] == "string"
    assert schema["input_schema"]["properties"]["optional"]["type"] == "integer"
    assert schema["input_schema"]["required"] == ["key"]


def test_tool_schema_uses_function_name_when_doc_missing() -> None:
    def f(x: str) -> str:
        return x
    schema = _tool_schema(f)
    assert schema["description"] == "f"


# ---------- end-to-end via cobra4 source ----------


def _run_c4(tmp_path: Path, src: str) -> tuple[int, str, str]:
    f = tmp_path / "prog.c4"
    f.write_text(src)
    proc = subprocess.run(
        [sys.executable, "-m", "cobra4.cli", "run", str(f)],
        capture_output=True, text=True, cwd=tmp_path,
    )
    return proc.returncode, proc.stdout, proc.stderr


def test_e2e_agent_with_mock_provider_returns_stop_text(tmp_path: Path) -> None:
    src = (
        "lang use llm\n"
        "use asyncio\n"
        "use cobra4.runtime.llm as _llm\n"
        "\n"
        "agent greet(name: str) -> str {\n"
        "    tools: []\n"
        "    model: \"claude-sonnet-4-6\"\n"
        "    prompt \"hi {name}\"\n"
        "}\n"
        "\n"
        "_llm.set_provider(_llm.MockProvider(scripted=[\n"
        "    _llm.Response(kind=\"stop\", text=\"hello, ada!\"),\n"
        "]))\n"
        "out = asyncio.run(greet(\"ada\"))\n"
        "log(\"r\", v=out)\n"
    )
    code, _, stderr = _run_c4(tmp_path, src)
    assert code == 0, stderr
    assert "hello, ada" in stderr


def test_e2e_agent_invokes_tool_then_stops(tmp_path: Path) -> None:
    src = (
        "lang use llm\n"
        "use asyncio\n"
        "use cobra4.runtime.llm as _llm\n"
        "\n"
        "fn lookup(key: str) -> str {\n"
        "    \"Look up by key.\"\n"
        "    return \"value-{key}\"\n"
        "}\n"
        "\n"
        "agent ask(question: str) -> str {\n"
        "    tools: [lookup]\n"
        "    model: \"claude-sonnet-4-6\"\n"
        "    prompt \"q: {question}\"\n"
        "}\n"
        "\n"
        "_llm.set_provider(_llm.MockProvider(scripted=[\n"
        "    _llm.Response(kind=\"tool_use\", tool_calls=[\n"
        "        _llm.ToolCall(name=\"lookup\", arguments={\"key\":\"abc\"}, tool_use_id=\"t1\"),\n"
        "    ]),\n"
        "    _llm.Response(kind=\"stop\", text=\"answer using value-abc\"),\n"
        "]))\n"
        "out = asyncio.run(ask(\"q?\"))\n"
        "log(\"out\", v=out)\n"
    )
    code, _, stderr = _run_c4(tmp_path, src)
    assert code == 0, stderr
    assert "value-abc" in stderr


def test_e2e_agent_prompt_substitution_at_call_site(tmp_path: Path) -> None:
    """The `{question}` in the prompt should be substituted by cobra4's
    string interpolation at agent-call time. Verify by capturing the
    provider's `messages` and checking the prompt content."""
    src = (
        "lang use llm\n"
        "use asyncio\n"
        "use cobra4.runtime.llm as _llm\n"
        "\n"
        "agent ask(question: str) -> str {\n"
        "    tools: []\n"
        "    model: \"x\"\n"
        "    prompt \"\"\"interrogated with: {question}\"\"\"\n"
        "}\n"
        "\n"
        "mock = _llm.MockProvider(scripted=[\n"
        "    _llm.Response(kind=\"stop\", text=\"k\"),\n"
        "])\n"
        "_llm.set_provider(mock)\n"
        "asyncio.run(ask(\"why is the sky blue?\"))\n"
        "log(\"sent_prompt\", v=mock.calls[0][\"messages\"][0][\"content\"])\n"
    )
    code, _, stderr = _run_c4(tmp_path, src)
    assert code == 0, stderr
    assert "why is the sky blue?" in stderr
