"""Source map cobra4 → Python with column precision.

Records ``python_line:python_col → cobra4_line:cobra4_col`` so:

- ``c4 run`` rewrites tracebacks back to the cobra4 source.
- LSP can report inferred-type spans precisely.

Storage format: each Python line maps to a list of segments; each
segment carries a Python column range and the corresponding cobra4
location. Lookup picks the segment whose range contains the queried
column (or the last seen, if past the end).
"""

from __future__ import annotations

from bisect import bisect_right
from dataclasses import dataclass, field


@dataclass
class Segment:
    py_col: int  # start column on the Python line
    c4_line: int
    c4_col: int


@dataclass
class SourceMap:
    cobra4_path: str = ""
    # py_line -> sorted segments by py_col
    lines: dict[int, list[Segment]] = field(default_factory=dict)

    # ---------- writing ----------

    def record(
        self, py_line: int, c4_line: int, py_col: int = 0, c4_col: int = 0
    ) -> None:
        segs = self.lines.setdefault(py_line, [])
        segs.append(Segment(py_col=py_col, c4_line=c4_line, c4_col=c4_col))
        # Keep sorted for binary-search lookup.
        segs.sort(key=lambda s: s.py_col)

    # ---------- reading ----------

    def lookup_line(self, py_line: int) -> int:
        """Backward-compatible API: return the cobra4 line for a Python line."""
        segs = self.lines.get(py_line)
        if not segs:
            return 0
        return segs[0].c4_line

    # Old name kept for compatibility.
    lookup = lookup_line

    def lookup_position(self, py_line: int, py_col: int) -> tuple[int, int]:
        """Return ``(c4_line, c4_col)`` for a given Python position.

        Picks the latest segment whose ``py_col`` is ≤ the queried
        column. Returns ``(0, 0)`` if no segment exists.
        """
        segs = self.lines.get(py_line)
        if not segs:
            return (0, 0)
        cols = [s.py_col for s in segs]
        idx = bisect_right(cols, py_col) - 1
        if idx < 0:
            idx = 0
        s = segs[idx]
        return (s.c4_line, s.c4_col)

    # ---------- serialization ----------

    def serialize(self) -> str:
        """Plain-text format, one segment per line: ``py_line:py_col:c4_line:c4_col``."""
        rows = []
        for py_line in sorted(self.lines):
            for seg in self.lines[py_line]:
                rows.append(f"{py_line}:{seg.py_col}:{seg.c4_line}:{seg.c4_col}")
        return "\n".join(rows)

    @classmethod
    def parse(cls, text: str, cobra4_path: str = "") -> "SourceMap":
        m = cls(cobra4_path=cobra4_path)
        for line in text.splitlines():
            line = line.strip()
            if not line:
                continue
            parts = line.split(":")
            if len(parts) == 4:
                py_line, py_col, c4_line, c4_col = (int(x) for x in parts)
                m.record(py_line, c4_line, py_col, c4_col)
            elif len(parts) == 2:  # backwards-compat with old format
                py, c4 = (int(x) for x in parts)
                m.record(py, c4, 0, 0)
        return m
