"""Tests for declarative infrastructure (resource declarations + lifecycle).

Covers:
- Parsing `resource` blocks.
- Codegen produces `_c4_infra.declare_resource` calls.
- Runtime: registry, plan / apply / destroy lifecycle.
- The built-in `local.file` adapter, including idempotency.
- Cross-resource references (`other.field` in field blocks).
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

from cobra4.parser import parse
from cobra4 import ast_nodes as N
from cobra4.codegen import generate
from cobra4.runtime import infra as infra_mod


@pytest.fixture(autouse=True)
def _clear_registry():
    infra_mod.clear_registry()
    yield
    infra_mod.clear_registry()


# ---------- parsing ----------


def test_parse_resource_decl_keeps_adapter_path() -> None:
    m = parse(
        "resource hello = local.file {\n"
        '    path: "./x.json"\n'
        '    contents: {"a": 1}\n'
        "}\n"
    )
    r = m.body[0]
    assert isinstance(r, N.ResourceDecl)
    assert r.name == "hello"
    assert r.adapter_path == "local.file"
    assert [k for k, _ in r.fields] == ["path", "contents"]


def test_parse_resource_with_dotted_adapter_path() -> None:
    m = parse("resource api = aws.lambda {\n" '    name: "my-fn"\n' "}\n")
    assert m.body[0].adapter_path == "aws.lambda"


def test_parse_resource_field_can_reference_other_resource() -> None:
    """Cross-references parse fine — semantic check happens at apply."""
    m = parse(
        'resource a = local.file { path: "./a.json" }\n'
        "resource b = local.file { path: a.path }\n"
    )
    assert len(m.body) == 2
    assert isinstance(m.body[1], N.ResourceDecl)


# ---------- codegen ----------


def test_codegen_emits_declare_resource_call() -> None:
    out = generate(parse('resource hello = local.file { path: "./x.json" }\n')).code
    assert "_c4_infra.declare_resource" in out
    assert "'local.file'" in out
    assert "'hello'" in out


# ---------- runtime: local.file ----------


def test_apply_creates_file(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.chdir(tmp_path)
    target = tmp_path / "out.json"
    infra_mod.declare_resource(
        "hello",
        "local.file",
        lambda: {"path": str(target), "contents": {"hi": 1}},
    )
    infra_mod.apply()
    assert target.exists()
    assert json.loads(target.read_text()) == {"hi": 1}


def test_apply_is_idempotent(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.chdir(tmp_path)
    target = tmp_path / "out.json"
    infra_mod.declare_resource(
        "hello",
        "local.file",
        lambda: {"path": str(target), "contents": {"a": 1}},
    )
    infra_mod.apply()
    actions = infra_mod.plan()
    assert len(actions) == 1
    name, action = actions[0]
    assert action.kind == "noop"


def test_apply_detects_change(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.chdir(tmp_path)
    target = tmp_path / "out.json"

    counter = {"n": 0}

    def fields():
        counter["n"] += 1
        return {"path": str(target), "contents": {"version": counter["n"]}}

    infra_mod.declare_resource("hello", "local.file", fields)
    infra_mod.apply()  # version=1 written, state stored

    # Second apply re-runs fields_fn -> version=2, plan should see UPDATE
    actions = infra_mod.plan()
    assert actions[0][1].kind == "update"
    assert "contents" in actions[0][1].diff


def test_destroy_removes_file_and_clears_state(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.chdir(tmp_path)
    target = tmp_path / "out.json"
    infra_mod.declare_resource(
        "hello",
        "local.file",
        lambda: {"path": str(target), "contents": "x"},
    )
    infra_mod.apply()
    assert target.exists()
    infra_mod.destroy()
    assert not target.exists()
    state = infra_mod.load_state()
    assert "hello" not in state


def test_resource_attribute_access_returns_state_field(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.chdir(tmp_path)
    target = tmp_path / "out.json"
    r = infra_mod.declare_resource(
        "hello",
        "local.file",
        lambda: {"path": str(target), "contents": "x"},
    )
    infra_mod.apply()
    assert r.path == str(target)
    assert r.size > 0


def test_resource_unknown_attribute_raises_attributeerror(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.chdir(tmp_path)
    target = tmp_path / "out.json"
    r = infra_mod.declare_resource(
        "hello",
        "local.file",
        lambda: {"path": str(target), "contents": "x"},
    )
    infra_mod.apply()
    with pytest.raises(AttributeError, match="hello.*has no field 'nonexistent'"):
        r.nonexistent


def test_unknown_adapter_raises_at_phase_time(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.chdir(tmp_path)
    infra_mod.declare_resource(
        "x",
        "totally.fake",
        lambda: {"foo": "bar"},
    )
    with pytest.raises(infra_mod.InfraError, match="Unknown infra adapter"):
        infra_mod.plan()


def test_register_custom_adapter() -> None:
    class _Spy:
        def __init__(self):
            self.calls = []

        def plan(self, current, desired):
            self.calls.append(("plan", current, desired))
            return infra_mod.Action(kind="noop")

        def apply(self, current, desired):
            self.calls.append(("apply", current, desired))
            return desired

        def destroy(self, current):
            self.calls.append(("destroy", current))

    spy = _Spy()
    infra_mod.register_adapter("test.spy", spy)
    infra_mod.declare_resource("r", "test.spy", lambda: {"k": "v"})
    infra_mod.plan()
    assert spy.calls[0][0] == "plan"


# ---------- end-to-end via CLI ----------


def _run_c4(args: list[str], cwd: Path) -> tuple[int, str]:
    proc = subprocess.run(
        [sys.executable, "-m", "cobra4.cli", *args],
        capture_output=True,
        text=True,
        cwd=cwd,
    )
    return proc.returncode, proc.stdout + "\n" + proc.stderr


def test_e2e_apply_creates_files_with_cross_reference(tmp_path: Path) -> None:
    src = tmp_path / "infra.c4"
    src.write_text(
        "resource hello = local.file {\n"
        '    path: "./greetings.json"\n'
        '    contents: {"hi": 1}\n'
        "}\n"
        "resource downstream = local.file {\n"
        '    path: "./derived.txt"\n'
        '    contents: "based on: {hello.path}"\n'
        "}\n"
    )
    code, out = _run_c4(["infra", "apply", str(src)], tmp_path)
    assert code == 0, out
    assert (tmp_path / "greetings.json").exists()
    derived = (tmp_path / "derived.txt").read_text()
    assert "based on: ./greetings.json" == derived


def test_e2e_plan_then_apply_then_plan_noop(tmp_path: Path) -> None:
    src = tmp_path / "infra.c4"
    src.write_text(
        "resource hello = local.file {\n"
        '    path: "./hi.txt"\n'
        '    contents: "hello"\n'
        "}\n"
    )
    code, _ = _run_c4(["infra", "apply", str(src)], tmp_path)
    assert code == 0
    code, plan_out = _run_c4(["infra", "plan", str(src)], tmp_path)
    assert code == 0
    assert "NOOP" in plan_out


def test_e2e_destroy_removes_managed_files(tmp_path: Path) -> None:
    src = tmp_path / "infra.c4"
    src.write_text(
        "resource hello = local.file {\n"
        '    path: "./bye.txt"\n'
        '    contents: "x"\n'
        "}\n"
    )
    _run_c4(["infra", "apply", str(src)], tmp_path)
    assert (tmp_path / "bye.txt").exists()
    code, _ = _run_c4(["infra", "destroy", str(src)], tmp_path)
    assert code == 0
    assert not (tmp_path / "bye.txt").exists()
