"""Tests for the built-in benchmark runner."""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

from cobra4.tools.bench import (
    BenchResult,
    run_benchmarks,
    time_budget,
    format_table,
    format_compare,
)

# ---------- runner ----------


def test_time_budget_runs_for_about_the_requested_time() -> None:
    counter = [0]

    def fn():
        counter[0] += 1

    r = time_budget("trivial", fn, seconds=0.05)
    assert r.iterations >= 2
    assert r.total_seconds > 0


def test_time_budget_collects_samples_and_computes_percentiles() -> None:
    r = time_budget("noop", lambda: None, seconds=0.05)
    assert r.samples
    assert r.p50_us >= 0
    assert r.p95_us >= r.p50_us


def test_time_budget_is_capped_at_10k_iterations() -> None:
    r = time_budget("noop", lambda: None, seconds=10.0)  # would run forever
    assert r.iterations <= 10_000


# ---------- targets ----------


def test_run_benchmarks_with_all_targets_returns_one_result_each() -> None:
    results = run_benchmarks(seconds=0.05)
    assert len(results) >= 5
    names = {r.name for r in results}
    assert {
        "parser",
        "codegen",
        "smart-dispatch",
        "workflow",
        "async-parallel",
    } <= names


def test_run_benchmarks_can_filter_to_subset() -> None:
    results = run_benchmarks(["parser", "codegen"], seconds=0.05)
    assert {r.name for r in results} == {"parser", "codegen"}


def test_run_benchmarks_warns_on_unknown_target(capsys) -> None:
    run_benchmarks(["totally-fake"], seconds=0.05)
    err = capsys.readouterr().err
    assert "unknown" in err


# ---------- output formatting ----------


def test_format_table_has_header_and_rows() -> None:
    results = [BenchResult(name="x", iterations=100, total_seconds=1.0)]
    results[0].samples = [0.01] * 100
    out = format_table(results)
    assert "target" in out
    assert "x" in out
    assert "ops/s" in out


def test_format_compare_shows_delta_percent() -> None:
    cur = [BenchResult(name="x", iterations=100, total_seconds=1.0)]
    cur[0].samples = [0.01] * 100
    base = [{"name": "x", "ops_per_sec": 50.0}]
    out = format_compare(cur, base)
    assert "Δ%" in out
    # 100 / 50 = 2x → +100%
    assert "+100" in out or "100.0" in out


# ---------- CLI ----------


def _run_c4(args: list[str], cwd: Path) -> tuple[int, str]:
    proc = subprocess.run(
        [sys.executable, "-m", "cobra4.cli", *args],
        capture_output=True,
        text=True,
        cwd=cwd,
    )
    return proc.returncode, proc.stdout + "\n" + proc.stderr


def test_cli_bench_runs_default(tmp_path: Path) -> None:
    code, out = _run_c4(["bench", "--seconds", "0.05"], tmp_path)
    assert code == 0, out
    assert "ops/s" in out
    assert "parser" in out


def test_cli_bench_writes_json(tmp_path: Path) -> None:
    out_path = tmp_path / "results.json"
    code, _ = _run_c4(
        ["bench", "parser", "--seconds", "0.05", "--json", str(out_path)], tmp_path
    )
    assert code == 0
    assert out_path.exists()
    data = json.loads(out_path.read_text())
    assert data
    assert data[0]["name"] == "parser"
    assert "ops_per_sec" in data[0]


def test_cli_bench_compare_against_baseline(tmp_path: Path) -> None:
    baseline = tmp_path / "baseline.json"
    baseline.write_text(json.dumps([{"name": "parser", "ops_per_sec": 1.0}]))
    code, out = _run_c4(
        ["bench", "parser", "--seconds", "0.05", "--compare", str(baseline)], tmp_path
    )
    assert code == 0, out
    assert "Δ%" in out


def test_cli_bench_subset_filters_targets(tmp_path: Path) -> None:
    out_path = tmp_path / "subset.json"
    _run_c4(
        ["bench", "parser", "codegen", "--seconds", "0.05", "--json", str(out_path)],
        tmp_path,
    )
    data = json.loads(out_path.read_text())
    names = {d["name"] for d in data}
    assert names == {"parser", "codegen"}
