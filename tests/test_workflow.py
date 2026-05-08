"""Tests for workflow / task DAG runner.

Two layers:
- Pure runtime tests on `Workflow` (Python API).
- End-to-end tests through `c4 run` on the cobra4 syntax.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

from cobra4.parser import parse
from cobra4 import ast_nodes as N
from cobra4.codegen import generate
from cobra4.runtime.workflow import Workflow, WorkflowError


# ---------- runtime ----------


def test_workflow_runs_linear_chain() -> None:
    wf = Workflow("test")
    wf.add("a", lambda: 10, deps=())
    wf.add("b", lambda a: a + 1, deps=("a",))
    wf.add("c", lambda b: b * 2, deps=("b",))
    out = wf.run()
    assert out == {"a": 10, "b": 11, "c": 22}


def test_workflow_runs_diamond() -> None:
    wf = Workflow("d")
    wf.add("root", lambda: 1, deps=())
    wf.add("left", lambda r: r + 10, deps=("root",))
    wf.add("right", lambda r: r + 100, deps=("root",))
    wf.add("merged", lambda l, r: l + r, deps=("left", "right"))
    out = wf.run()
    assert out == {"root": 1, "left": 11, "right": 101, "merged": 112}


def test_workflow_undeclared_dep_errors_at_add_time() -> None:
    wf = Workflow("d")
    with pytest.raises(WorkflowError, match="undeclared"):
        wf.add("late", lambda x: x, deps=("missing",))


def test_workflow_redeclaration_errors() -> None:
    wf = Workflow("d")
    wf.add("a", lambda: 1, deps=())
    with pytest.raises(WorkflowError, match="redeclared"):
        wf.add("a", lambda: 2, deps=())


def test_workflow_retries_count() -> None:
    state = {"count": 0}

    def flaky():
        state["count"] += 1
        if state["count"] < 3:
            raise ValueError("not yet")
        return "ok"

    wf = Workflow("d")
    wf.add("x", flaky, deps=(), retries=5)
    out = wf.run()
    assert out["x"] == "ok"
    assert state["count"] == 3


def test_workflow_retries_exhausted_raises_workflowerror() -> None:
    def always_fails():
        raise RuntimeError("nope")

    wf = Workflow("d")
    wf.add("x", always_fails, deps=(), retries=2)
    with pytest.raises(WorkflowError, match="failed after 3"):
        wf.run()


def test_workflow_on_failure_recovers() -> None:
    wf = Workflow("d")
    wf.add(
        "x",
        lambda: (_ for _ in ()).throw(ValueError("boom")),
        deps=(),
        retries=0,
        on_failure=lambda e: f"recovered: {e}",
    )
    out = wf.run()
    assert "recovered" in out["x"]


def test_workflow_timeout_raises() -> None:
    import time as _time

    def slow():
        _time.sleep(0.5)
        return 42

    wf = Workflow("d")
    wf.add("x", slow, deps=(), timeout=0.05)
    with pytest.raises(WorkflowError, match="failed"):
        wf.run()


def test_workflow_topo_handles_unordered_declaration() -> None:
    """Declaring `b` (depends on `a`) before `a` is rejected at add()
    because deps must already be declared. Confirm the message."""
    wf = Workflow("d")
    with pytest.raises(WorkflowError, match="undeclared"):
        wf.add("b", lambda a: a, deps=("a",))


def test_workflow_independent_tasks_run_in_order_for_determinism() -> None:
    """Tasks with no deps run in declaration order — keeps logs / errors stable."""
    seen: list[str] = []
    wf = Workflow("d")
    wf.add("first", lambda: seen.append("first"), deps=())
    wf.add("second", lambda: seen.append("second"), deps=())
    wf.add("third", lambda: seen.append("third"), deps=())
    wf.run()
    assert seen == ["first", "second", "third"]


# ---------- parser / codegen ----------


def test_parse_workflow_decl_lists_tasks() -> None:
    src = (
        "workflow w {\n"
        "    a = task f()\n"
        "    b = task g(a)\n"
        "}\n"
    )
    m = parse(src)
    assert isinstance(m.body[0], N.WorkflowDecl)
    wf = m.body[0]
    assert wf.name == "w"
    assert [t.var for t in wf.tasks] == ["a", "b"]


def test_codegen_emits_workflow_runtime_calls() -> None:
    src = "workflow w { a = task f() }\n"
    out = generate(parse(src)).code
    assert "_c4_wf_mod.Workflow('w')" in out
    assert ".add('a'" in out
    assert ".run()" in out


def test_codegen_extracts_dependencies_from_arg_names() -> None:
    src = "workflow w { a = task f()\n b = task g(a) }\n"
    out = generate(parse(src)).code
    # b's deps should include 'a'
    add_b = [ln for ln in out.splitlines() if ".add('b'" in ln][0]
    assert "deps=('a', )" in add_b


def test_codegen_separates_task_options_from_call_args() -> None:
    src = "workflow w { a = task f(retries=3, timeout=10) }\n"
    out = generate(parse(src)).code
    add_a = [ln for ln in out.splitlines() if ".add('a'" in ln][0]
    # Options go to .add(...), not into the lambda body call.
    assert "retries=3" in add_a
    assert "timeout=10" in add_a
    # The lambda body should not contain those kwargs.
    body = add_a.split("lambda :", 1)[1].split(",", 1)[0]
    assert "retries" not in body and "timeout" not in body


# ---------- end-to-end ----------


def _run_c4(tmp_path: Path, src: str) -> tuple[int, str, str]:
    f = tmp_path / "prog.c4"
    f.write_text(src)
    proc = subprocess.run(
        [sys.executable, "-m", "cobra4.cli", "run", str(f)],
        capture_output=True, text=True,
    )
    return proc.returncode, proc.stdout, proc.stderr


def test_e2e_simple_etl_workflow(tmp_path: Path) -> None:
    src = (
        "fn fetch() = [1, 2, 3]\n"
        "fn double(xs) {\n"
        "    out = []\n"
        "    for x in xs { out.append(x * 2) }\n"
        "    return out\n"
        "}\n"
        "workflow etl {\n"
        "    raw = task fetch()\n"
        "    transformed = task double(raw)\n"
        "}\n"
        "log(\"got\", n=len(transformed), first=transformed[0])\n"
    )
    code, _, stderr = _run_c4(tmp_path, src)
    assert code == 0, stderr
    assert "n=3" in stderr
    assert "first=2" in stderr


def test_e2e_workflow_retries_on_transient_failure(tmp_path: Path) -> None:
    src = (
        "state = {\"n\": 0}\n"
        "fn flaky() {\n"
        "    state[\"n\"] += 1\n"
        "    if state[\"n\"] < 3 { raise ValueError(\"not yet\") }\n"
        "    return \"ok\"\n"
        "}\n"
        "workflow w {\n"
        "    r = task flaky(retries=5)\n"
        "}\n"
        "log(\"r\", v=r, n=state[\"n\"])\n"
    )
    code, _, stderr = _run_c4(tmp_path, src)
    assert code == 0, stderr
    assert "v=ok" in stderr
    assert "n=3" in stderr
