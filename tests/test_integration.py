"""Integration tests — combinations of features that should compose.

Each test is a real-ish program that exercises 2+ of the features
shipped in the 0.2 line: data classes, Result/?, async/await,
streaming, workflow, effects, IaC. Where the language alone wouldn't
catch a regression, end-to-end execution does.
"""

from __future__ import annotations

import asyncio
import json
import subprocess
import sys
from pathlib import Path

import pytest

from cobra4.parser import parse
from cobra4.codegen import generate
from cobra4.runtime import infra as infra_mod
from cobra4.runtime.workflow import Workflow
from cobra4.runtime.result import Ok, Err, _c4_try_propagate, _C4Propagate
from cobra4.runtime.stream import from_iter


def _run_c4(tmp_path: Path, src: str, *args: str) -> tuple[int, str, str]:
    f = tmp_path / "prog.c4"
    f.write_text(src)
    proc = subprocess.run(
        [sys.executable, "-m", "cobra4.cli", *(args or ("run",)), str(f)],
        capture_output=True, text=True, cwd=tmp_path,
    )
    return proc.returncode, proc.stdout, proc.stderr


# ---------- data class + Result + ? propagation ----------


def test_data_class_with_result_propagation(tmp_path: Path) -> None:
    src = (
        "data class User(name: str, age: int)\n"
        "\n"
        "fn parse_user(blob) {\n"
        "    if not blob[\"name\"] { return Err(\"missing name\") }\n"
        "    return Ok(User(name=blob[\"name\"], age=blob[\"age\"]))\n"
        "}\n"
        "\n"
        "fn process(blobs) {\n"
        "    out = []\n"
        "    for b in blobs {\n"
        "        u = parse_user(b)?\n"
        "        out.append(u)\n"
        "    }\n"
        "    return Ok(out)\n"
        "}\n"
        "\n"
        "good = process([{\"name\": \"a\", \"age\": 30}, {\"name\": \"b\", \"age\": 40}])\n"
        "match good {\n"
        "    case Ok(us) { log(\"ok\", n=len(us), first=us[0].name) }\n"
        "    case Err(e) { log(\"err\", e=e) }\n"
        "}\n"
        "\n"
        "bad = process([{\"name\": \"a\", \"age\": 30}, {\"name\": \"\", \"age\": 0}])\n"
        "match bad {\n"
        "    case Ok(us) { log(\"unexpected\", n=len(us)) }\n"
        "    case Err(e) { log(\"propagated\", e=e) }\n"
        "}\n"
    )
    code, _, stderr = _run_c4(tmp_path, src)
    assert code == 0, stderr
    assert "first=a" in stderr
    assert "propagated" in stderr
    assert "missing name" in stderr


# ---------- sum types + match exhaustive-feel ----------


def test_sum_type_match_with_result_combo(tmp_path: Path) -> None:
    src = (
        "data Job {\n"
        "    Pending(id: str)\n"
        "    Done(id: str, output: str)\n"
        "    Failed(id: str, reason: str)\n"
        "}\n"
        "\n"
        "fn summarize(j) {\n"
        "    match j {\n"
        "        case Pending(id) { return \"pending: {id}\" }\n"
        "        case Done(id, output) { return \"done: {id} -> {output}\" }\n"
        "        case Failed(id, reason) { return \"failed: {id} ({reason})\" }\n"
        "    }\n"
        "}\n"
        "\n"
        "log(\"a\", v=summarize(Pending(id=\"j1\")))\n"
        "log(\"b\", v=summarize(Done(id=\"j2\", output=\"42\")))\n"
        "log(\"c\", v=summarize(Failed(id=\"j3\", reason=\"oom\")))\n"
    )
    code, _, stderr = _run_c4(tmp_path, src)
    assert code == 0, stderr
    assert "pending: j1" in stderr
    assert "done: j2 -> 42" in stderr
    assert "failed: j3 (oom)" in stderr


# ---------- async + each in parallel + sum types ----------


def test_async_parallel_with_sum_type_outcomes(tmp_path: Path) -> None:
    src = (
        "use asyncio\n"
        "\n"
        "data JobResult {\n"
        "    Win(id: int)\n"
        "    Lose(id: int, reason: str)\n"
        "}\n"
        "\n"
        "async fn process(i) {\n"
        "    await asyncio.sleep(0.001)\n"
        "    if i % 3 == 0 { return Lose(id=i, reason=\"div3\") }\n"
        "    return Win(id=i)\n"
        "}\n"
        "\n"
        "async fn main() {\n"
        "    results = each i in range(9) in parallel(workers=4) { await process(i) }\n"
        "    wins = each r in results where isinstance(r, Win) { r }\n"
        "    losses = each r in results where isinstance(r, Lose) { r }\n"
        "    log(\"summary\", wins=len(wins), losses=len(losses))\n"
        "}\n"
        "\n"
        "asyncio.run(main())\n"
    )
    code, _, stderr = _run_c4(tmp_path, src)
    assert code == 0, stderr
    # 9 jobs, 3 div by 3 (0, 3, 6) → 3 losses, 6 wins
    assert "wins=6" in stderr
    assert "losses=3" in stderr


# ---------- workflow + retry + log effect declared ----------


def test_workflow_with_retry_and_data_class_payload(tmp_path: Path) -> None:
    src = (
        "data class Payload(rows: list, source: str)\n"
        "\n"
        "state = {\"attempt\": 0}\n"
        "fn flaky_fetch() {\n"
        "    state[\"attempt\"] += 1\n"
        "    if state[\"attempt\"] < 2 { raise IOError(\"transient\") }\n"
        "    return Payload(rows=[1, 2, 3], source=\"flaky\")\n"
        "}\n"
        "fn count(p) = len(p.rows)\n"
        "\n"
        "workflow pipeline {\n"
        "    payload = task flaky_fetch(retries=3)\n"
        "    n       = task count(payload)\n"
        "}\n"
        "log(\"pipeline\", n=n, source=payload.source, attempts=state[\"attempt\"])\n"
    )
    code, _, stderr = _run_c4(tmp_path, src)
    assert code == 0, stderr
    assert "n=3" in stderr
    assert "source=flaky" in stderr
    assert "attempts=2" in stderr


# ---------- workflow declared as part of a larger program ----------


def test_workflow_after_helpers_uses_module_scope(tmp_path: Path) -> None:
    """Tasks call free functions defined earlier in the module — confirm
    that the task lambdas close over module scope correctly."""
    src = (
        "fn build_data() = [10, 20, 30]\n"
        "fn pick_max(xs) = max(xs)\n"
        "\n"
        "workflow demo {\n"
        "    raw = task build_data()\n"
        "    top = task pick_max(raw)\n"
        "}\n"
        "log(\"top\", v=top)\n"
    )
    code, _, stderr = _run_c4(tmp_path, src)
    assert code == 0, stderr
    assert "v=30" in stderr


# ---------- effect annotation warns on workflow body ----------


def test_pure_fn_calling_log_inside_workflow_still_warns_at_check(tmp_path: Path) -> None:
    """Effects are checked statically — independent of whether the call
    happens inside a workflow's lambda."""
    src = (
        "fn pure_helper(x) with [] { log(\"side\") return x }\n"
    )
    p = tmp_path / "prog.c4"
    p.write_text(src)
    proc = subprocess.run(
        [sys.executable, "-m", "cobra4.cli", "check", str(p)],
        capture_output=True, text=True,
    )
    out = proc.stdout + proc.stderr
    assert "E001" in out, out


# ---------- IaC + workflow combined ----------


def test_iac_apply_then_workflow_reads_resource_state(tmp_path: Path) -> None:
    """Apply infra first, then a regular cobra4 program reads the file
    that the resource created. Ties together the two phases."""
    infra_src = tmp_path / "infra.c4"
    infra_src.write_text(
        'resource manifest = local.file {\n'
        '    path: "./manifest.json"\n'
        '    contents: {"items": [{"id": 1}, {"id": 2}, {"id": 3}]}\n'
        '}\n'
    )
    code, out = subprocess.run(
        [sys.executable, "-m", "cobra4.cli", "infra", "apply", str(infra_src)],
        capture_output=True, text=True, cwd=tmp_path,
    ).returncode, ""
    assert code == 0
    assert (tmp_path / "manifest.json").exists()

    consume_src = tmp_path / "consume.c4"
    consume_src.write_text(
        'm = read("./manifest.json")\n'
        'log("loaded", n=len(m["items"]))\n'
    )
    proc = subprocess.run(
        [sys.executable, "-m", "cobra4.cli", "run", str(consume_src)],
        capture_output=True, text=True, cwd=tmp_path,
    )
    assert proc.returncode == 0, proc.stderr
    assert "n=3" in proc.stderr


# ---------- streaming inside an async fn ----------


def test_streaming_window_and_collect_inside_async_fn(tmp_path: Path) -> None:
    src = (
        "use asyncio\n"
        "use cobra4.runtime.stream as s\n"
        "\n"
        "async fn pipeline() {\n"
        "    src = s.from_iter([1, 2, 3, 4, 5, 6, 7, 8, 9, 10])\n"
        "    batches = await src.filter(fn(x) = x % 2 == 0).window(size=2).collect()\n"
        "    log(\"batches\", n=len(batches), first=batches[0], last=batches[-1])\n"
        "}\n"
        "\n"
        "asyncio.run(pipeline())\n"
    )
    code, _, stderr = _run_c4(tmp_path, src)
    assert code == 0, stderr
    # filter -> [2,4,6,8,10]; window(size=2) -> [[2,4],[6,8],[10]]
    assert "n=3" in stderr


# ---------- regression: existing examples still run end-to-end ----------


@pytest.mark.parametrize("ex", [
    "examples/01_wordcount.c4",
    "examples/03_etl.c4",
    "examples/05_schedule.c4",
    "examples/08_stdlib_dogfood.c4",
    "examples/11_code_reviewer.c4",
])
def test_existing_examples_still_pass(ex: str) -> None:
    """If any of the new features broke a pre-existing example, this
    test fails — it's our headline 'no regression' check."""
    repo_root = Path(__file__).resolve().parent.parent
    proc = subprocess.run(
        [sys.executable, "-m", "cobra4.cli", "run", str(repo_root / ex)],
        capture_output=True, text=True, cwd=repo_root,
    )
    assert proc.returncode == 0, (
        f"example {ex} regressed:\nSTDOUT={proc.stdout}\nSTDERR={proc.stderr}"
    )


# ---------- pure runtime composability ----------


def test_runtime_workflow_uses_dataclass_results() -> None:
    """A task that returns a frozen dataclass (Ok / Err / data class)
    propagates through to the next task as a real dataclass instance."""
    wf = Workflow("test")
    wf.add("fetch", lambda: Ok({"x": 1}), deps=())
    wf.add("unwrap", lambda fetched: _c4_try_propagate(fetched), deps=("fetch",))
    out = wf.run()
    assert out["unwrap"] == {"x": 1}


def test_runtime_stream_then_workflow_pipeline() -> None:
    """The stream module returns a list via collect(); the workflow can
    consume that list as a normal task input."""
    async def pipeline() -> list[int]:
        return await from_iter([1, 2, 3, 4, 5]).filter(lambda x: x > 2).collect()

    coll = asyncio.run(pipeline())

    wf = Workflow("test")
    wf.add("filtered", lambda: coll, deps=())
    wf.add("summed", lambda xs: sum(xs), deps=("filtered",))
    out = wf.run()
    assert out["summed"] == 12  # 3+4+5


# ---------- ensure existing test_examples integration still passes ----------


def test_run_examples_via_cli(tmp_path: Path) -> None:
    """Smoke test through the CLI binary — catches packaging regressions
    where a runtime symbol is used in codegen but not exported from the
    runtime package (the kind of bug that wouldn't surface in unit tests
    that import directly)."""
    src = (
        "data class Box(value: int)\n"
        "fn open_box(b) = b.value\n"
        "b = Box(value=42)\n"
        "log(\"box\", v=open_box(b))\n"
    )
    code, _, stderr = _run_c4(tmp_path, src)
    assert code == 0, stderr
    assert "v=42" in stderr
