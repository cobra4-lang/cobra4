"""Tests for `data class` shorthand and sum-type `data ... { ... }` declarations."""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

from cobra4.parser import parse
from cobra4 import ast_nodes as N
from cobra4.codegen import generate


def test_parse_data_class_declares_fields() -> None:
    m = parse("data class Point(x: int, y: int = 0)\n")
    assert len(m.body) == 1
    s = m.body[0]
    assert isinstance(s, N.DataClassDecl)
    assert s.name == "Point"
    assert [f.name for f in s.fields] == ["x", "y"]
    assert s.fields[0].default is None
    assert s.fields[1].default is not None  # `y = 0`


def test_parse_data_sum_declares_variants() -> None:
    src = """data Event {
    Placed(id: str, total: float)
    Refunded(id: str, reason: str)
    Shipped(id: str)
}
"""
    m = parse(src)
    assert len(m.body) == 1
    s = m.body[0]
    assert isinstance(s, N.DataSumDecl)
    assert s.name == "Event"
    assert [v.name for v in s.variants] == ["Placed", "Refunded", "Shipped"]
    assert [f.name for f in s.variants[0].fields] == ["id", "total"]


def test_parse_data_class_no_fields() -> None:
    """Empty body — degenerates to a marker class."""
    m = parse("data class Marker()\n")
    s = m.body[0]
    assert isinstance(s, N.DataClassDecl)
    assert s.fields == []


def test_codegen_data_class_emits_dataclass() -> None:
    m = parse("data class Point(x: int, y: int = 0)\n")
    out = generate(m).code
    assert "@_c4_dc.dataclass" in out
    assert "class Point:" in out
    assert "x: int" in out
    assert "y: int = 0" in out


def test_codegen_data_sum_emits_base_and_variant_subclasses() -> None:
    src = """data Event {
    Placed(id: str)
    Refunded(id: str, reason: str)
}
"""
    m = parse(src)
    out = generate(m).code
    assert "class Event:" in out
    assert "class Placed(Event):" in out
    assert "class Refunded(Event):" in out


def test_codegen_required_fields_come_before_defaults() -> None:
    """@dataclass requires non-default fields before default ones — even
    if the user wrote them in a different order."""
    m = parse("data class C(a: int = 0, b: int)\n")
    out = generate(m).code
    a_pos = out.find("a: int = 0")
    b_pos = out.find("b: int")
    assert b_pos < a_pos, f"required field 'b' must precede defaulted 'a': {out!r}"


def _run_c4(tmp_path: Path, src: str) -> tuple[int, str, str]:
    f = tmp_path / "prog.c4"
    f.write_text(src)
    proc = subprocess.run(
        [sys.executable, "-m", "cobra4.cli", "run", str(f)],
        capture_output=True, text=True,
    )
    return proc.returncode, proc.stdout, proc.stderr


def test_runtime_data_class_can_be_constructed_and_accessed(tmp_path: Path) -> None:
    src = (
        "data class Point(x: int, y: int = 0)\n"
        "p = Point(3, 4)\n"
        "log(\"pt\", x=p.x, y=p.y)\n"
    )
    code, _stdout, stderr = _run_c4(tmp_path, src)
    assert code == 0, stderr
    assert "x=3" in stderr and "y=4" in stderr  # log() goes to stderr


def test_runtime_data_sum_pattern_match(tmp_path: Path) -> None:
    src = (
        "data Event {\n"
        "    Placed(id: str)\n"
        "    Refunded(id: str, reason: str)\n"
        "}\n"
        "ev = Refunded(id=\"a\", reason=\"too expensive\")\n"
        "match ev {\n"
        "    case Placed(id) { log(\"placed\", id=id) }\n"
        "    case Refunded(id, reason) { log(\"refunded\", id=id, reason=reason) }\n"
        "}\n"
    )
    code, _stdout, stderr = _run_c4(tmp_path, src)
    assert code == 0, stderr
    assert "refunded" in stderr
    assert "id=a" in stderr
