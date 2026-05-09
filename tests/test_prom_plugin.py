"""Tests for the Prometheus plugin and runtime."""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

from cobra4.plugins.builtin.prom import _transform, _split_kwargs
from cobra4.runtime.prom import (
    make_counter, make_histogram, make_gauge, metrics_text, reset_registry,
)


@pytest.fixture(autouse=True)
def _reset_metrics():
    reset_registry()
    yield
    reset_registry()


# ---------- plugin source transform ----------


def test_transform_counter_no_kwargs() -> None:
    src = "metric counter c\n"
    out = _transform(src)
    assert 'c = _c4_prom_make_counter("c")' in out


def test_transform_counter_with_labels_and_doc() -> None:
    src = 'metric counter requests labels=["method", "status"] doc="HTTP requests"\n'
    out = _transform(src)
    assert 'requests = _c4_prom_make_counter("requests"' in out
    assert 'labels=["method", "status"]' in out
    assert 'doc="HTTP requests"' in out


def test_transform_histogram_with_buckets() -> None:
    src = 'metric histogram h labels=["r"] buckets=[10.0, 50.0] doc="x"\n'
    out = _transform(src)
    assert 'h = _c4_prom_make_histogram("h"' in out
    assert 'buckets=[10.0, 50.0]' in out


def test_transform_gauge_minimal() -> None:
    src = 'metric gauge active doc="connections"\n'
    out = _transform(src)
    assert 'active = _c4_prom_make_gauge("active"' in out


def test_transform_does_not_touch_other_lines() -> None:
    src = "fn helper() = 42\nmetric counter c\nlog(\"ok\")\n"
    out = _transform(src)
    assert "fn helper() = 42" in out
    assert "log(\"ok\")" in out


def test_split_kwargs_parses_list_with_strings() -> None:
    out = _split_kwargs('labels=["a", "b"] doc="x"')
    assert out == ['labels=["a", "b"]', 'doc="x"']


def test_split_kwargs_handles_no_kwargs() -> None:
    assert _split_kwargs("") == []


# ---------- runtime: counter ----------


def test_counter_increments() -> None:
    c = make_counter("c", labels=["lbl"])
    c.labels(lbl="a").inc()
    c.labels(lbl="a").inc(2)
    c.labels(lbl="b").inc()
    out = metrics_text()
    assert 'c{lbl="a"} 3.0' in out
    assert 'c{lbl="b"} 1.0' in out


def test_counter_without_labels_uses_inc_directly() -> None:
    c = make_counter("plain")
    c.inc()
    c.inc(4)
    assert "plain 5.0" in metrics_text()


def test_counter_rejects_inc_without_labels_when_labels_required() -> None:
    c = make_counter("c", labels=["x"])
    with pytest.raises(ValueError, match="requires .labels"):
        c.inc()


def test_counter_rejects_label_mismatch() -> None:
    c = make_counter("c", labels=["x", "y"])
    with pytest.raises(ValueError, match="expects labels"):
        c.labels(x="a").inc()


# ---------- runtime: histogram ----------


def test_histogram_observes_into_buckets() -> None:
    h = make_histogram("h", buckets=[1.0, 5.0, 10.0])
    h.observe(0.5)  # bucket 1
    h.observe(3.0)  # bucket 5
    h.observe(7.0)  # bucket 10
    h.observe(20.0)  # bucket inf
    out = metrics_text()
    assert "h_count 4" in out
    assert "h_sum 30.5" in out


def test_histogram_with_labels() -> None:
    h = make_histogram("h", labels=["r"], buckets=[10.0, 100.0])
    h.labels(r="/x").observe(5)
    out = metrics_text()
    assert 'h_count{r="/x"} 1' in out


# ---------- runtime: gauge ----------


def test_gauge_set_and_inc() -> None:
    g = make_gauge("g")
    g.set(7)
    g.inc()
    g.inc(2)
    assert "g 10.0" in metrics_text()


def test_gauge_with_labels_set() -> None:
    g = make_gauge("g", labels=["env"])
    g.labels(env="prod").set(42)
    g.labels(env="staging").set(7)
    out = metrics_text()
    assert 'g{env="prod"} 42.0' in out
    assert 'g{env="staging"} 7.0' in out


# ---------- exposition format ----------


def test_metrics_text_includes_help_and_type_lines() -> None:
    make_counter("c", doc="my counter")
    out = metrics_text()
    assert "# HELP c my counter" in out
    assert "# TYPE c counter" in out


def test_metrics_text_empty_when_no_metrics() -> None:
    out = metrics_text()
    assert out.strip() == ""


# ---------- end-to-end via cobra4 source ----------


def _run_c4(tmp_path: Path, src: str) -> tuple[int, str, str]:
    f = tmp_path / "prog.c4"
    f.write_text(src)
    proc = subprocess.run(
        [sys.executable, "-m", "cobra4.cli", "run", str(f)],
        capture_output=True, text=True, cwd=tmp_path,
    )
    return proc.returncode, proc.stdout, proc.stderr


def test_e2e_counter_increments_and_exposes(tmp_path: Path) -> None:
    src = (
        'lang use prom\n'
        'metric counter requests labels=["status"] doc="reqs"\n'
        'requests.labels(status="200").inc()\n'
        'requests.labels(status="200").inc()\n'
        'log("text", out=prom.metrics_text())\n'
    )
    code, _, stderr = _run_c4(tmp_path, src)
    assert code == 0, stderr
    assert "requests" in stderr
    # The counter incremented twice with status=200
    assert '200' in stderr


def test_e2e_histogram_observe(tmp_path: Path) -> None:
    src = (
        'lang use prom\n'
        'metric histogram lat buckets=[10.0, 100.0] doc="latency"\n'
        'lat.observe(5)\n'
        'lat.observe(50)\n'
        'log("text", out=prom.metrics_text())\n'
    )
    code, _, stderr = _run_c4(tmp_path, src)
    assert code == 0, stderr
    assert "lat" in stderr


def test_e2e_gauge_set(tmp_path: Path) -> None:
    src = (
        'lang use prom\n'
        'metric gauge active doc="conns"\n'
        'active.set(42)\n'
        'log("text", out=prom.metrics_text())\n'
    )
    code, _, stderr = _run_c4(tmp_path, src)
    assert code == 0, stderr
    assert "active" in stderr
